"""HTTP endpoints for channels: management, the widget and the chat-app webhooks."""

from __future__ import annotations

import hmac
import json
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import channels as ch
from .agents import runtime as rt
from .agents.api import sse
from .models import Principal

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def mount(app: FastAPI, plat: Any, ctx: Callable[..., tuple[Any, Principal]], need: Callable[..., None]) -> None:
    box = plat.box
    limiter = ch.RateLimit()

    def base() -> str:
        return (os.environ.get("SMRITI_PUBLIC_URL") or plat.settings.public_base_url).rstrip("/")

    def errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from None
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    # ------------------------------------------------------------ management

    @app.get("/api/channels")
    def channels_list(c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        need(p, "view")
        return {
            "kinds": [{"kind": k, "description": v, "secrets": ch.SECRET_FIELDS[k]} for k, v in ch.KINDS.items()],
            "channels": [ch.public(r, base()) for r in ch.list_for(t, p)],
            "can_manage": p.can("curate") or p.can("administer"),
            "can_map_users": p.can("administer"),
        }

    @app.post("/api/channels")
    def channels_create(body: dict[str, Any], c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        return ch.public(errors(lambda: ch.save(t, p, box, body)), base())

    @app.put("/api/channels/{cid}")
    def channels_update(cid: str, body: dict[str, Any], c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        return ch.public(errors(lambda: ch.save(t, p, box, body, cid)), base())

    @app.delete("/api/channels/{cid}")
    def channels_delete(cid: str, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, bool]:
        t, p = c
        errors(lambda: ch.delete(t, p, cid))
        return {"ok": True}

    @app.get("/api/channels/{cid}/signing-key")
    def channels_signing_key(cid: str, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, str]:
        """For a signed widget: the key the host site's server signs user tokens with."""
        t, p = c
        row = errors(lambda: ch._row(t, cid))
        if not (row["created_by"] == p.user_id or p.can("administer")):
            raise HTTPException(403, "only the channel's creator or an administrator can see its key")
        t.audit.record(p.user_id, "channel_key_view", channel_id=cid)
        return {"signing_key": json.loads(row["config"]).get("signing_key", "")}

    @app.post("/api/channels/{cid}/register")
    def channels_register(cid: str, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        """Telegram: sets the bot's webhook. Discord: registers the /ask command."""
        t, p = c
        row = errors(lambda: ch._row(t, cid))
        if not (row["created_by"] == p.user_id or p.can("administer")):
            raise HTTPException(403, "only the channel's creator or an administrator can do this")
        cfg, sec = json.loads(row["config"]), box.open(row["secrets"])
        if row["kind"] == "telegram":
            r = ch.HTTP.post(
                f"{ch.API['telegram']}/bot{sec['bot_token']}/setWebhook",
                json={
                    "url": f"{base()}/api/channels/{cid}/telegram",
                    "secret_token": cfg["webhook_secret"],
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            if r.status_code >= 300 or not r.json().get("ok"):
                raise HTTPException(502, f"Telegram refused the webhook: {r.text[:200]}")
            return {"webhook": f"{base()}/api/channels/{cid}/telegram"}
        if row["kind"] == "discord":
            return errors(lambda: ch.discord_register(sec["bot_token"], cfg["application_id"]))
        raise HTTPException(400, "nothing to register for this kind of channel")

    # ------------------------------------------------------------ widget

    @app.get("/widget.js")
    def widget_js() -> FileResponse:
        return FileResponse(
            os.path.join(WEB_DIR, "widget.js"),
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
        )

    def cors(origin: str) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Smriti-User-Token",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    def page_origin(request: Request) -> str | None:
        """The Origin header, or the Referer's origin (same-origin GETs carry no Origin)."""
        if o := request.headers.get("origin"):
            return o
        ref = request.headers.get("referer") or ""
        m = re.match(r"(https?://[^/]+)", ref)
        return m.group(1) if m else None

    def widget(cid: str, origin: str | None) -> tuple[Any, dict[str, Any], str]:
        t, row = errors(lambda: ch.find(plat, cid))
        if row["kind"] != "widget":
            raise HTTPException(404, "no such widget")
        o = errors(lambda: ch.check_origin(row, origin))
        return t, row, o

    @app.options("/api/widget/{cid}/{rest:path}")
    def widget_preflight(cid: str, rest: str, origin: str | None = Header(default=None)) -> Response:
        _, _, o = widget(cid, origin)
        return Response(status_code=204, headers=cors(o))

    @app.api_route("/api/widget/{cid}/config", methods=["GET", "POST"])
    def widget_config(cid: str, request: Request) -> JSONResponse:
        t, row, o = widget(cid, page_origin(request))
        cfg = json.loads(row["config"])
        agent = errors(lambda: ch.agent_for(t, row))
        body = {
            "title": cfg.get("title") or agent["name"],
            "greeting": cfg.get("greeting"),
            "color": cfg.get("color"),
            "starters": cfg.get("starters") or agent.get("starters") or [],
            "signed": cfg["identity"] == "signed",
            "notice": rt.NOTICE,
        }
        return JSONResponse(body, headers=cors(o))

    @app.post("/api/widget/{cid}/chat")
    def widget_chat(
        cid: str,
        body: dict[str, Any],
        request: Request,
        x_smriti_user_token: str | None = Header(default=None),
    ) -> Response:
        t, row, o = widget(cid, page_origin(request))
        client = request.client.host if request.client else "?"
        errors(lambda: limiter.check(f"{cid}:{client}"))
        p = errors(lambda: ch.principal_for(t, row, token=x_smriti_user_token))
        agent = errors(lambda: ch.agent_for(t, row))
        q = str(body.get("question") or "").strip()[:4000]
        if not q:
            raise HTTPException(400, "question is required")
        conv = body.get("conversation_id")
        if conv:
            errors(lambda: rt.own_conversation(t, p, conv))
        ch._used(t, row)
        resp = sse(ch.widget_events(t, row, rt.chat(t, p, box, agent, q, conversation_id=conv, trigger="widget")))
        resp.headers.update(cors(o))
        return resp

    @app.post("/api/widget/{cid}/approve/{run_id}")
    def widget_approve(
        cid: str,
        run_id: str,
        body: dict[str, Any],
        request: Request,
        x_smriti_user_token: str | None = Header(default=None),
    ) -> Response:
        t, row, o = widget(cid, page_origin(request))
        if json.loads(row["config"])["identity"] != "signed":
            raise HTTPException(403, "approvals in the widget need signed-in users; open Smriti to approve")
        p = errors(lambda: ch.principal_for(t, row, token=x_smriti_user_token))
        resp = sse(ch.widget_events(t, row, rt.approve(t, p, box, run_id, bool(body.get("approve")))))
        resp.headers.update(cors(o))
        return resp

    # ------------------------------------------------------------ chat-app webhooks

    def bot(cid: str, kind: str) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
        t, row = errors(lambda: ch.find(plat, cid))
        if row["kind"] != kind:
            raise HTTPException(404, "no such channel")
        return t, row, json.loads(row["config"]), box.open(row["secrets"])

    def answer(t: Any, row: dict[str, Any], external_user: str, question: str, thread: str) -> tuple[str, str | None]:
        """Runs the agent and returns (text, run_id needing approval or None). Never raises."""
        try:
            p = ch.principal_for(t, row, external_user=external_user)
            r = ch.ask(t, box, row, p, question, thread)
        except PermissionError as e:
            return str(e), None
        except Exception as e:
            t.audit.record(row["acts_as"], "channel_error", channel_id=row["id"], error=str(e)[:300])
            return "Something went wrong answering that. Try again, or ask in Smriti.", None
        run_id = r.get("run_id") if r.get("status") == "awaiting_approval" else None
        if run_id:
            ch.remember_asker(t, row, run_id, external_user)
        return ch.reply_text(t, row, r, base()), run_id

    def decided(t: Any, row: dict[str, Any], external_user: str, run_id: str, ok: bool) -> tuple[str, str | None]:
        try:
            p = ch.principal_for(t, row, external_user=external_user)
            r = ch.approve(t, box, row, p, run_id, ok, external_user)
        except (PermissionError, ValueError, KeyError) as e:
            return str(e), None
        nxt = r.get("run_id") if r.get("status") == "awaiting_approval" else None
        if nxt:
            ch.remember_asker(t, row, nxt, external_user)
        return ch.reply_text(t, row, r, base()), nxt

    # Slack ---------------------------------------------------------

    @app.post("/api/channels/{cid}/slack")
    async def slack_events(cid: str, request: Request, tasks: BackgroundTasks) -> Any:
        t, row, _cfg, sec = bot(cid, "slack")
        raw = await request.body()
        errors(
            lambda: ch.slack_verify(
                sec["signing_secret"],
                request.headers.get("X-Slack-Request-Timestamp"),
                request.headers.get("X-Slack-Signature"),
                raw,
            )
        )
        if request.headers.get("X-Slack-Retry-Num"):
            return {"ok": True}  # we already took this one
        if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            payload = json.loads(parse_qs(raw.decode())["payload"][0])  # a button press
            act = (payload.get("actions") or [{}])[0]
            user, chan = payload["user"]["id"], payload["channel"]["id"]
            ts = (payload.get("message") or {}).get("thread_ts") or (payload.get("message") or {}).get("ts")

            def press() -> None:
                text, nxt = decided(t, row, user, act.get("value", ""), act.get("action_id") == "approve")
                ch.slack_post(sec["bot_token"], chan, text, ts, nxt)

            tasks.add_task(press)
            return {"ok": True}
        body = json.loads(raw)
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge")}
        ev = body.get("event") or {}
        if ev.get("bot_id") or ev.get("subtype") or ev.get("type") not in ("app_mention", "message"):
            return {"ok": True}
        if ev["type"] == "message" and ev.get("channel_type") != "im":
            return {"ok": True}
        q = ch.MENTION.sub("", ev.get("text") or "").strip()
        thread = ev.get("thread_ts") or ev.get("ts")

        def run() -> None:
            text, run_id = answer(t, row, ev.get("user", ""), q, f"{ev.get('channel')}:{thread}")
            ch.slack_post(sec["bot_token"], ev["channel"], text, thread, run_id)

        if q:
            tasks.add_task(run)
        return {"ok": True}

    # Telegram ------------------------------------------------------

    @app.post("/api/channels/{cid}/telegram")
    async def telegram_update(
        cid: str,
        request: Request,
        tasks: BackgroundTasks,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        t, row, cfg, sec = bot(cid, "telegram")
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token, cfg["webhook_secret"]
        ):
            raise HTTPException(403, "bad Telegram secret token")
        upd = await request.json()
        if cb := upd.get("callback_query"):
            m = re.fullmatch(r"ap:([A-Za-z0-9]+):([01])", cb.get("data") or "")
            chat_id = cb["message"]["chat"]["id"]
            if m:

                def press() -> None:
                    text, nxt = decided(t, row, str(cb["from"]["id"]), m.group(1), m.group(2) == "1")
                    ch.telegram_send(sec["bot_token"], chat_id, text, nxt)

                tasks.add_task(press)
            return {"ok": True}
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text or msg.get("from", {}).get("is_bot"):
            return {"ok": True}
        if text.startswith("/start"):
            tasks.add_task(
                ch.telegram_send,
                sec["bot_token"],
                msg["chat"]["id"],
                "Ask me a question. Every answer cites its source.",
            )
            return {"ok": True}
        text = re.sub(r"^/ask(@\w+)?\s*", "", text)

        def run() -> None:
            reply, run_id = answer(t, row, str(msg["from"]["id"]), text, f"tg:{msg['chat']['id']}")
            ch.telegram_send(sec["bot_token"], msg["chat"]["id"], reply, run_id, msg.get("message_id"))

        tasks.add_task(run)
        return {"ok": True}

    # Discord -------------------------------------------------------

    @app.post("/api/channels/{cid}/discord")
    async def discord_interaction(cid: str, request: Request, tasks: BackgroundTasks) -> Any:
        t, row, cfg, _sec = bot(cid, "discord")
        raw = await request.body()
        errors(
            lambda: ch.discord_verify(
                cfg["public_key"],
                request.headers.get("X-Signature-Ed25519"),
                request.headers.get("X-Signature-Timestamp"),
                raw,
            )
        )
        it = json.loads(raw)
        if it.get("type") == 1:
            return {"type": 1}  # PING
        user = str(((it.get("member") or {}).get("user") or it.get("user") or {}).get("id", ""))
        app_id, token = it.get("application_id") or cfg["application_id"], it.get("token", "")
        if it.get("type") == 2:  # /ask
            opts = {o["name"]: o.get("value") for o in (it.get("data") or {}).get("options", [])}
            q = str(opts.get("question") or "").strip()

            def run() -> None:
                text, run_id = answer(t, row, user, q, f"dc:{it.get('channel_id')}")
                ch.discord_followup(app_id, token, text, run_id)

            tasks.add_task(run)
            return {"type": 5}  # "thinking…", answered by the follow-up
        if it.get("type") == 3:  # a button
            m = re.fullmatch(r"ap:([A-Za-z0-9]+):([01])", (it.get("data") or {}).get("custom_id", ""))
            if m:

                def press() -> None:
                    text, nxt = decided(t, row, user, m.group(1), m.group(2) == "1")
                    ch.discord_followup(app_id, token, text, nxt)

                tasks.add_task(press)
            return {"type": 6}
        return {"type": 1}
