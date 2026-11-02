"""Channels: put an agent where people already are.

    widget    a chat bubble for any web page (``/widget.js``)
    slack     a Slack app: mention it in a channel or message it directly
    telegram  a Telegram bot
    discord   a Discord app with an ``/ask`` command

A channel is bound to one published agent. It answers as either

* ``fixed``: one person, the channel's creator. It never sees more than they can.
* ``mapped`` (bots): chat-app user ids mapped to directory users. Unmapped
  people are refused. Setting a map needs the administer grant.
* ``signed`` (widget): the host page passes a short-lived token its own server
  signs with the channel secret, so the widget acts as the signed-in user.

Answers that leave Smriti for a chat app get two extra checks:
personal data in the reply is masked (on by default), and an answer citing
documents marked confidential or above is withheld with a link back to
Smriti. Approvals appear as buttons, and only the person who asked can press
them. Inbound requests are authenticated: Slack signing secret, Telegram
secret token, Discord Ed25519 signature, widget Origin allow-list.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any

import httpx

from .agents import can_edit, can_use, get_row, view
from .agents import runtime as rt
from .models import Principal, class_rank
from .privacy import mask_text
from .store import channel_threads, channels

KINDS = {
    "widget": "A chat bubble for your intranet or website.",
    "slack": "Answer in Slack: mention the app in a channel or message it directly.",
    "telegram": "A Telegram bot for field staff.",
    "discord": "A Discord app with an /ask command.",
}
SECRET_FIELDS = {
    "widget": [],
    "slack": ["bot_token", "signing_secret"],
    "telegram": ["bot_token"],
    "discord": ["bot_token"],
}
LIMITS = {"slack": 3900, "telegram": 4000, "discord": 1900, "widget": 20000}
API = {
    "slack": "https://slack.com/api",
    "telegram": "https://api.telegram.org",
    "discord": "https://discord.com/api/v10",
}
HTTP = httpx.Client(timeout=15)  # replaced in tests
RATE = 30  # widget requests per minute per channel and client


class ChannelError(ValueError):
    pass


# ---------------------------------------------------------------- definitions


def _row(t: Any, cid: str) -> dict[str, Any]:
    rows = t.store.rows(channels, tenant=t.id, id=cid)
    if not rows:
        raise KeyError(f"no channel {cid}")
    return rows[0]


def public(row: dict[str, Any], base_url: str) -> dict[str, Any]:
    cfg = json.loads(row["config"] or "{}")
    out = {
        k: row[k]
        for k in (
            "id",
            "kind",
            "name",
            "agent_id",
            "acts_as",
            "enabled",
            "created_by",
            "created_at",
            "last_used",
            "calls",
        )
    }
    out["config"] = {k: v for k, v in cfg.items() if k not in ("webhook_secret", "signing_key")}
    out["has_secrets"] = bool(row["secrets"])
    base = base_url.rstrip("/")
    if row["kind"] == "widget":
        out["embed"] = (
            f'<script src="{base}/widget.js" data-smriti-widget="{row["id"]}" data-host="{base}" async></script>'
        )
    else:
        out["webhook_url"] = f"{base}/api/channels/{row['id']}/{row['kind']}"
    return out


def save(t: Any, p: Principal, box: Any, body: dict[str, Any], cid: str | None = None) -> dict[str, Any]:
    old = _row(t, cid) if cid else None
    kind = old["kind"] if old else body.get("kind")
    if kind not in KINDS:
        raise ChannelError(f"kind must be one of {', '.join(KINDS)}")
    if old and not (old["created_by"] == p.user_id or p.can("administer")):
        raise PermissionError("only the person who created this channel or an administrator can change it")
    agent_id = str(body.get("agent_id") or (old and old["agent_id"]) or "")
    agent = get_row(t, agent_id)
    if not can_edit(p, agent):
        raise PermissionError("you can only connect agents you can edit")
    if not (p.can("curate") or p.can("administer")):
        raise PermissionError("connecting an agent to a channel needs the curate or administer grant")
    prev = json.loads(old["config"]) if old else {}
    cfg: dict[str, Any] = {
        "identity": body.get("identity", prev.get("identity", "fixed")),
        "mask_pii": bool(body.get("mask_pii", prev.get("mask_pii", kind != "widget"))),
        "max_class": str(body.get("max_class", prev.get("max_class", "internal"))),
    }
    allowed = ("fixed", "signed") if kind == "widget" else ("fixed", "mapped")
    if cfg["identity"] not in allowed:
        raise ChannelError(f"identity must be one of {', '.join(allowed)} for a {kind} channel")
    if kind == "widget":
        origins = [str(o).strip().rstrip("/") for o in body.get("origins", prev.get("origins", [])) if str(o).strip()]
        for o in origins:
            if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?", o):
                raise ChannelError(f"'{o}' is not an origin like https://intranet.example.org")
        if not origins:
            raise ChannelError("list at least one site origin the widget may run on")
        cfg |= {
            "origins": origins,
            "title": str(body.get("title", prev.get("title", agent["name"])))[:80],
            "greeting": str(
                body.get("greeting", prev.get("greeting", "Ask me anything. Every answer cites its source."))
            )[:300],
            "color": str(body.get("color", prev.get("color", "#6D28D9")))[:9],
            "starters": [str(s)[:200] for s in body.get("starters", prev.get("starters", []))][:4],
            "signing_key": prev.get("signing_key") or secrets.token_urlsafe(32),
        }
    else:
        users = body.get("users", prev.get("users", {})) or {}
        if cfg["identity"] == "mapped":
            if not p.can("administer") and users != prev.get("users"):
                raise PermissionError("mapping chat-app users to people needs the administer grant")
            for uid in users.values():
                try:
                    t.principal(uid)
                except Exception:
                    raise ChannelError(f"'{uid}' is not in the directory") from None
            if not users:
                raise ChannelError("add at least one user mapping, or answer as yourself")
        cfg["users"] = {str(k): str(v) for k, v in users.items()}
        cfg["webhook_secret"] = prev.get("webhook_secret") or secrets.token_hex(24)
        if kind == "discord":
            cfg["public_key"] = str(body.get("public_key", prev.get("public_key", ""))).strip()
            cfg["application_id"] = str(body.get("application_id", prev.get("application_id", ""))).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", cfg["public_key"]):
                raise ChannelError("the Discord public key is the 64-character hex key on the app's General page")
    sec = box.open(old["secrets"]) if old else {}
    for f in SECRET_FIELDS[kind]:
        if body.get(f):
            sec[f] = str(body[f]).strip()
    missing = [f for f in SECRET_FIELDS[kind] if not sec.get(f)]
    if missing:
        raise ChannelError(f"missing {', '.join(missing)}")
    values = {
        "name": str(body.get("name") or (old and old["name"]) or f"{agent['name']} on {kind}")[:200],
        "agent_id": agent_id,
        "config": json.dumps(cfg),
        "secrets": box.seal(sec) or None,
        "enabled": bool(body.get("enabled", old["enabled"] if old else True)),
    }
    if old:
        t.store.update_row(channels, {"tenant": t.id, "id": cid}, **values)
    else:
        cid = ("wg_" if kind == "widget" else "ch_") + secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
        t.store.insert(
            channels,
            id=cid,
            tenant=t.id,
            kind=kind,
            acts_as=p.user_id,
            created_by=p.user_id,
            created_at=time.time(),
            calls=0,
            **values,
        )
    t.audit.record(p.user_id, "channel_save", channel_id=cid, kind=kind, agent_id=agent_id, identity=cfg["identity"])
    return _row(t, cid)


def list_for(t: Any, p: Principal) -> list[dict[str, Any]]:
    rows = t.store.rows(channels, order_by="created_at", tenant=t.id)
    return [r for r in rows if r["created_by"] == p.user_id or p.can("administer")]


def delete(t: Any, p: Principal, cid: str) -> None:
    row = _row(t, cid)
    if not (row["created_by"] == p.user_id or p.can("administer")):
        raise PermissionError("only the person who created this channel or an administrator can delete it")
    t.store.delete_rows(channels, tenant=t.id, id=cid)
    t.store.delete_rows(channel_threads, tenant=t.id, channel_id=cid)
    t.audit.record(p.user_id, "channel_delete", channel_id=cid)


def find(plat: Any, cid: str) -> tuple[Any, dict[str, Any]]:
    """The tenant and row for a public channel id, from any tenant."""
    for t in plat.tenants.values():
        rows = t.store.rows(channels, tenant=t.id, id=cid)
        if rows:
            if not rows[0]["enabled"]:
                raise PermissionError("this channel is turned off")
            return t, rows[0]
    raise KeyError("no such channel")


# ---------------------------------------------------------------- identity


def sign_user(key: str, user_id: str, ttl_s: int = 3600, now: float | None = None) -> str:
    """What the host page's server does to let the widget act as its signed-in user."""
    exp = int((now or time.time()) + ttl_s)
    sig = hmac.new(key.encode(), f"{user_id}.{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{exp}.{sig}"


def verify_user(key: str, token: str, now: float | None = None) -> str:
    try:
        user_id, exp, sig = token.rsplit(".", 2)
    except ValueError:
        raise PermissionError("malformed user token") from None
    good = hmac.new(key.encode(), f"{user_id}.{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        raise PermissionError("the user token's signature does not match")
    if not exp.isdigit() or int(exp) < (now or time.time()):
        raise PermissionError("the user token has expired; sign a new one")
    return user_id


def principal_for(t: Any, row: dict[str, Any], external_user: str | None = None, token: str | None = None) -> Principal:
    cfg = json.loads(row["config"])
    if cfg["identity"] == "mapped":
        uid = cfg.get("users", {}).get(str(external_user or ""))
        if not uid:
            raise PermissionError("you are not set up to use this assistant; ask your Smriti administrator")
        return t.principal(uid)
    if cfg["identity"] == "signed":
        if not token:
            raise PermissionError("sign in to the host site to use this assistant")
        return t.principal(verify_user(cfg["signing_key"], token))
    return t.principal(row["acts_as"])


# ---------------------------------------------------------------- running


def agent_for(t: Any, row: dict[str, Any]) -> dict[str, Any]:
    agent = view(get_row(t, row["agent_id"]))
    if agent["status"] != "published":
        raise PermissionError("the agent behind this channel is not published")
    return agent


def ask(t: Any, box: Any, row: dict[str, Any], p: Principal, question: str, thread: str | None) -> dict[str, Any]:
    """Runs the channel's agent, continuing the conversation for this thread."""
    agent = agent_for(t, row)
    arow = get_row(t, agent["id"])
    if not (can_use(p, arow) or can_edit(p, arow)):
        raise PermissionError("this assistant is not shared with you")
    cid = None
    if thread:
        prev = t.store.rows(channel_threads, tenant=t.id, channel_id=row["id"], thread=thread, user_id=p.user_id)
        if prev and time.time() - prev[0]["updated_at"] < 6 * 3600:
            cid = prev[0]["conversation_id"]
    r = rt.run_sync(rt.chat(t, p, box, agent, question[:4000], conversation_id=cid, trigger=row["kind"]))
    if thread and r.get("conversation_id"):
        t.store.delete_rows(channel_threads, tenant=t.id, channel_id=row["id"], thread=thread, user_id=p.user_id)
        t.store.insert(
            channel_threads,
            tenant=t.id,
            channel_id=row["id"],
            thread=thread,
            user_id=p.user_id,
            conversation_id=r["conversation_id"],
            updated_at=time.time(),
        )
    _used(t, row)
    return r


def approve(
    t: Any, box: Any, row: dict[str, Any], p: Principal, run_id: str, ok: bool, external_user: str | None
) -> dict[str, Any]:
    if external_user is not None:
        who = t.store.rows(channel_threads, tenant=t.id, channel_id=row["id"], thread=f"run:{run_id}")
        if not who or who[0]["user_id"] != external_user:
            raise PermissionError("only the person who asked can approve this")
    return rt.run_sync(rt.approve(t, p, box, run_id, ok))


def remember_asker(t: Any, row: dict[str, Any], run_id: str, external_user: str) -> None:
    t.store.insert(
        channel_threads,
        tenant=t.id,
        channel_id=row["id"],
        thread=f"run:{run_id}",
        user_id=external_user,
        conversation_id="-",
        updated_at=time.time(),
    )


def _used(t: Any, row: dict[str, Any]) -> None:
    t.store.update_row(
        channels, {"tenant": t.id, "id": row["id"]}, last_used=time.time(), calls=(row["calls"] or 0) + 1
    )


def reply_text(t: Any, row: dict[str, Any], r: dict[str, Any], base_url: str) -> str:
    """The answer as plain text for a chat app, after the external-channel checks."""
    cfg = json.loads(row["config"])
    link = f"{base_url.rstrip('/')}/#agents"
    if r.get("status") == "awaiting_approval":
        a = r["approval"]
        args = ", ".join(f"{k}={v}" for k, v in (a.get("arguments") or {}).items())
        return f"Approval needed: {a.get('tool')} wants to run {a.get('action')}{f' ({args})' if args else ''}. {a.get('description') or ''}".strip()
    marking = r.get("marking")
    limit = cfg.get("max_class", "internal")
    if marking and class_rank(marking) > class_rank(limit):
        t.audit.record(row["acts_as"], "channel_withheld", channel_id=row["id"], marking=marking)
        return f"This answer cites {marking} documents, so it is not shown in {row['kind'].title()}. Open it in Smriti: {link}"
    answer = r.get("answer") or ""
    if cfg.get("mask_pii", True):
        answer, cats = mask_text(answer)
        if cats:
            answer += "\n\n(Personal data is hidden here. Open Smriti to see it.)"
    cited = {c["n"]: c for c in r.get("citations") or []}
    if cited:
        lines = [
            f"[{n}] {c['title']}"
            + (f", {c['section']}" if c.get("section") else "")
            + (f" ({c['ref_no']})" if c.get("ref_no") else "")
            for n, c in sorted(cited.items())
        ]
        answer += "\n\nSources:\n" + "\n".join(lines)
    cap = LIMITS[row["kind"]]
    if len(answer) > cap:
        answer = answer[: cap - 80].rsplit(" ", 1)[0] + f" …\n\nFull answer in Smriti: {link}"
    return answer


# ---------------------------------------------------------------- widget


class RateLimit:
    def __init__(self, per_min: int = RATE) -> None:
        self.per_min = per_min
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= self.per_min:
                raise PermissionError("too many questions; wait a minute")
            q.append(now)


def check_origin(row: dict[str, Any], origin: str | None) -> str:
    cfg = json.loads(row["config"])
    o = (origin or "").rstrip("/")
    if o not in cfg.get("origins", []):
        raise PermissionError("this site is not allowed to use the widget")
    return o


# ---------------------------------------------------------------- Slack


def slack_verify(secret: str, ts: str | None, sig: str | None, body: bytes, now: float | None = None) -> None:
    if not ts or not sig or not ts.isdigit() or abs((now or time.time()) - int(ts)) > 300:
        raise PermissionError("stale or unsigned Slack request")
    good = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        raise PermissionError("bad Slack signature")


def slack_post(token: str, channel: str, text: str, thread_ts: str | None = None, run_id: str | None = None) -> None:
    body: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    if run_id:
        body["blocks"] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "approve",
                        "value": run_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Decline"},
                        "action_id": "decline",
                        "value": run_id,
                    },
                ],
            },
        ]
    HTTP.post(f"{API['slack']}/chat.postMessage", json=body, headers={"Authorization": f"Bearer {token}"})


