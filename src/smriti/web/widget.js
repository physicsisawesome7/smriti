/* Smriti chat widget. Add to any page whose origin the channel allows:

     <script src="https://smriti.example/widget.js" data-smriti-widget="wg_…"
             data-host="https://smriti.example" async></script>

   Optional attributes: data-user-token (for a signed widget: a token your
   server makes with the channel's signing key), data-position="left",
   data-open="true". JavaScript: SmritiWidget.open(), .close(), .ask(text),
   .setUserToken(token). Styles live in a shadow root, so the page's CSS
   cannot break the widget and the widget cannot leak styles onto the page. */
(() => {
  "use strict";
  const me = document.currentScript || document.querySelector("script[data-smriti-widget]");
  if (!me || window.SmritiWidget) return;
  const id = me.dataset.smritiWidget;
  const host = (me.dataset.host || new URL(me.src).origin).replace(/\/$/, "");
  let token = me.dataset.userToken || "";
  const left = me.dataset.position === "left";
  const store = (() => { try { return window.sessionStorage; } catch { return null; } })();
  const convKey = `smriti:${id}:conv`;
  let conv = store?.getItem(convKey) || null;
  let cfg = null;
  let busy = false;

  const root = document.createElement("div");
  root.setAttribute("data-smriti", "");
  const sh = root.attachShadow({ mode: "open" });
  sh.innerHTML = `<style>
    :host { all: initial; }
    * { box-sizing: border-box; font-family: Inter, system-ui, -apple-system, "Segoe UI", "Noto Sans", "Noto Sans Devanagari", sans-serif; }
    .fab { position: fixed; ${left ? "left" : "right"}: 20px; bottom: 20px; z-index: 2147483000; width: 56px; height: 56px; border-radius: 50%; border: 0; cursor: pointer;
      background: var(--c, #6D28D9); color: #fff; box-shadow: 0 8px 24px rgb(0 0 0 / .22); display: grid; place-items: center; }
    .fab svg { width: 26px; height: 26px; }
    .panel { position: fixed; ${left ? "left" : "right"}: 20px; bottom: 88px; z-index: 2147483000; width: min(400px, calc(100vw - 32px)); height: min(600px, calc(100vh - 120px));
      background: #fff; color: #18181B; border: 1px solid #E4E4E7; border-radius: 16px; box-shadow: 0 16px 48px rgb(0 0 0 / .2); display: flex; flex-direction: column; overflow: hidden; }
    .panel[hidden] { display: none; }
    header { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-bottom: 1px solid #E4E4E7; }
    header b { flex: 1; font-size: 15px; color: #0A0A0A; }
    header button { border: 0; background: none; font-size: 18px; cursor: pointer; color: #52525B; }
    .log { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 10px; font-size: 14px; line-height: 1.5; }
    .me { align-self: flex-end; background: var(--c, #6D28D9); color: #fff; padding: 8px 12px; border-radius: 14px 14px 4px 14px; max-width: 85%; white-space: pre-wrap; }
    .bot { align-self: flex-start; background: #F4F4F5; padding: 10px 12px; border-radius: 14px 14px 14px 4px; max-width: 92%; white-space: pre-wrap; }
    .bot.off { color: #52525B; }
    .cite { font-size: 11px; vertical-align: super; color: var(--c, #6D28D9); font-weight: 600; }
    .src { margin-top: 8px; padding-top: 8px; border-top: 1px solid #E4E4E7; font-size: 12px; color: #52525B; display: grid; gap: 4px; white-space: normal; }
    .src span b { color: var(--c, #6D28D9); }
    .steps { font-size: 12px; color: #71717A; }
    .ap { margin-top: 8px; display: flex; gap: 6px; }
    .ap button, .starters button { border: 1px solid #D4D4D8; background: #fff; border-radius: 99px; padding: 5px 11px; cursor: pointer; font-size: 12.5px; color: #18181B; }
    .ap button.y { background: var(--c, #6D28D9); color: #fff; border-color: var(--c, #6D28D9); }
    .starters { display: flex; flex-wrap: wrap; gap: 6px; }
    form { display: flex; gap: 8px; padding: 10px; border-top: 1px solid #E4E4E7; }
    textarea { flex: 1; resize: none; border: 1px solid #D4D4D8; border-radius: 10px; padding: 9px 10px; font-size: 14px; height: 42px; max-height: 120px; color: #18181B; }
    textarea:focus, button:focus-visible { outline: 2px solid var(--c, #6D28D9); outline-offset: 1px; }
    form button { border: 0; border-radius: 10px; padding: 0 14px; background: #0A0A0A; color: #fff; cursor: pointer; font-weight: 500; }
    form button:disabled { opacity: .5; }
    .foot { font-size: 10.5px; color: #71717A; padding: 0 12px 8px; }
  </style>
  <button class="fab" aria-label="Open the assistant" aria-expanded="false"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg></button>
  <section class="panel" role="dialog" aria-label="Assistant" hidden>
    <header><b class="title">Assistant</b><button class="new" title="New conversation" aria-label="New conversation">↺</button><button class="x" aria-label="Close">×</button></header>
    <div class="log" aria-live="polite"></div>
    <form><textarea placeholder="Ask a question…" aria-label="Your question"></textarea><button type="submit">Ask</button></form>
    <div class="foot"></div>
  </section>`;
  const $ = (s) => sh.querySelector(s);
  const fab = $(".fab"), panel = $(".panel"), log = $(".log"), form = $("form"), input = $("textarea");

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const fmt = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\[(\d+)\]/g, '<span class="cite">[$1]</span>');
  const headers = () => ({ "Content-Type": "application/json", ...(token ? { "X-Smriti-User-Token": token } : {}) });
  const add = (cls, html) => { const d = document.createElement("div"); d.className = cls; d.innerHTML = html; log.append(d); log.scrollTop = log.scrollHeight; return d; };

  async function load() {
    if (cfg) return cfg;
    const r = await fetch(`${host}/api/widget/${id}/config`, { method: "POST", headers: headers(), body: "{}" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `error ${r.status}`);
    cfg = await r.json();
    root.style.setProperty("--c", cfg.color || "#6D28D9");
    sh.host.style.setProperty("--c", cfg.color || "#6D28D9");
    $(".title").textContent = cfg.title;
    $(".foot").textContent = cfg.notice || "";
    greet();
    return cfg;
  }

  function greet() {
    log.innerHTML = "";
    add("bot", esc(cfg.greeting || "Ask me anything."));
    if (cfg.starters?.length) {
      const s = add("starters", cfg.starters.map((q, i) => `<button type="button" data-i="${i}">${esc(q)}</button>`).join(""));
      s.querySelectorAll("button").forEach((b) => (b.onclick = () => ask(cfg.starters[+b.dataset.i])));
    }
  }

  async function stream(path, body, bubble) {
    const r = await fetch(`${host}${path}`, { method: "POST", headers: headers(), body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `error ${r.status}`);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "", text = "", steps = 0;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        const ev = /^event: (\w+)/m.exec(chunk)?.[1], raw = /^data: (.*)$/m.exec(chunk)?.[1];
        if (!ev || raw === undefined) continue;
        const d = JSON.parse(raw);
        if (ev === "start" && d.conversation_id) { conv = d.conversation_id; store?.setItem(convKey, conv); }
        else if (ev === "step") { steps++; if (!text) bubble.innerHTML = `<span class="steps">Working… ${steps} step${steps > 1 ? "s" : ""}</span>`; }
        else if (ev === "token") { text += d; bubble.innerHTML = fmt(text); }
        else if (ev === "approval") return approval(bubble, d);
        else if (ev === "final") return final(bubble, d);
        else if (ev === "error") throw new Error(d.detail);
        log.scrollTop = log.scrollHeight;
      }
    }
  }

  function final(bubble, d) {
    bubble.classList.toggle("off", !!d.abstained);
    const cited = (d.citations || []).map((c) => `<span><b>[${c.n}]</b> ${esc(c.title)}${c.section ? `, ${esc(c.section)}` : ""}${c.ref_no ? ` (${esc(c.ref_no)})` : ""}</span>`).join("");
    bubble.innerHTML = fmt(d.answer) + (cited ? `<div class="src">${cited}</div>` : "");
    log.scrollTop = log.scrollHeight;
  }

  function approval(bubble, d) {
    const args = Object.entries(d.arguments || {}).map(([k, v]) => `${k}=${v}`).join(", ");
    bubble.innerHTML = `<b>Approval needed</b><br>${esc(d.tool)} wants to run <b>${esc(d.action)}</b>${args ? ` (${esc(args)})` : ""}.<br>${esc(d.description || "")}`
      + (cfg.signed ? `<div class="ap"><button class="y" type="button">Approve</button><button type="button">Decline</button></div>` : `<div class="steps">Open Smriti to approve this.</div>`);
    bubble.querySelectorAll(".ap button").forEach((b, i) => (b.onclick = async () => {
      bubble.querySelector(".ap").remove();
      const next = add("bot", `<span class="steps">${i === 0 ? "Approved. Running…" : "Declined."}</span>`);
      try { await stream(`/api/widget/${id}/approve/${encodeURIComponent(d.run_id)}`, { approve: i === 0 }, next); } catch (e) { next.textContent = e.message; }
    }));
  }

  async function ask(q) {
    q = String(q || "").trim();
    if (!q || busy) return;
    busy = true; form.querySelector("button").disabled = true;
    log.querySelector(".starters")?.remove();
    add("me", esc(q));
    const bubble = add("bot", `<span class="steps">Searching…</span>`);
    try { await load(); await stream(`/api/widget/${id}/chat`, { question: q, conversation_id: conv }, bubble); }
    catch (e) { bubble.classList.add("off"); bubble.textContent = e.message; }
    busy = false; form.querySelector("button").disabled = false; input.focus();
  }

  const open = async () => {
    panel.hidden = false; fab.setAttribute("aria-expanded", "true"); input.focus();
    try { await load(); } catch (e) { log.innerHTML = ""; add("bot off", esc(e.message)); }
  };
  const close = () => { panel.hidden = true; fab.setAttribute("aria-expanded", "false"); fab.focus(); };
  fab.onclick = () => (panel.hidden ? open() : close());
  $(".x").onclick = close;
  $(".new").onclick = () => { conv = null; store?.removeItem(convKey); if (cfg) greet(); };
  form.onsubmit = (e) => { e.preventDefault(); const q = input.value; input.value = ""; ask(q); };
  input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } };
  sh.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) close(); });

  (document.body ? Promise.resolve() : new Promise((r) => document.addEventListener("DOMContentLoaded", r))).then(() => {
    document.body.append(root);
    if (me.dataset.open === "true") open();
  });
  window.SmritiWidget = { open, close, ask: (q) => open().then(() => ask(q)), setUserToken: (t) => { token = t || ""; cfg = null; } };
})();
