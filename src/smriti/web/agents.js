/* Agents: the list, the builder with a live preview, chat with steps and approvals,
   tools and API keys. Loaded before app.js; uses its helpers at call time. */
"use strict";

const AG = { tab: "agents", cat: null, editing: null, chat: null, conv: null, convs: [], turns: [], sel: null, toolKind: null, toolEdit: null, shared: null };
const KIND_IC = { classic: "◎", agentic: "⚙", research: "☰", workflow: "⑂" };
const KIND_LABEL = { classic: "Classic", agentic: "Agentic", research: "Research", workflow: "Workflow" };

/* The agent inventory comes from the server (smriti/agents/inventory.py), grouped by purpose. */
const agentTemplates = () => AG.cat?.agent_templates || [];

/* ------------------------------------------------------------ helpers */

async function streamSSE(path, body, onEvent) {
  const headers = { "Content-Type": "application/json" };
  if (S.user) headers["X-Smriti-User"] = S.user;
  const r = await fetch(path, { method: "POST", headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (_) {} throw new Error(`${r.status}: ${m}`); }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let k;
    while ((k = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, k); buf = buf.slice(k + 2);
      const ev = (raw.match(/^event: (.*)$/m) || [])[1];
      const data = JSON.parse((raw.match(/^data: (.*)$/m) || [])[1] || "null");
      if (ev === "error") throw new Error(data.detail);
      onEvent(ev, data);
    }
  }
}

async function loadCatalog(force) {
  if (!AG.cat || force) AG.cat = await api("/api/agents/catalog");
  return AG.cat;
}

function toolInfo(ref) {
  const c = AG.cat;
  if (!c) return { label: ref, icon: "◇" };
  const b = c.tools.builtin.find((t) => t.kind === ref);
  if (b) return b;
  const t = c.configured.find((x) => x.ref === ref);
  return t ? { label: t.name, icon: t.icon } : { label: ref, icon: "◇" };
}

/* ------------------------------------------------------------ router */

