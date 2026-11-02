"""Channels: the widget and the Slack, Telegram and Discord bots."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from test_agents import PG, _fake_llm, _rest_tool, quote_first_passage

from smriti import channels

KM = {"X-Smriti-User": "km"}
SITE = "https://intranet.example.org"


class Outbox:
    """Captures what Smriti sends to Slack, Telegram and Discord."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.sent.append((request.method, str(request.url), body))
        if "setWebhook" in str(request.url):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True, "id": "1"})


@pytest.fixture
def outbox(monkeypatch):  # type: ignore[no-untyped-def]
    box = Outbox()
    monkeypatch.setattr(channels, "HTTP", httpx.Client(transport=httpx.MockTransport(box)))
    return box


def _desk(c, **kw):  # type: ignore[no-untyped-def]
    body = {"name": "Tender desk", "kind": "classic", "visibility": "tenant", "status": "published", **kw}
    r = c.post("/api/agents", json=body, headers=KM)
    assert r.status_code == 200, r.text
    return r.json()


def _channel(c, headers=KM, **body):  # type: ignore[no-untyped-def]
    return c.post("/api/channels", json=body, headers=headers)


def _events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        if block.startswith("event: "):
            ev, data = block.split("\n", 1)
            out.append((ev[7:], json.loads(data[6:])))
    return out


# ---------------------------------------------------------------- definitions


def test_channel_rules(fresh_app):  # type: ignore[no-untyped-def]
    c = TestClient(fresh_app)
    a = _desk(c)
    assert _channel(c, {"X-Smriti-User": "asha"}, kind="widget", agent_id=a["id"], origins=[SITE]).status_code == 403
    assert "origin" in _channel(c, kind="widget", agent_id=a["id"], origins=[]).text
    assert "origin like" in _channel(c, kind="widget", agent_id=a["id"], origins=["intranet"]).text
    assert "missing bot_token" in _channel(c, kind="telegram", agent_id=a["id"]).text
    mapped = _channel(c, kind="telegram", agent_id=a["id"], bot_token="t", identity="mapped", users={"1": "asha"})
    assert mapped.status_code == 403  # mapping identities is an administrator's job
    w = _channel(c, kind="widget", agent_id=a["id"], origins=[SITE]).json()
    assert w["id"].startswith("wg_") and 'data-smriti-widget="' + w["id"] in w["embed"]
    assert "signing_key" not in json.dumps(w)
    tg = _channel(c, kind="telegram", agent_id=a["id"], bot_token="123:SECRET").json()
    assert tg["webhook_url"].endswith(f"/api/channels/{tg['id']}/telegram") and "SECRET" not in json.dumps(tg)
    assert "webhook_secret" not in json.dumps(c.get("/api/channels", headers=KM).json())


# ---------------------------------------------------------------- widget


def test_widget_checks_origin_and_answers_with_citations(fresh_app):  # type: ignore[no-untyped-def]
    c = TestClient(fresh_app)
    a = _desk(c, starters=["Performance guarantee period?"])
    w = _channel(c, kind="widget", agent_id=a["id"], origins=[SITE], title="Ask the tender desk").json()
    assert c.get("/widget.js").headers["content-type"].startswith("text/javascript")
    assert c.get(f"/api/widget/{w['id']}/config", headers={"Origin": "https://evil.example"}).status_code == 403
    pre = c.options(f"/api/widget/{w['id']}/chat", headers={"Origin": SITE})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == SITE
    cfg = c.get(f"/api/widget/{w['id']}/config", headers={"Origin": SITE})
    assert cfg.json()["title"] == "Ask the tender desk" and cfg.json()["starters"] and not cfg.json()["signed"]
    r = c.post(f"/api/widget/{w['id']}/chat", json={"question": PG}, headers={"Origin": SITE})
    assert r.headers["access-control-allow-origin"] == SITE
    final = dict(_events(r.text))["final"]
    assert final["citations"] and not final["abstained"]
    off = c.put(f"/api/channels/{w['id']}", json={"enabled": False}, headers=KM)
    assert off.status_code == 200
    assert c.post(f"/api/widget/{w['id']}/chat", json={"question": PG}, headers={"Origin": SITE}).status_code == 403


