/* Integrations: put an agent in a web page (widget), Slack, Telegram or Discord.
   The "Integrations" tab in Agents. Loaded before agents.js; uses app.js helpers at call time. */
"use strict";

const CH_IC = { widget: "◱", slack: "#", telegram: "✈", discord: "◆" };
const CH_LABEL = { widget: "Website widget", slack: "Slack", telegram: "Telegram", discord: "Discord" };
const CH_STEPS = {
  widget: [
    "Add the sites that may show the widget under Allowed origins, for example your intranet.",
    "Paste the embed code before </body> on those pages.",
    "For staff portals, choose Signed-in users: your server signs a short-lived token for each user, so the widget sees only what that person may see.",
  ],
  slack: [
    "Create an app at api.slack.com/apps. Under OAuth & Permissions add the bot scopes app_mentions:read, chat:write and im:history, then install it to your workspace.",
    "Paste the Bot User OAuth Token (xoxb-…) and the Signing Secret (Basic Information) here and save.",
    "Under Event Subscriptions, set the Request URL to the webhook URL below and subscribe to app_mention and message.im.",
    "Under Interactivity, set the same URL followed by nothing else, so the Approve and Decline buttons work.",
  ],
  telegram: [
    "Talk to @BotFather, send /newbot and copy the token it gives you.",
    "Paste the token here and save.",
    "Press Connect webhook. Smriti tells Telegram where to send messages, with a secret only Telegram and Smriti know.",
  ],
  discord: [
    "Create an application at discord.com/developers. Copy the Application ID and Public Key from General Information.",
    "Under Bot, create a token. Paste all three here and save.",
    "Set the Interactions Endpoint URL to the webhook URL below, then press Register /ask.",
    "Invite the app to your server with the applications.commands scope.",
  ],
};

async function tabChannels(box) {
  let d;
  try { d = await api("/api/channels"); } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const agentsList = await api("/api/agents");
  const name = (id) => agentsList.find((a) => a.id === id)?.name || id;
  const card = (c) => `<div class="conn">
      <div class="ch"><span class="ki">${CH_IC[c.kind]}</span><div style="flex:1;min-width:0"><div class="cn">${esc(c.name)}</div>
        <div class="cs">${esc(CH_LABEL[c.kind])} · ${esc(name(c.agent_id))} · ${c.config.identity === "fixed" ? `answers as ${esc(c.acts_as)}` : c.config.identity === "signed" ? "signed-in users" : `${Object.keys(c.config.users || {}).length} mapped people`} · ${c.calls} calls</div></div>
        ${c.enabled ? pill("on", "ok") : pill("off", "neutral")}</div>
      <div class="chips">${c.kind !== "widget" && c.config.mask_pii ? `<span class="chip v">personal data masked</span>` : ""}<span class="chip">withholds above ${esc(c.config.max_class)}</span>${(c.config.origins || []).map((o) => `<span class="chip">${esc(o)}</span>`).join("")}</div>
      <div class="row"><button class="btn ghost sm" data-setup="${esc(c.id)}">Setup</button><button class="btn ghost sm" data-toggle="${esc(c.id)}">${c.enabled ? "Turn off" : "Turn on"}</button><button class="btn ghost sm" data-del="${esc(c.id)}">Delete</button></div>
    </div>`;
  box.innerHTML = `
    <p class="muted" style="margin-top:0">Put a published agent where people already work. Every answer still cites its sources and acts as a known person. Answers sent to chat apps have personal data masked, and answers citing confidential documents stay in Smriti.</p>
    <div class="row" style="margin-bottom:12px"><h2 style="margin:0">Your integrations</h2><span class="spacer"></span></div>
    ${d.channels.length ? `<div class="conns">${d.channels.map(card).join("")}</div>` : `<div class="empty">No integrations yet.</div>`}
    ${d.can_manage ? `<h2 style="margin-top:26px">Add an integration</h2><div class="kinds">${d.kinds.map((k) => `<button class="kind" data-new="${k.kind}"><div class="ki">${CH_IC[k.kind]}</div><div class="kl">${esc(CH_LABEL[k.kind])}</div><div class="kd">${esc(k.description)}</div></button>`).join("")}</div>`
      : `<p class="muted">Adding integrations needs the curate or administer grant.</p>`}`;
  $$("[data-new]", box).forEach((b) => (b.onclick = () => channelForm(box, d, agentsList, { kind: b.dataset.new, config: {} })));
  $$("[data-setup]", box).forEach((b) => (b.onclick = () => channelSetup(d.channels.find((c) => c.id === b.dataset.setup))));
  $$("[data-toggle]", box).forEach((b) => (b.onclick = async () => {
    const c = d.channels.find((x) => x.id === b.dataset.toggle);
    try { await api(`/api/channels/${c.id}`, { method: "PUT", json: { enabled: !c.enabled } }); } catch (e) { toast(e.message, 6000); }
    tabChannels(box);
  }));
  $$("[data-del]", box).forEach((b) => (b.onclick = async () => {
    if (!confirm("Delete this integration? The widget or bot stops answering at once.")) return;
    await api(`/api/channels/${b.dataset.del}`, { method: "DELETE" }); toast("Integration deleted"); tabChannels(box);
  }));
}