async function viewAgents() {
  await loadCatalog();
  if (AG.shared) return viewShared();
  if (AG.tab === "chat" && AG.chat) return viewAgentChat();
  if (AG.tab === "build") return viewBuilder();
  const tabs = [["agents", "Agents"], ["tools", "Tools"], ["channels", "Integrations"]];
  $("#view").innerHTML = `
    <div class="eyebrow">Agents</div>
    <h1>Agents that cite their work</h1>
    <p class="lede">Build assistants with their own instructions, sources and tools. Each one acts as the person asking, so it only sees what they may see. Every answer is checked sentence by sentence against what the agent's tools returned, and anything that changes data waits for your approval.</p>
    <div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${AG.tab === k ? "on" : ""}">${esc(l)}</button>`).join("")}</div>
    <div id="agBody"></div>`;
  $$(".tabs button").forEach((b) => (b.onclick = () => { AG.tab = b.dataset.tab; viewAgents(); }));
  await ({ agents: tabAgentList, tools: tabTools, channels: tabChannels }[AG.tab] || tabAgentList)($("#agBody"));
}

/* ------------------------------------------------------------ list */

async function tabAgentList(box) {
  const list = await api("/api/agents");
  const card = (a) => `<div class="conn agent">
      <div class="ch"><span class="ki">${KIND_IC[a.kind] || "◇"}</span><div style="flex:1;min-width:0"><div class="cn">${esc(a.name)}</div>
        <div class="cs">${esc(KIND_LABEL[a.kind])} · ${a.visibility === "private" ? "only you" : a.visibility === "tenant" ? "everyone" : "roles: " + esc((a.roles || []).join(", "))} · by ${esc(a.owner)}</div></div>
        ${pill(a.status, a.status === "published" ? "ok" : "neutral")}</div>
      ${a.description ? `<div class="muted" style="font-size:13px">${esc(a.description)}</div>` : ""}
      <div class="chips">${(a.tools || []).map((r) => { const t = toolInfo(r); return `<span class="chip">${esc(t.icon)} ${esc(t.label)}</span>`; }).join("")}${a.schedule ? `<span class="chip v">⏱ ${esc(a.schedule)}</span>` : ""}</div>
      <div class="row"><button class="btn accent sm" data-chat="${esc(a.id)}">Chat</button>
        ${a.can_edit ? `<button class="btn ghost sm" data-edit="${esc(a.id)}">Edit</button><button class="btn ghost sm" data-keys="${esc(a.id)}">API keys</button>` : ""}</div>
    </div>`;
  box.innerHTML = `
    <div class="row" style="margin-bottom:12px"><h2 style="margin:0">Your agents</h2><span class="spacer"></span><button class="btn accent" id="newAgent">+ New agent</button></div>
    ${list.length ? `<div class="conns">${list.map(card).join("")}</div>` : `<div class="empty">No agents yet. Start from a template below or build one from scratch.</div>`}
    <h2 style="margin-top:26px">Start from the inventory</h2>
    <p class="muted" style="margin-top:-4px">Ready-made agents for common desks. Each opens in the builder as your own draft; every one runs offline with built-in tools.</p>
    ${Object.entries(AG.cat?.categories || {}).map(([cat, label]) => {
      const items = agentTemplates().map((t, i) => [t, i]).filter(([t]) => t.category === cat);
      return items.length ? `<h3 class="invcat">${esc(label)}</h3><div class="kinds">${items.map(([t, i]) => `<button class="kind" data-tpl="${i}"><div class="ki">${KIND_IC[t.kind]}</div><div class="kl">${esc(t.name)}</div><div class="kd">${esc(t.description)}</div><div class="kd mono" style="margin-top:6px">${esc(KIND_LABEL[t.kind])} · ${t.tools.map((r) => toolInfo(r).label).join(", ")}</div></button>`).join("")}</div>` : "";
    }).join("")}
    <h2 style="margin-top:26px">Start from a workflow</h2>
    <p class="muted" style="margin-top:-4px">Chain agents, conditions, code and human sign-off into one run. Every step still cites its sources and acts as the person asking.</p>
    <div class="kinds">${(AG.cat.workflow?.templates || []).map((t, i) => `<button class="kind" data-wtpl="${i}"><div class="ki">${KIND_IC.workflow}</div><div class="kl">${esc(t.name)}</div><div class="kd">${esc(t.description)}</div><div class="kd mono" style="margin-top:6px">${t.graph.nodes.filter((n) => n.type !== "note").map((n) => WF_IC[n.type]).join(" → ")}</div></button>`).join("")}</div>`;
  $("#newAgent").onclick = () => openBuilder({ name: "", kind: "classic", tools: ["search"] });
  $$("[data-tpl]", box).forEach((b) => (b.onclick = () => openBuilder((({ id, category, category_label, ...t }) => t)(JSON.parse(JSON.stringify(agentTemplates()[+b.dataset.tpl]))))));
  $$("[data-wtpl]", box).forEach((b) => (b.onclick = () => {
    const t = AG.cat.workflow.templates[+b.dataset.wtpl];
    openBuilder({ name: t.name, description: t.description, kind: "workflow", graph: JSON.parse(JSON.stringify(t.graph)), tools: [] });
  }));
  $$("[data-chat]", box).forEach((b) => (b.onclick = () => openChat(list.find((a) => a.id === b.dataset.chat))));
  $$("[data-edit]", box).forEach((b) => (b.onclick = () => openBuilder(list.find((a) => a.id === b.dataset.edit))));
  $$("[data-keys]", box).forEach((b) => (b.onclick = () => openKeys(list.find((a) => a.id === b.dataset.keys))));
}

/* ------------------------------------------------------------ builder */

function openBuilder(agent) { AG.editing = agent; AG.tab = "build"; AG.preview = []; viewAgents(); }

function viewBuilder() {
  const a = AG.editing || {};
  const c = AG.cat;
  const src = a.sources || { mode: "all", doc_types: [], connectors: [] };
  const tools = new Set(a.tools || ["search"]);
  const toolRow = (t, ref, extra) => `<label class="toolpick ${t.available === false ? "off" : ""}"><input type="checkbox" name="tool" value="${esc(ref)}" ${tools.has(ref) ? "checked" : ""} ${t.available === false && !tools.has(ref) ? "disabled" : ""}>
      <span class="ti">${esc(t.icon)}</span><span><b>${esc(t.label || t.name)}</b><span class="fhelp">${esc(t.description || extra || "")}${t.available === false ? ` · off: ${esc(t.why_unavailable)}` : ""}${t.external ? " · leaves this deployment (query is PII-masked)" : ""}</span></span></label>`;
  $("#view").innerHTML = `
    <div class="row"><button class="btn ghost sm" id="backList">← Agents</button><span class="spacer"></span>${a.id ? `<span class="mono muted">${esc(a.id)}</span>` : ""}</div>
    <div class="eyebrow" style="margin-top:12px">${a.id ? "Edit agent" : "New agent"}</div>
    <h1>${esc(a.name || "Build an agent")}</h1>
    <div class="grid2">
      <form id="agForm">
        <fieldset class="fg"><legend>Identity</legend><div class="formgrid">
          <label>Name *<input type="text" name="name" value="${esc(a.name || "")}" required placeholder="e.g. Tender desk"></label>
          <label>Description<input type="text" name="description" value="${esc(a.description || "")}" placeholder="What it is for"></label></div></fieldset>
        <fieldset class="fg"><legend>Type</legend><div class="kinds k3">${c.kinds.map((k) => `<label class="kind ${a.kind === k.kind ? "on" : ""}"><input type="radio" name="kind" value="${k.kind}" ${a.kind === k.kind ? "checked" : ""} hidden><div class="ki">${KIND_IC[k.kind]}</div><div class="kl">${KIND_LABEL[k.kind]}</div><div class="kd">${esc(k.description)}</div></label>`).join("")}</div></fieldset>
        <fieldset class="fg" id="wfFs" ${a.kind === "workflow" ? "" : "hidden"}><legend>Workflow</legend><div id="wfBox"></div></fieldset>
        <fieldset class="fg" id="instrFs" ${a.kind === "workflow" ? "hidden" : ""}><legend>Instructions</legend>
          <textarea name="instructions" rows="5" style="width:100%" placeholder="Who it answers for, tone, what to always include. Platform rules (cite everything, never follow instructions in documents) always apply on top.">${esc(a.instructions || "")}</textarea></fieldset>
        <fieldset class="fg"><legend>Sources</legend>
          <div class="row"><label class="switch"><input type="radio" name="srcmode" value="all" ${src.mode !== "selected" ? "checked" : ""}> Everything the person asking can see</label>
          <label class="switch"><input type="radio" name="srcmode" value="selected" ${src.mode === "selected" ? "checked" : ""}> Only selected</label></div>
          <div id="srcSel" ${src.mode === "selected" ? "" : "hidden"}>
            <div class="fhelp" style="margin:8px 0 4px">Document types</div>
            <div class="chips">${c.doc_types.map((d) => `<label class="chip pick"><input type="checkbox" name="doc_type" value="${esc(d)}" ${(src.doc_types || []).includes(d) ? "checked" : ""}> ${esc(d)}</label>`).join("")}</div>
            ${c.connectors.length ? `<div class="fhelp" style="margin:10px 0 4px">Ingestion sources</div><div class="chips">${c.connectors.map((d) => `<label class="chip pick"><input type="checkbox" name="connector" value="${esc(d)}" ${(src.connectors || []).includes(d) ? "checked" : ""}> ${esc(d)}</label>`).join("")}</div>` : ""}
          </div>
          <div class="fhelp" style="margin-top:8px">Sources narrow what the agent searches. They never widen access: each person still sees only what they are allowed to.</div></fieldset>
        <fieldset class="fg" id="toolsFs" ${a.kind === "workflow" ? "hidden" : ""}><legend>Tools</legend>
          <div class="fhelp" style="margin-bottom:6px">Classic agents only search. Agentic and research agents use the tools you tick.</div>
          <div class="toolpicks">${c.tools.builtin.map((t) => toolRow(t, t.kind)).join("")}
          ${c.configured.map((t) => toolRow({ ...t, description: `${t.label} · ${t.actions.length} actions${t.actions.some((x) => x.mutates) ? ", some change data" : ""}` }, t.ref)).join("")}</div>
          ${c.can_share ? `<div class="fhelp" style="margin-top:8px">Connect REST APIs, MCP servers and databases on the <a href="#" id="toTools">Tools</a> tab.</div>` : ""}</fieldset>
        <fieldset class="fg"><legend>Behaviour</legend><div class="formgrid">
          <label>Approval before actions<select name="approval">
            <option value="mutating" ${a.approval !== "always" && a.approval !== "never" ? "selected" : ""}>Ask before anything that changes data</option>
            <option value="always" ${a.approval === "always" ? "selected" : ""}>Ask before every tool call</option>
            <option value="never" ${a.approval === "never" ? "selected" : ""}>Don't ask (still asks after reading outside content)</option></select></label>
          <label>Max tool steps<input type="number" name="max_steps" min="1" max="12" value="${esc(a.max_steps || 6)}"></label>
          <label class="full">Starter questions (one per line)<textarea name="starters" rows="3">${esc((a.starters || []).join("\n"))}</textarea></label>
          <label class="full"><span><input type="checkbox" name="require_citations" ${a.require_citations === false ? "" : "checked"}> Only answer when the answer is supported by cited results</span></label></div></fieldset>
        <fieldset class="fg"><legend>Sharing and schedule</legend><div class="formgrid">
          <label>Who can use it<select name="visibility" ${c.can_share ? "" : "disabled"}>
            <option value="private" ${!a.visibility || a.visibility === "private" ? "selected" : ""}>Only me</option>
            <option value="tenant" ${a.visibility === "tenant" ? "selected" : ""}>Everyone in ${esc(S.me.tenant.name)}</option>
            <option value="roles" ${a.visibility === "roles" ? "selected" : ""}>Selected roles</option></select>
            ${c.can_share ? "" : `<span class="fhelp">Sharing needs the curate or administer grant.</span>`}</label>
          <label>Status<select name="status"><option value="draft" ${a.status !== "published" ? "selected" : ""}>Draft</option><option value="published" ${a.status === "published" ? "selected" : ""}>Published</option></select></label>
          <div class="full chips" id="roleSel" ${a.visibility === "roles" ? "" : "hidden"}>${c.roles.map((r) => `<label class="chip pick"><input type="checkbox" name="role" value="${esc(r)}" ${(a.roles || []).includes(r) ? "checked" : ""}> ${esc(r)}</label>`).join("")}</div>
          <label>Schedule (cron)<input type="text" name="schedule" value="${esc(a.schedule || "")}" placeholder="0 9 * * 1-5" class="mono"></label>
          <label>Scheduled question<input type="text" name="schedule_prompt" value="${esc(a.schedule_prompt || "")}" placeholder="Summarise circulars published this week"></label>
          <span class="fhelp full">Scheduled runs act as you and land in a conversation called "Scheduled: ${esc(a.name || "…")}". Only published agents run on schedule.</span></div></fieldset>
        <div class="row"><button type="submit" class="btn">Save</button><button type="button" class="btn accent" id="saveChat">Save &amp; chat</button><span class="spacer"></span>
          ${a.id && a.can_edit !== false ? `<button type="button" class="btn ghost" id="delAgent">Delete</button>` : ""}</div>
      </form>
      <div class="stack" id="pvCol">
        <div class="card"><div class="row"><h2 style="margin:0">Try it</h2><span class="spacer"></span><span class="mono muted">${esc(c.model)}</span></div>
          <p class="muted" style="margin:6px 0 10px">Runs the agent as it is in the form, unsaved, as ${esc(S.me.user.name)}.</p>
          <form class="askbox" id="pvForm"><textarea id="pvQ" rows="2" placeholder="Ask something to test the agent…"></textarea><div class="row"><span class="spacer"></span><button class="btn accent" id="pvBtn" type="submit">Run</button></div></form>
          <div class="suggest" id="pvStarters" style="margin-top:8px"></div></div>
        <div class="stack" id="pvOut"></div>
      </div>
    </div>`;
  const form = $("#agForm");
  $("#backList").onclick = () => { AG.tab = "agents"; viewAgents(); };
  const toTools = $("#toTools"); if (toTools) toTools.onclick = (e) => { e.preventDefault(); AG.tab = "tools"; viewAgents(); };
  let wfEd = null;
  const syncKind = () => {
    const isWf = (form.elements.kind.value || a.kind) === "workflow";
    $("#wfFs").hidden = !isWf; $("#instrFs").hidden = isWf; $("#toolsFs").hidden = isWf;
    if (isWf && !wfEd) wfEd = workflowEditor($("#wfBox"), a.graph, c);
  };
  $$("input[name=kind]", form).forEach((r) => (r.onchange = () => { $$(".kinds.k3 .kind", form).forEach((k) => k.classList.toggle("on", k.querySelector("input").checked)); syncKind(); }));
  syncKind();
  $$("input[name=srcmode]", form).forEach((r) => (r.onchange = () => ($("#srcSel").hidden = form.elements.srcmode.value !== "selected")));
  form.elements.visibility.onchange = () => ($("#roleSel").hidden = form.elements.visibility.value !== "roles");
  const starters = () => form.elements.starters.value.split("\n").map((s) => s.trim()).filter(Boolean);
  const drawStarters = () => { $("#pvStarters").innerHTML = starters().map((s, i) => `<button type="button" data-i="${i}">${esc(s.length > 70 ? s.slice(0, 68) + "…" : s)}</button>`).join(""); $$("#pvStarters button").forEach((b) => (b.onclick = () => { $("#pvQ").value = starters()[+b.dataset.i]; runPreview(); })); };
  form.elements.starters.oninput = drawStarters; drawStarters();
  const collect = () => {
    const f = form.elements;
    const checked = (n) => $$(`input[name=${n}]:checked`, form).map((x) => x.value);
    return {
      name: f.name.value.trim(), description: f.description.value.trim(), kind: f.kind.value || "classic", instructions: f.instructions.value,
      sources: { mode: f.srcmode.value, doc_types: checked("doc_type"), connectors: checked("connector"), doc_ids: [] },
      tools: checked("tool"), approval: f.approval.value, max_steps: +f.max_steps.value || 6, starters: starters(),
      require_citations: f.require_citations.checked, visibility: f.visibility.value, roles: checked("role"), status: f.status.value,
      schedule: f.schedule.value.trim(), schedule_prompt: f.schedule_prompt.value.trim(),
      ...(f.kind.value === "workflow" && wfEd ? { graph: wfEd.get(), tools: [] } : {}),
    };
  };
  const save = async (chat) => {
    const body = collect();
    if (!body.name) return toast("Give the agent a name");
    try {
      const saved = a.id ? await api(`/api/agents/${a.id}`, { method: "PUT", json: body }) : await api("/api/agents", { method: "POST", json: body });
      toast("Agent saved");
      if (chat) return openChat(saved);
      AG.editing = { ...saved, can_edit: true }; viewBuilder();
    } catch (e) { toast(e.message, 6000); }
  };
  form.onsubmit = (e) => { e.preventDefault(); save(false); };
  $("#saveChat").onclick = () => save(true);
  const del = $("#delAgent");
  if (del) del.onclick = async () => { if (!confirm("Delete this agent? Its API keys stop working. Conversations stay.")) return; await api(`/api/agents/${a.id}`, { method: "DELETE" }); toast("Agent deleted"); AG.tab = "agents"; viewAgents(); };
  const runPreview = async () => {
    const q = $("#pvQ").value.trim();
    if (!q) return;
    const turn = { q, steps: [], partial: "", res: null };
    AG.preview = [turn];
    const out = $("#pvOut");
    const draw = () => { out.innerHTML = turnHtml(turn, 0, true); bindTurn(out, [turn], true); };
    draw();
    $("#pvBtn").disabled = true;
    try {
      await streamSSE("/api/agents/preview", { agent: collect(), question: q }, (ev, d) => handleEvent(turn, ev, d, draw));
    } catch (e) { turn.res = errorRes(e); draw(); }
    $("#pvBtn").disabled = false;
  };
  $("#pvForm").onsubmit = (e) => { e.preventDefault(); runPreview(); };
  $("#pvQ").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); runPreview(); } };
}

function errorRes(e) { return { answer: e.message, abstained: true, steps: [], citations: [], passages: [], verification: {}, guardrails: [], notice: "" }; }

/* ------------------------------------------------------------ turns (shared by chat and preview) */

function handleEvent(turn, ev, d, draw) {
  if (ev === "start") { turn.run_id = d.run_id; if (d.conversation_id) AG.conv = d.conversation_id; }
  else if (ev === "step") { turn.steps.push(d); turn.approval = null; draw(); }
  else if (ev === "token") { turn.partial += d; const el = document.querySelector(`[data-turn="${turn.key}"] .answer`); if (el) el.innerHTML = renderAnswer(turn.partial); else draw(); }
  else if (ev === "approval") { turn.approval = d; draw(); }
  else if (ev === "final") { turn.res = d; turn.steps = d.steps || turn.steps; turn.approval = null; draw(); }
}

function stepHtml(s) {
  if (s.type === "node") {
    const cls = s.status === "error" || s.status === "declined" ? "b" : s.status === "abstained" || s.status === "waiting" ? "w" : "";
    return `<div class="s wf-step"><span class="dot ${cls}"></span><div><div>${WF_IC[s.node_type] || "•"} <b>${esc(s.title)}</b> ${s.status && s.status !== "done" ? pill(s.status, cls === "b" ? "bad" : s.status === "approved" ? "ok" : "warn") : ""}</div>
      <div class="muted" style="font-size:12.5px">${esc(s.summary || "")}</div></div></div>`;
  }
  const inner = s.node_title ? `<span class="chip">${esc(s.node_title)}</span> ` : "";
  const ic = s.type === "plan" ? "☰" : s.type === "retrieve" ? "◎" : s.type === "section" ? "§" : s.icon || "⚙";
  const cls = s.status === "error" || s.status === "declined" ? "b" : s.status === "abstained" ? "w" : "";
  const title = s.type === "tool" ? `${esc(s.tool)} · <span class="mono">${esc(s.action)}</span>` : s.type === "section" ? `Sub-question` : s.type === "plan" ? "Plan" : "Retrieve";
  const args = s.arguments && Object.keys(s.arguments).length ? `<pre class="json args">${esc(JSON.stringify(s.arguments, null, 1))}</pre>` : "";
  const items = s.items ? `<ol class="plan">${s.items.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : "";
  const files = (s.artifacts || []).map((f) => `<a class="chip v" href="/api/artifacts/${esc(f.id)}?as=${encodeURIComponent(S.user)}">⇩ ${esc(f.filename)}</a>`).join(" ");
  return `<div class="s ${s.node_title ? "wf-inner" : ""}"><span class="dot ${cls}"></span><div><div>${inner}${ic} ${title} ${s.status && s.status !== "ok" ? pill(s.status, cls === "b" ? "bad" : "warn") : ""}${s.approved ? " " + pill("approved", "violet") : ""}${s.ms ? ` <span class="mono muted">${s.ms} ms</span>` : ""}${s.cites?.length ? ` <span class="mono muted">→ [${s.cites.join(",")}]</span>` : ""}</div>
    <div class="muted" style="font-size:12.5px">${esc(s.summary || "")}</div>${items}${args}${files}</div></div>`;
}