def test_signed_widget_acts_as_the_signed_in_user(fresh_app):  # type: ignore[no-untyped-def]
    c = TestClient(fresh_app)
    a = _desk(c, name="Land desk", kind="classic")
    w = _channel(c, kind="widget", agent_id=a["id"], origins=[SITE], identity="signed").json()
    key = c.get(f"/api/channels/{w['id']}/signing-key", headers=KM).json()["signing_key"]
    assert c.get(f"/api/channels/{w['id']}/signing-key", headers={"X-Smriti-User": "asha"}).status_code == 403
    url = f"/api/widget/{w['id']}/chat"
    q = {"question": "Who owns plot P17-001 in the land schedule?"}
    assert c.post(url, json=q, headers={"Origin": SITE}).status_code == 403  # no token
    bad = channels.sign_user("wrong-key", "rakesh")
    assert c.post(url, json=q, headers={"Origin": SITE, "X-Smriti-User-Token": bad}).status_code == 403
    old = channels.sign_user(key, "rakesh", ttl_s=-10)
    assert "expired" in c.post(url, json=q, headers={"Origin": SITE, "X-Smriti-User-Token": old}).text

    def ask(user):  # type: ignore[no-untyped-def]
        r = c.post(url, json=q, headers={"Origin": SITE, "X-Smriti-User-Token": channels.sign_user(key, user)})
        return dict(_events(r.text))["final"]

    rakesh, asha = ask("rakesh"), ask("asha")  # rakesh has P-17 access, asha does not
    assert rakesh["citations"] and any("P17" in x["text"] for x in rakesh["citations"])
    assert asha["abstained"] or all("P17" not in x["text"] for x in asha["citations"])


# ---------------------------------------------------------------- Telegram