function channelForm(box, d, agentsList, c) {
  const kind = c.kind;
  const usable = agentsList.filter((a) => a.can_edit && a.status === "published");
  const secretFields = d.kinds.find((k) => k.kind === kind).secrets;
  const label = { bot_token: kind === "slack" ? "Bot token (xoxb-…)" : "Bot token", signing_secret: "Signing secret" };
  box.innerHTML = `
    <div class="row"><button class="btn ghost sm" id="chBack">← Integrations</button></div>
    <h2 style="margin-top:12px">${CH_IC[kind]} New ${esc(CH_LABEL[kind])} integration</h2>
    <div class="grid2"><form id="chForm">
      <fieldset class="fg"><legend>Agent</legend><div class="formgrid">
        <label>Published agent *<select name="agent_id" required>${usable.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</select>
          ${usable.length ? "" : `<span class="fhelp">Publish an agent you own first.</span>`}</label>
        <label>Name<input type="text" name="name" placeholder="e.g. Intranet help bubble"></label></div></fieldset>
      ${kind === "widget" ? `
      <fieldset class="fg"><legend>Website</legend><div class="formgrid">
        <label class="full">Allowed origins (one per line) *<textarea name="origins" rows="2" class="mono">${esc(location.origin)}</textarea><span class="fhelp">Only pages on these sites can talk to the widget. Smriti's own address is filled in so you can try it here.</span></label>
        <label>Title<input type="text" name="title" placeholder="Ask the tender desk"></label>
        <label>Colour<input type="text" name="color" value="#6D28D9" class="mono"></label>
        <label class="full">Greeting<input type="text" name="greeting" value="Ask me anything. Every answer cites its source."></label>
        <label class="full">Who it answers as<select name="identity"><option value="fixed">Me (${esc(S.me.user.name)}): visitors see only what I can see</option><option value="signed">Signed-in users: my site's server signs a token per user</option></select></label></div></fieldset>`
      : `
      <fieldset class="fg"><legend>Connection</legend><div class="formgrid">
        ${secretFields.map((f) => `<label>${esc(label[f] || f)} *<input type="password" name="${f}" autocomplete="off" required></label>`).join("")}
        ${kind === "discord" ? `<label>Application ID *<input type="text" name="application_id" class="mono" required></label><label class="full">Public key *<input type="text" name="public_key" class="mono" required></label>` : ""}
        <span class="fhelp full">Tokens are encrypted at rest and never shown again.</span></div></fieldset>
      <fieldset class="fg"><legend>Who it answers as</legend><div class="formgrid">
        <label class="full"><select name="identity"><option value="fixed">Me (${esc(S.me.user.name)}): everyone in the chat sees only what I can see</option>${d.can_map_users ? `<option value="mapped">Mapped people: each chat user answers as their own Smriti user</option>` : ""}</select></label>
        ${d.can_map_users ? `<label class="full" id="mapBox" hidden>Mapping (one per line: chat user id = Smriti user id)<textarea name="users" rows="3" class="mono" placeholder="U02ABCDEF = asha"></textarea><span class="fhelp">People not in the list are refused.</span></label>` : ""}</div></fieldset>`}
      <fieldset class="fg"><legend>Safety</legend><div class="formgrid">
        <label class="full"><span><input type="checkbox" name="mask_pii" ${kind === "widget" ? "" : "checked"}> Mask personal data in answers${kind === "widget" ? "" : " (recommended: the chat app's servers store every message)"}</span></label>
        <label>Withhold answers citing documents above<select name="max_class"><option>public</option><option selected>internal</option><option>confidential</option></select></label></div></fieldset>
      <div class="row"><button class="btn accent" type="submit">Create</button></div>
    </form>
    <div class="card"><h2 style="margin-top:0">Setup</h2><ol class="steps-list">${CH_STEPS[kind].map((s) => `<li>${esc(s)}</li>`).join("")}</ol></div></div>`;
  const form = $("#chForm");
  $("#chBack").onclick = () => tabChannels(box);
  if (form.elements.identity && $("#mapBox")) form.elements.identity.onchange = () => ($("#mapBox").hidden = form.elements.identity.value !== "mapped");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const f = form.elements;
    const body = { kind, agent_id: f.agent_id.value, name: f.name.value.trim(), identity: f.identity.value, mask_pii: f.mask_pii.checked, max_class: f.max_class.value };
    if (kind === "widget") Object.assign(body, { origins: f.origins.value.split("\n").map((s) => s.trim()).filter(Boolean), title: f.title.value.trim() || undefined, color: f.color.value.trim(), greeting: f.greeting.value });
    else {
      for (const s of secretFields) body[s] = f[s].value;
      if (kind === "discord") Object.assign(body, { application_id: f.application_id.value.trim(), public_key: f.public_key.value.trim() });
      if (f.users) body.users = Object.fromEntries(f.users.value.split("\n").map((l) => l.split("=").map((x) => x.trim())).filter((p) => p.length === 2 && p[0] && p[1]));
    }
    try { const made = await api("/api/channels", { method: "POST", json: body }); toast("Integration created"); await tabChannels(box); channelSetup(made); }
    catch (err) { toast(err.message, 7000); }
  };
}

async function channelSetup(c) {
  const signNode = `import crypto from "node:crypto";\n// On your server, for the signed-in user:\nconst exp = Math.floor(Date.now() / 1000) + 3600;\nconst sig = crypto.createHmac("sha256", SIGNING_KEY).update(\`\${userId}.\${exp}\`).digest("hex");\nconst token = \`\${userId}.\${exp}.\${sig}\`;  // put it in data-user-token`;
  const signPy = `import hashlib, hmac, time\nexp = int(time.time()) + 3600\nsig = hmac.new(SIGNING_KEY.encode(), f"{user_id}.{exp}".encode(), hashlib.sha256).hexdigest()\ntoken = f"{user_id}.{exp}.{sig}"  # put it in data-user-token`;
  $("#drawerBody").innerHTML = `
    <div class="row"><div><div class="eyebrow">${esc(CH_LABEL[c.kind])}</div><h2 style="margin:4px 0 0">${esc(c.name)}</h2></div><span class="spacer"></span><button class="btn ghost sm" id="dClose">Close</button></div>
    <ol class="steps-list">${CH_STEPS[c.kind].map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
    ${c.embed ? `<div class="eyebrow" style="margin-top:12px">Embed code</div><pre class="json" id="embedCode">${esc(c.embed)}</pre>
      <div class="row"><button class="btn ghost sm" id="copyEmbed">Copy</button>${(c.config.origins || []).includes(location.origin) && c.config.identity === "fixed" ? `<button class="btn accent sm" id="tryWidget">Try it on this page</button>` : ""}</div>` : ""}
    ${c.config.identity === "signed" ? `<div class="eyebrow" style="margin-top:12px">Signing users</div><p class="muted">Keep the key on your server. Tokens should expire within an hour.</p>
      <button class="btn ghost sm" id="showKey">Show signing key</button><pre class="json" id="keyBox" hidden></pre><pre class="json">${esc(signNode)}</pre><pre class="json">${esc(signPy)}</pre>` : ""}
    ${c.webhook_url ? `<div class="eyebrow" style="margin-top:12px">Webhook URL</div><pre class="json">${esc(c.webhook_url)}</pre>
      ${c.kind === "telegram" ? `<button class="btn accent sm" id="register">Connect webhook</button>` : c.kind === "discord" ? `<button class="btn accent sm" id="register">Register /ask</button>` : ""}
      <p class="fhelp">Smriti must be reachable from the internet at this address (set SMRITI_PUBLIC_URL). On a closed network, use the widget instead.</p>` : ""}`;
  $("#drawer").hidden = false;
  $("#dClose").onclick = () => ($("#drawer").hidden = true);
  const copy = $("#copyEmbed"); if (copy) copy.onclick = () => navigator.clipboard?.writeText(c.embed).then(() => toast("Copied"));
  const tryW = $("#tryWidget");
  if (tryW) tryW.onclick = () => {
    document.querySelector("[data-smriti]")?.remove(); delete window.SmritiWidget;
    const s = document.createElement("script");
    s.src = "/widget.js"; s.dataset.smritiWidget = c.id; s.dataset.host = location.origin; s.dataset.open = "true";
    document.body.append(s); $("#drawer").hidden = true;
  };
  const key = $("#showKey");
  if (key) key.onclick = async () => { const r = await api(`/api/channels/${c.id}/signing-key`); $("#keyBox").textContent = r.signing_key; $("#keyBox").hidden = false; };
  const reg = $("#register");
  if (reg) reg.onclick = async () => { try { await api(`/api/channels/${c.id}/register`, { method: "POST" }); toast("Done"); } catch (e) { toast(e.message, 7000); } };
}