function turnHtml(t, i, preview) {
  t.key = t.key || Math.random().toString(36).slice(2);
  const r = t.res;
  let banner = "";
  if (r?.blocked) banner = `<div class="banner block">Blocked by the input guardrail (${esc(r.blocked)}).</div>`;
  else if (r?.abstained) banner = `<div class="banner abst">The agent's sources and tools don't support an answer, so it says so instead of guessing.</div>`;
  if (["confidential", "secret"].includes(r?.marking)) banner += `<div class="banner mark">Contains ${esc(r.marking)} material</div>`;
  if (r?.withheld) banner = `<div class="banner block">Withheld: this answer cites documents you cannot open.</div>`;
  const ans = r ? r.answer : t.partial;
  const steps = t.steps || [];
  const ap = t.approval;
  const files = (r?.artifacts || []).map((f) => `<a class="btn ghost sm" href="/api/artifacts/${esc(f.id)}?as=${encodeURIComponent(S.user)}">⇩ ${esc(f.filename)} <span class="mono muted">${fmtBytes(f.size)}</span></a>`).join(" ");
  return `<div class="turn" data-turn="${t.key}" data-i="${i}">
    <div class="q">${esc(t.q)}</div>
    ${steps.length ? `<details class="tl" ${r ? "" : "open"}><summary>${steps.length} step${steps.length > 1 ? "s" : ""}${r ? "" : ' <span class="spin"></span>'}</summary><div class="steps">${steps.map(stepHtml).join("")}</div></details>` : ""}
    ${ap ? `<div class="approve"><div class="eyebrow">Approval needed</div><div><b>${esc(ap.tool)}</b> wants to run <span class="mono">${esc(ap.action)}</span>: ${esc(ap.description)}</div>
      <pre class="json args">${esc(JSON.stringify(ap.arguments, null, 1))}</pre><div class="muted" style="font-size:12.5px">Why you're asked: ${esc(ap.reason)}.</div>
      <div class="row" style="margin-top:8px"><button class="btn accent sm" data-approve="1">Approve and run</button><button class="btn ghost sm" data-approve="0">Decline</button></div></div>` : ""}
    ${banner}
    ${ans || !ap ? `<div class="answer ${r?.abstained ? "abstain" : ""}">${ans ? renderAnswer(ans).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>") : '<span class="spin"></span>'}</div>` : ""}
    ${files ? `<div class="row" style="margin-top:8px">${files}</div>` : ""}
    ${r && !r.withheld ? `<div class="notice">${pill(KIND_LABEL[r.agent?.kind] || "agent", "violet")} ${r.verification?.faithfulness !== undefined ? pill("faithfulness " + r.verification.faithfulness, r.verification.faithfulness >= 0.9 ? "ok" : "warn") : ""}
      <span class="mono">${esc(r.model || "")}</span><span class="mono">${r.timings_ms ? r.timings_ms.total + " ms" : ""}</span><span class="spacer"></span>
      ${(r.passages || []).length ? `<button class="btn ghost sm" data-ev="${i}">Evidence</button>` : ""}</div>${r.notice ? `<div class="notice" style="border:0;padding:0;margin-top:4px">${esc(r.notice)}</div>` : ""}` : ""}
  </div>`;
}

function bindTurn(box, turns, preview) {
  $$(".turn", box).forEach((el) => {
    const t = turns[+el.dataset.i];
    if (!t) return;
    $$(".cite", el).forEach((c) => (c.onclick = () => showEvidence(t, +c.dataset.n, preview)));
    const ev = $("[data-ev]", el); if (ev) ev.onclick = () => showEvidence(t, null, preview);
    $$("[data-approve]", el).forEach((b) => (b.onclick = () => decide(t, b.dataset.approve === "1", () => { box.innerHTML = turns.map((x, j) => turnHtml(x, j, preview)).join(""); bindTurn(box, turns, preview); })));
  });
}

async function decide(t, ok, draw) {
  const id = t.approval.run_id;
  t.approval = null; draw();
  try { await streamSSE(`/api/runs/${id}/approve?approve=${ok}`, undefined, (ev, d) => { if (ev !== "start") handleEvent(t, ev, d, draw); }); }
  catch (e) { t.res = errorRes(e); draw(); }
  if (AG.tab === "chat") refreshConvs();
}

function showEvidence(t, n, preview) {
  if (!t.res) return;
  if (preview || !$("#evidence")) {
    $("#drawerBody").innerHTML = `<div class="row"><h2 style="margin:0">Evidence</h2><span class="spacer"></span><button class="btn ghost sm" id="dClose">Close</button></div><div class="stack" id="evidence" style="margin-top:12px"></div>`;
    $("#drawer").hidden = false; $("#dClose").onclick = () => ($("#drawer").hidden = true);
  }
  renderEvidence({ res: t.res, passages: t.res.passages }, n || undefined);
}

/* ------------------------------------------------------------ chat */

function openChat(agent) { AG.chat = agent; AG.tab = "chat"; AG.conv = null; AG.turns = []; viewAgents(); }

async function refreshConvs() {
  AG.convs = await api(`/api/conversations?agent_id=${encodeURIComponent(AG.chat.id)}`);
  const box = $("#convList");
  if (!box) return;
  box.innerHTML = `<button class="btn ghost sm" id="newConv" style="width:100%">+ New chat</button>` + AG.convs.map((c) => `<a class="convi ${c.id === AG.conv ? "on" : ""}" data-c="${esc(c.id)}">${esc(c.title)}<span class="mono muted">${fmtTime(c.updated_at)}${c.shared ? " · shared" : ""}</span></a>`).join("");
  $("#newConv").onclick = () => { AG.conv = null; AG.turns = []; drawChat(); refreshConvs(); };
  $$("[data-c]", box).forEach((a) => (a.onclick = () => loadConv(a.dataset.c)));
}

async function loadConv(id) {
  const c = await api(`/api/conversations/${id}`);
  AG.conv = id;
  AG.turns = [];
  for (let i = 0; i < c.messages.length; i++) {
    const m = c.messages[i];
    if (m.role === "user") AG.turns.push({ q: m.content, steps: [], partial: "", res: null });
    else if (AG.turns.length) { const t = AG.turns[AG.turns.length - 1]; t.res = { ...m, answer: m.content, passages: m.citations || [], agent: { kind: AG.chat.kind } }; t.steps = m.steps || []; }
  }
  drawChat(); refreshConvs();
}

async function viewAgentChat() {
  const a = AG.chat;
  $("#view").innerHTML = `
    <div class="row"><button class="btn ghost sm" id="backList">← Agents</button><span class="spacer"></span>
      <button class="btn ghost sm" id="shareConv">Share</button>${a.can_edit ? `<button class="btn ghost sm" id="editAgent">Edit agent</button>` : ""}</div>
    <div class="agchat">
      <aside class="convs" id="convList"></aside>
      <div class="stack">
        <div><div class="eyebrow">${KIND_IC[a.kind]} ${esc(KIND_LABEL[a.kind])} agent</div><h1 style="margin-bottom:4px">${esc(a.name)}</h1>
          <p class="lede" style="margin-top:0">${esc(a.description || "")}</p>
          <div class="chips">${(a.tools || []).map((r) => { const t = toolInfo(r); return `<span class="chip">${esc(t.icon)} ${esc(t.label)}</span>`; }).join("")}</div></div>
        <form class="askbox" id="chatForm"><textarea id="cq" rows="2" placeholder="Ask ${esc(a.name)}…"></textarea>
          <div class="row"><span class="mono muted" style="font-size:11.5px">acting as ${esc(S.me.user.name)}</span><span class="spacer"></span><button class="btn accent" id="cBtn" type="submit">Send</button></div></form>
        <div class="suggest">${(a.starters || []).map((s, i) => `<button type="button" data-st="${i}">${esc(s.length > 80 ? s.slice(0, 78) + "…" : s)}</button>`).join("")}</div>
        <div class="stack" id="cturns"></div>
      </div>
      <div class="stack" id="evidence"><div class="empty">Steps, evidence and verification appear here.</div></div>
    </div>`;
  $("#backList").onclick = () => { AG.tab = "agents"; AG.chat = null; viewAgents(); };
  const ed = $("#editAgent"); if (ed) ed.onclick = () => openBuilder(a);
  $("#shareConv").onclick = shareConv;
  $("#chatForm").onsubmit = (e) => { e.preventDefault(); sendChat(); };
  $("#cq").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); } };
  $$("[data-st]").forEach((b) => (b.onclick = () => { $("#cq").value = a.starters[+b.dataset.st]; sendChat(); }));
  drawChat();
  refreshConvs();
}

function drawChat() {
  const box = $("#cturns");
  if (!box) return;
  const order = AG.turns.map((t, i) => [t, i]).reverse();
  box.innerHTML = order.map(([t, i]) => turnHtml(t, i, false)).join("") || `<div class="empty">Ask a question to start.</div>`;
  bindTurn(box, AG.turns, false);
}

async function sendChat() {
  const q = $("#cq").value.trim();
  if (!q) return;
  const turn = { q, steps: [], partial: "", res: null };
  AG.turns.push(turn);
  $("#cq").value = "";
  $("#cBtn").disabled = true;
  drawChat();
  try {
    await streamSSE(`/api/agents/${encodeURIComponent(AG.chat.id)}/chat`, { question: q, conversation_id: AG.conv, stream: true }, (ev, d) => handleEvent(turn, ev, d, drawChat));
    if (turn.res) renderEvidence({ res: turn.res, passages: turn.res.passages });
  } catch (e) { turn.res = errorRes(e); drawChat(); }
  $("#cBtn").disabled = false;
  refreshConvs();
}

async function shareConv() {
  if (!AG.conv) return toast("Start a conversation first");
  const r = await api(`/api/conversations/${AG.conv}/share`, { method: "POST" });
  const url = location.origin + r.path;
  try { await navigator.clipboard.writeText(url); } catch (_) {}
  toast("Link copied. People who open it see only answers whose sources they can open.", 5000);
  refreshConvs();
}

async function viewShared() {
  const token = AG.shared;
  try {
    const c = await api(`/api/shared/${encodeURIComponent(token)}`);
    const turns = [];
    for (const m of c.messages) {
      if (m.role === "user") turns.push({ q: m.content, steps: [], res: null });
      else if (turns.length) Object.assign(turns[turns.length - 1], { res: { ...m, answer: m.content, passages: m.citations || [] }, steps: m.steps || [] });
    }
    $("#view").innerHTML = `<div class="eyebrow">Shared conversation</div><h1>${esc(c.title)}</h1>
      <p class="lede">Shared by ${esc(c.owner)}. Read-only. ${c.withheld ? `${c.withheld} answer(s) are withheld because they cite documents you cannot open.` : "You can open every source cited here."}</p>
      <div class="row" style="margin-bottom:12px"><button class="btn ghost sm" id="leaveShared">Close</button></div><div class="stack" id="shTurns"></div>`;
    $("#shTurns").innerHTML = turns.map((t, i) => turnHtml(t, i, true)).join("");
    bindTurn($("#shTurns"), turns, true);
  } catch (e) { $("#view").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  $("#leaveShared") && ($("#leaveShared").onclick = () => { AG.shared = null; history.replaceState(null, "", "/"); viewAgents(); });
}

/* ------------------------------------------------------------ API keys */

async function openKeys(agent) {
  const draw = async (fresh) => {
    const keys = await api(`/api/agents/${encodeURIComponent(agent.id)}/keys`);
    const origin = location.origin;
    const k = fresh ? fresh.key : "sk-smriti-…";
    $("#drawerBody").innerHTML = `
      <div class="row"><div><div class="eyebrow">API keys</div><h2 style="margin:4px 0 0">${esc(agent.name)}</h2></div><span class="spacer"></span><button class="btn ghost sm" id="dClose">Close</button></div>
      <p class="muted">A key works only for this agent and acts as you: it never reads more than you can. It is shown once.</p>
      ${fresh ? `<div class="approve"><div class="eyebrow">New key: copy it now</div><pre class="json">${esc(fresh.key)}</pre></div>` : ""}
      <form class="row" id="keyForm" style="margin:12px 0"><input type="text" name="n" placeholder="Key name, e.g. intranet bot" style="flex:1"><button class="btn accent sm">Create key</button></form>
      <div class="tablewrap"><table class="t"><thead><tr><th>Name</th><th>Key</th><th>Acts as</th><th>Calls</th><th>Last used</th><th></th></tr></thead><tbody>
        ${keys.map((x) => `<tr><td>${esc(x.name)}</td><td class="mono">${esc(x.prefix)}…</td><td>${esc(x.acts_as)}</td><td class="mono">${x.calls}</td><td>${fmtTime(x.last_used)}</td><td>${x.revoked ? pill("revoked", "neutral") : `<button class="btn ghost sm" data-rev="${x.id}">Revoke</button>`}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">No keys yet.</td></tr>`}
      </tbody></table></div>
      <h3 style="margin-top:20px">Call it</h3>
      <pre class="json">curl ${esc(origin)}/api/agents/${esc(agent.id)}/chat \\
  -H "Authorization: Bearer ${esc(k)}" -H "Content-Type: application/json" \\
  -d '{"question": "What is the LD ceiling under clause 46.1?"}'</pre>
      <p class="muted" style="margin:12px 0 6px">Or with any OpenAI client:</p>
      <pre class="json">from openai import OpenAI
client = OpenAI(base_url="${esc(origin)}/v1", api_key="${esc(k)}")
r = client.chat.completions.create(model="agent/${esc(agent.id)}",
    messages=[{"role": "user", "content": "What is the LD ceiling under clause 46.1?"}])
print(r.choices[0].message.content)</pre>`;
    $("#dClose").onclick = () => ($("#drawer").hidden = true);
    $("#keyForm").onsubmit = async (e) => { e.preventDefault(); const r = await api(`/api/agents/${encodeURIComponent(agent.id)}/keys`, { method: "POST", json: { name: e.target.elements.n.value || "key" } }); draw(r); };
    $$("[data-rev]").forEach((b) => (b.onclick = async () => { if (!confirm("Revoke this key? Anything using it stops working.")) return; await api(`/api/keys/${b.dataset.rev}`, { method: "DELETE" }); draw(); }));
  };
  $("#drawer").hidden = false;
  $("#drawerBody").innerHTML = `<span class="spin"></span>`;
  try { await draw(); } catch (e) { $("#drawerBody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* ------------------------------------------------------------ tools */

async function tabTools(box) {
  const d = await api("/api/tools");
  if (d.can_manage && !AG.toolPresets) AG.toolPresets = await api("/api/tools/presets");
  if (AG.toolKind) return toolForm(box, d);
  box.innerHTML = `
    <h2>Built in</h2>
    <div class="conns">${d.catalog.builtin.map((t) => `<div class="conn"><div class="ch"><span class="ki">${esc(t.icon)}</span><div style="flex:1"><div class="cn">${esc(t.label)}</div><div class="cs">${esc(t.kind)}</div></div>${t.available ? pill("ready", "ok") : pill("off", "neutral")}</div>
      <div class="muted" style="font-size:13px">${esc(t.description)}</div>${t.available ? "" : `<div class="fhelp">${esc(t.why_unavailable)}</div>`}</div>`).join("")}</div>
    <div class="row" style="margin:26px 0 12px"><h2 style="margin:0">Connected</h2><span class="spacer"></span>${d.can_manage ? "" : `<span class="muted">Connecting tools needs the curate or administer grant.</span>`}</div>
    ${d.configured.length ? `<div class="conns">${d.configured.map((t) => `<div class="conn"><div class="ch"><span class="ki">${esc(t.icon)}</span><div style="flex:1;min-width:0"><div class="cn">${esc(t.name)}</div><div class="cs">${esc(t.label)} · ${t.actions.length} actions${t.config.allowed_roles ? " · roles: " + esc(t.config.allowed_roles) : ""}</div></div></div>
      <div class="chips">${t.actions.slice(0, 10).map((x) => `<span class="chip ${x.mutates ? "w" : ""}" title="${esc(x.description)}">${x.mutates ? "✎ " : ""}${esc(x.name)}</span>`).join("")}${t.actions.length > 10 ? `<span class="chip">+${t.actions.length - 10}</span>` : ""}</div>
      ${d.can_manage ? `<div class="row"><button class="btn ghost sm" data-tedit="${t.id}">Edit</button><span class="spacer"></span><button class="btn ghost sm" data-tdel="${t.id}">Remove</button></div>` : ""}</div>`).join("")}</div>` : `<div class="empty">No connected tools yet.</div>`}
    ${d.can_manage ? `<h2 style="margin-top:26px">Connect a tool</h2><div class="kinds">${d.catalog.configurable.map((t) => `<button class="kind" data-tk="${esc(t.kind)}"><div class="ki">${esc(t.icon)}</div><div class="kl">${esc(t.label)}</div><div class="kd">${esc(t.description)}</div></button>`).join("")}</div>
      <h2 style="margin-top:22px">Or try a demo tool</h2><p class="muted" style="margin-top:0">Built-in targets served by this Smriti instance.</p>
      <div class="row">${(AG.toolPresets || []).map((p, i) => `<button class="btn ghost sm" data-tp="${i}">${esc(p.name)}</button>`).join("")}</div>` : ""}`;
  $$("[data-tk]", box).forEach((b) => (b.onclick = () => { AG.toolKind = b.dataset.tk; AG.toolEdit = null; AG.toolPreset = null; tabTools(box); }));
  $$("[data-tp]", box).forEach((b) => (b.onclick = () => { AG.toolPreset = AG.toolPresets[+b.dataset.tp]; AG.toolKind = AG.toolPreset.kind; AG.toolEdit = null; tabTools(box); }));
  $$("[data-tedit]", box).forEach((b) => (b.onclick = () => { AG.toolEdit = d.configured.find((t) => t.id === +b.dataset.tedit); AG.toolKind = AG.toolEdit.kind; tabTools(box); }));
  $$("[data-tdel]", box).forEach((b) => (b.onclick = async () => { if (!confirm("Remove this tool? Agents that use it lose it.")) return; await api(`/api/tools/${b.dataset.tdel}`, { method: "DELETE" }); AG.cat = null; await loadCatalog(); tabTools(box); }));
}

function toolForm(box, d) {
  const type = d.catalog.configurable.find((t) => t.kind === AG.toolKind);
  const ed = AG.toolEdit;
  const preset = !ed && AG.toolPreset && AG.toolPreset.kind === AG.toolKind ? AG.toolPreset : null;
  const values = ed ? ed.config : preset ? preset.values : {};
  const groups = {};
  type.fields.forEach((f) => (groups[f.group] = groups[f.group] || []).push(f));
  box.innerHTML = `
    <div class="row"><h2 style="margin:0">${esc(type.icon)} ${ed ? "Edit" : "Connect"} ${esc(type.label)}</h2><span class="spacer"></span><button class="btn ghost sm" id="tBack">← Tools</button></div>
    <p class="muted">${esc(type.description)} Saving reads the tool's actions${AG.toolKind === "rest_api" || AG.toolKind === "mcp" ? " from the server" : ""}, so you can see exactly what agents will be able to call.</p>
    <div class="grid2"><form id="tForm">
      <fieldset class="fg"><legend>Name</legend><input type="text" name="__name" value="${esc(ed ? ed.name : preset ? preset.name : "")}" placeholder="e.g. Works ERP" style="width:100%" required></fieldset>
      ${Object.entries(groups).map(([g, fs]) => `<fieldset class="fg"><legend>${esc(g)}</legend><div class="formgrid">${fs.map((f) => fieldHtml(f, values, ed)).join("")}</div></fieldset>`).join("")}
      <div class="row"><span class="spacer"></span><button type="submit" class="btn accent">${ed ? "Save and re-read actions" : "Save and read actions"}</button></div>
    </form><div id="tOut" class="stack"><div class="empty">Credentials are encrypted at rest and never shown again. Actions that change data always need approval in agents that use them.</div></div></div>`;
  $("#tBack").onclick = () => { AG.toolKind = null; AG.toolEdit = null; AG.toolPreset = null; tabTools(box); };
  if (preset) type.fields.filter((f) => f.type === "secret" && preset.values[f.name]).forEach((f) => ($("#tForm").elements[f.name].value = preset.values[f.name]));
  const form = $("#tForm");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const v = {};
    type.fields.forEach((f) => {
      const el = form.elements[f.name];
      if (!el) return;
      if (f.type === "bool") v[f.name] = el.checked;
      else if (f.type === "number") { if (el.value !== "") v[f.name] = Number(el.value); }
      else if (el.value !== "") v[f.name] = el.value;
    });
    $("#tOut").innerHTML = `<div class="card"><span class="spin"></span> Connecting and reading actions…</div>`;
    try {
      const body = { kind: type.kind, name: form.elements.__name.value.trim(), values: v };
      const t = ed ? await api(`/api/tools/${ed.id}`, { method: "PUT", json: body }) : await api("/api/tools", { method: "POST", json: body });
      AG.cat = null; await loadCatalog();
      $("#tOut").innerHTML = `<div class="card"><h2>Saved: ${t.actions.length} action(s)</h2><div class="steps">${t.actions.map((x) => `<div class="s"><span class="dot ${x.mutates ? "w" : ""}"></span><div><span class="mono">${esc(x.name)}</span> ${x.mutates ? pill("needs approval", "warn") : pill("read", "ok")}<div class="muted" style="font-size:12.5px">${esc(x.description)}</div></div></div>`).join("")}</div>
        <p class="muted">Add <b>${esc(t.name)}</b> to an agent in its builder.</p></div>`;
      AG.toolEdit = t;
    } catch (err) { $("#tOut").innerHTML = `<div class="banner block">${esc(err.message)}</div>`; }
  };
}

/* ------------------------------------------------------------ boot hook */

(() => {
  const m = location.search.match(/[?&]shared=([^&]+)/);
  if (m) { AG.shared = decodeURIComponent(m[1]); try { localStorage.setItem("smriti.view", "agents"); } catch (_) {} }
})();