MENTION = re.compile(r"<@[A-Z0-9]+>\s*")


# ---------------------------------------------------------------- Telegram


def telegram_send(token: str, chat_id: Any, text: str, run_id: str | None = None, reply_to: Any = None) -> None:
    body: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        body["reply_to_message_id"] = reply_to
    if run_id:
        body["reply_markup"] = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"ap:{run_id}:1"},
                    {"text": "Decline", "callback_data": f"ap:{run_id}:0"},
                ]
            ]
        }
    HTTP.post(f"{API['telegram']}/bot{token}/sendMessage", json=body)


# ---------------------------------------------------------------- Discord


def discord_verify(public_key_hex: str, sig: str | None, ts: str | None, body: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not sig or not ts:
        raise PermissionError("unsigned Discord request")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(bytes.fromhex(sig), ts.encode() + body)
    except (InvalidSignature, ValueError):
        raise PermissionError("bad Discord signature") from None


def discord_followup(application_id: str, token: str, text: str, run_id: str | None = None) -> None:
    body: dict[str, Any] = {"content": text}
    if run_id:
        body["components"] = [
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": f"ap:{run_id}:1"},
                    {"type": 2, "style": 4, "label": "Decline", "custom_id": f"ap:{run_id}:0"},
                ],
            }
        ]
    HTTP.patch(f"{API['discord']}/webhooks/{application_id}/{token}/messages/@original", json=body)


def discord_register(bot_token: str, application_id: str) -> dict[str, Any]:
    cmd = {
        "name": "ask",
        "description": "Ask the Smriti assistant. Answers cite their sources.",
        "options": [{"type": 3, "name": "question", "description": "Your question", "required": True}],
    }
    r = HTTP.post(
        f"{API['discord']}/applications/{application_id}/commands",
        json=cmd,
        headers={"Authorization": f"Bot {bot_token}"},
    )
    if r.status_code >= 300:
        raise ChannelError(f"Discord refused the command: {r.status_code} {r.text[:200]}")
    return {"registered": "/ask"}