def test_telegram_bot_answers_and_checks_its_secret(fresh_app, outbox):  # type: ignore[no-untyped-def]
    c = TestClient(fresh_app)
    a = _desk(c)
    tg = _channel(c, kind="telegram", agent_id=a["id"], bot_token="123:ABC").json()
    plat = fresh_app.state.platform
    secret = json.loads(plat.tenant(None).store.rows(channels.channels, id=tg["id"])[0]["config"])["webhook_secret"]
    assert c.post(f"/api/channels/{tg['id']}/register", headers=KM).json()["webhook"].endswith("/telegram")
    assert outbox.sent[-1][2]["secret_token"] == secret
    upd = {"message": {"message_id": 7, "chat": {"id": 42}, "from": {"id": 9}, "text": PG}}
    assert (
        c.post(
            tg["webhook_url"].split("8000")[-1], json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "x"}
        ).status_code
        == 403
    )
    r = c.post(f"/api/channels/{tg['id']}/telegram", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert r.status_code == 200
    _method, url, body = outbox.sent[-1]
    assert url.endswith("/bot123:ABC/sendMessage") and body["chat_id"] == 42 and body["reply_to_message_id"] == 7
    assert "Sources:" in body["text"] and re.search(r"\[\d+\] ", body["text"])


def test_external_replies_mask_personal_data_and_withhold_confidential(tenant):  # type: ignore[no-untyped-def]
    row = {
        "id": "ch_x",
        "kind": "telegram",
        "acts_as": "km",
        "config": json.dumps({"mask_pii": True, "max_class": "internal"}),
    }
    r = {
        "answer": "The owner's Aadhaar is 2345 6789 0124 [1].",
        "citations": [{"n": 1, "title": "Land schedule", "section": "P-17"}],
    }
    text = channels.reply_text(tenant, row, r, "https://smriti.example")
    assert "2345 6789 0124" not in text and "[AADHAAR]" in text and "[1] Land schedule, P-17" in text
    secret = channels.reply_text(tenant, row, {**r, "marking": "confidential"}, "https://smriti.example")
    assert "2345" not in secret and "not shown in Telegram" in secret and "https://smriti.example" in secret
    long = channels.reply_text(tenant, row, {"answer": "word " * 3000, "citations": []}, "https://s")
    assert len(long) <= channels.LIMITS["telegram"] and "Full answer in Smriti" in long


def test_mapped_bot_refuses_unknown_people(fresh_app, outbox):  # type: ignore[no-untyped-def]
    c = TestClient(fresh_app)
    a = _desk(c)
    admin = {"X-Smriti-User": "admin"}
    tg = _channel(c, kind="telegram", agent_id=a["id"], bot_token="1:A").json()
    mapping = {"identity": "mapped", "users": {"5": "rakesh"}}
    assert c.put(f"/api/channels/{tg['id']}", json=mapping, headers=KM).status_code == 403  # curators can't map people
    nobody = c.put(f"/api/channels/{tg['id']}", json={"identity": "mapped", "users": {"5": "nobody"}}, headers=admin)
    assert nobody.status_code == 400 and "not in the directory" in nobody.text
    assert c.put(f"/api/channels/{tg['id']}", json=mapping, headers=admin).status_code == 200
    t = fresh_app.state.platform.tenant(None)
    secret = json.loads(t.store.rows(channels.channels, id=tg["id"])[0]["config"])["webhook_secret"]
    hdr = {"X-Telegram-Bot-Api-Secret-Token": secret}
    for who in (6, 5):
        upd = {"message": {"message_id": 1, "chat": {"id": who}, "from": {"id": who}, "text": PG}}
        c.post(f"/api/channels/{tg['id']}/telegram", json=upd, headers=hdr)
    stranger, known = outbox.sent[-2][2]["text"], outbox.sent[-1][2]["text"]
    assert "not set up" in stranger and "Sources:" in known


def test_only_the_asker_can_press_approve(fresh_app, outbox, monkeypatch):  # type: ignore[no-untyped-def]
    plat = fresh_app.state.platform
    t = plat.tenant(None)
    log: list = []
    tool = _rest_tool(t, plat, monkeypatch, log)
    fn = f"t{tool['id']}_create_ticket"
    _fake_llm(t, monkeypatch, [lambda b: {"calls": [(fn, {"title": "Signal fault"})]}, quote_first_passage])
    c = TestClient(fresh_app)
    a = _desk(c, name="Helpdesk", kind="agentic", tools=[tool["ref"]])
    tg = _channel(c, kind="telegram", agent_id=a["id"], bot_token="1:A").json()
    secret = json.loads(t.store.rows(channels.channels, id=tg["id"])[0]["config"])["webhook_secret"]
    hdr = {"X-Telegram-Bot-Api-Secret-Token": secret}
    url = f"/api/channels/{tg['id']}/telegram"
    c.post(
        url,
        json={
            "message": {
                "message_id": 1,
                "chat": {"id": 1},
                "from": {"id": 11},
                "text": "Open a ticket for the signal fault",
            }
        },
        headers=hdr,
    )
    ask = outbox.sent[-1][2]
    assert "Approval needed" in ask["text"] and not log
    data = ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    press = lambda who: {"callback_query": {"data": data, "from": {"id": who}, "message": {"chat": {"id": 1}}}}  # noqa: E731
    c.post(url, json=press(99), headers=hdr)
    assert "only the person who asked" in outbox.sent[-1][2]["text"] and not log
    c.post(url, json=press(11), headers=hdr)
    assert [m for m, _, _ in log] == ["POST"]


# ---------------------------------------------------------------- Slack


def _slack_sign(secret: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = ts or int(time.time())
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig, "Content-Type": "application/json"}


def test_slack_events_are_signed_and_answered_in_thread(fresh_app, outbox):  # type: ignore[no-untyped-def]
    c = TestClient(fresh_app)
    a = _desk(c)
    sl = _channel(c, kind="slack", agent_id=a["id"], bot_token="xoxb-1", signing_secret="shh").json()
    url = f"/api/channels/{sl['id']}/slack"
    chal = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    assert c.post(url, content=chal, headers=_slack_sign("wrong", chal)).status_code == 403
    assert c.post(url, content=chal, headers=_slack_sign("shh", chal, ts=int(time.time()) - 900)).status_code == 403
    assert c.post(url, content=chal, headers=_slack_sign("shh", chal)).json() == {"challenge": "abc"}
    ev = {
        "type": "event_callback",
        "event": {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "171.1", "text": f"<@UBOT> {PG}"},
    }
    raw = json.dumps(ev).encode()
    assert c.post(url, content=raw, headers=_slack_sign("shh", raw)).status_code == 200
    _method, out_url, body = outbox.sent[-1]
    assert out_url.endswith("/chat.postMessage") and body["channel"] == "C1" and body["thread_ts"] == "171.1"
    assert "Sources:" in body["text"]
    n = len(outbox.sent)
    retry = {**_slack_sign("shh", raw), "X-Slack-Retry-Num": "1"}
    c.post(url, content=raw, headers=retry)
    assert len(outbox.sent) == n  # retries are not answered twice


# ---------------------------------------------------------------- Discord


def test_discord_interactions_are_verified(fresh_app, outbox):  # type: ignore[no-untyped-def]
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    c = TestClient(fresh_app)
    a = _desk(c)
    dc = _channel(c, kind="discord", agent_id=a["id"], bot_token="B", public_key=pub, application_id="777").json()
    url = f"/api/channels/{dc['id']}/discord"

    def signed(body: dict) -> tuple[bytes, dict[str, str]]:
        raw, ts = json.dumps(body).encode(), str(int(time.time()))
        return raw, {
            "X-Signature-Ed25519": key.sign(ts.encode() + raw).hex(),
            "X-Signature-Timestamp": ts,
            "Content-Type": "application/json",
        }

    raw, h = signed({"type": 1})
    assert c.post(url, content=raw, headers={**h, "X-Signature-Ed25519": "00" * 64}).status_code == 403
    assert c.post(url, content=raw, headers=h).json() == {"type": 1}
    cmd = {
        "type": 2,
        "application_id": "777",
        "token": "tok",
        "channel_id": "9",
        "member": {"user": {"id": "42"}},
        "data": {"name": "ask", "options": [{"name": "question", "value": PG}]},
    }
    raw, h = signed(cmd)
    assert c.post(url, content=raw, headers=h).json() == {"type": 5}
    method, out_url, body = outbox.sent[-1]
    assert (
        method == "PATCH" and out_url.endswith("/webhooks/777/tok/messages/@original") and "Sources:" in body["content"]
    )
    assert c.post(f"/api/channels/{dc['id']}/register", headers=KM).json() == {"registered": "/ask"}
