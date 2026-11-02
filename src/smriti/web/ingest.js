/* Ingest: sources (connectors), the add-source wizard, uploads and jobs.
   Loaded before app.js; uses its helpers ($, esc, api, pill, toast, openDoc, S) at call time. */
"use strict";

const ING = { tab: "sources", types: null, presets: null, editing: null, kind: null, files: [], pollTimer: null };

function canManage() { return can("curate") || can("administer"); }
function stopPolling() { if (ING.pollTimer) { clearInterval(ING.pollTimer); ING.pollTimer = null; } }
function fmtBytes(n) { if (n == null) return ""; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${u[i]}`; }
function fmtDur(a, b) { if (!a) return ""; const s = Math.max(0, (b || Date.now() / 1000) - a); return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} m ${Math.round(s % 60)} s`; }
const JOB_CLS = { done: "ok", failed: "bad", running: "violet", queued: "neutral", cancelling: "warn", cancelled: "neutral" };

async function viewIngest() {
  stopPolling();
  if (!ING.types) [ING.types, ING.presets] = await Promise.all([api("/api/connectors/types"), canManage() ? api("/api/connectors/presets") : []]);
  if (!canManage() && ["sources", "add"].includes(ING.tab)) ING.tab = can("upload") ? "upload" : "jobs";
  const tabs = [
    canManage() && ["sources", "Sources"],
    canManage() && ["add", ING.editing ? "Edit source" : "Add source"],
    can("upload") && ["upload", "Upload files"],
    ["jobs", "Jobs"],
  ].filter(Boolean);
  $("#view").innerHTML = `
    <div class="eyebrow">Ingest</div>
    <h1>Bring knowledge in</h1>
    <p class="lede">Connect websites, APIs, databases, MCP servers, feeds and shared folders, or upload files. Every item goes through the same pipeline: parsed, versioned, PII-masked, indexed, and governed by the access you set here. Re-runs only add what changed.</p>
    <div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${ING.tab === k ? "on" : ""}">${esc(l)}</button>`).join("")}</div>
    <div id="ingBody"></div>`;
  $$(".tabs button").forEach((b) => (b.onclick = () => { ING.tab = b.dataset.tab; if (ING.tab !== "add") ING.editing = null; viewIngest(); }));
  const body = { sources: tabSources, add: tabAdd, upload: tabUpload, jobs: tabJobs }[ING.tab];
  await body($("#ingBody"));
}

/* ------------------------------------------------------------ sources */

async function tabSources(box) {
  const d = await api("/api/connectors");
  const cards = d.connectors.map((c) => {
    const j = c.last_job;
    const st = j ? j.stats || {} : {};
    return `<div class="conn">
      <div class="ch"><span class="ki">${esc(c.icon)}</span><div style="flex:1;min-width:0"><div class="cn">${esc(c.name)}</div><div class="cs">${esc(c.label)}${c.schedule ? " · " + esc(c.schedule) : " · manual"}</div></div>
        <label class="switch"><input type="checkbox" data-en="${c.id}" ${c.enabled ? "checked" : ""}> on</label></div>
      <div class="row">${j ? pill(j.status, JOB_CLS[j.status]) : pill("never run", "neutral")}
        ${j ? `<span class="cs">${st.ingested || 0} new · ${st.new_versions || 0} updated · ${st.unchanged || 0} unchanged${st.failed ? ` · <span class="bad">${st.failed} failed</span>` : ""}</span>` : ""}</div>
      ${j ? `<div class="cs">${esc(new Date((j.finished_at || j.started_at || j.created_at) * 1000).toLocaleString("en-IN"))} by ${esc(j.created_by)}</div>` : ""}
      <div class="row"><button class="btn sm" data-run="${c.id}">Run now</button><button class="btn ghost sm" data-edit="${c.id}">Edit</button>
        ${j ? `<button class="btn ghost sm" data-job="${j.id}">Last job</button>` : ""}<span class="spacer"></span><button class="btn ghost sm" data-del="${c.id}" title="Documents already ingested stay in the library">Remove</button></div>
    </div>`;
  });
  box.innerHTML = `
    <div class="row" style="margin-bottom:12px"><h2 style="margin:0">Your sources</h2><span class="spacer"></span><button class="btn accent" id="addBtn">+ Add source</button></div>
    ${cards.length ? `<div class="conns">${cards.join("")}</div>` : `<div class="empty">No sources yet. Add a website, API, database, MCP server, feed or folder, or try one of the demo sources.</div>`}
    ${d.pack_sources.length ? `<h2 style="margin-top:24px">Sources managed in the tenant pack <span class="mono muted">(read-only here; edit packs/…/sources/*.yaml)</span></h2>
      <div class="tablewrap"><table class="t"><thead><tr><th>Source</th><th>Mode</th><th>Schedule</th><th>Last success</th><th>Tracked</th></tr></thead><tbody>
      ${d.pack_sources.map((s) => `<tr><td><b>${esc(s.source)}</b><div class="mono muted">${esc(s.start || "")}</div></td><td class="mono">${esc(s.mode)}</td><td class="mono">${esc(s.schedule || "manual")}</td><td>${s.last_success ? esc(new Date(s.last_success * 1000).toLocaleString("en-IN")) : pill("never", "neutral")}</td><td class="mono">${s.items_tracked}</td></tr>`).join("")}
      </tbody></table></div>` : ""}`;
  $("#addBtn").onclick = () => { ING.tab = "add"; ING.editing = null; ING.kind = null; viewIngest(); };
  $$("[data-run]", box).forEach((b) => (b.onclick = async () => {
    const r = await api(`/api/connectors/${b.dataset.run}/run`, { method: "POST" });
    toast(`Job #${r.job_id} queued`); ING.tab = "jobs"; viewIngest();
  }));
  $$("[data-edit]", box).forEach((b) => (b.onclick = () => { ING.editing = d.connectors.find((c) => c.id === +b.dataset.edit); ING.kind = ING.editing.kind; ING.tab = "add"; viewIngest(); }));
  $$("[data-job]", box).forEach((b) => (b.onclick = () => openJob(+b.dataset.job)));
  $$("[data-del]", box).forEach((b) => (b.onclick = async () => {
    if (!confirm("Remove this source? Documents it already ingested stay in the library.")) return;
    await api(`/api/connectors/${b.dataset.del}`, { method: "DELETE" }); toast("Source removed"); viewIngest();
  }));
  $$("[data-en]", box).forEach((x) => (x.onchange = async () => {
    await api(`/api/connectors/${x.dataset.en}/enabled?enabled=${x.checked}`, { method: "POST" });
    toast(x.checked ? "Schedule enabled" : "Schedule paused");
  }));
}

/* ------------------------------------------------------------ add / edit */

async function tabAdd(box) {
  const kinds = ING.types.filter((t) => t.kind !== "upload");
  if (!ING.kind) {
    box.innerHTML = `
      <h2>What do you want to connect?</h2>
      <div class="kinds">${kinds.map((t) => `<button class="kind" data-kind="${t.kind}"><div class="ki">${esc(t.icon)}</div><div class="kl">${esc(t.label)}</div><div class="kd">${esc(t.description)}</div></button>`).join("")}
        <button class="kind" data-kind="upload"><div class="ki">⇪</div><div class="kl">Upload files</div><div class="kd">PDF, Word, Excel, HTML, email, CSV, Markdown or ZIP from your computer.</div></button></div>
      ${ING.presets.length ? `<h2 style="margin-top:24px">Or try a demo source</h2><p class="muted" style="margin-top:0">Built-in sample targets served by this Smriti instance, one per connector kind.</p>
        <div class="row">${ING.presets.map((p, i) => `<button class="btn ghost sm" data-preset="${i}">${esc(p.name)}</button>`).join("")}</div>` : ""}`;
    $$("[data-kind]", box).forEach((b) => (b.onclick = () => { if (b.dataset.kind === "upload") { ING.tab = "upload"; } else { ING.kind = b.dataset.kind; } viewIngest(); }));
    $$("[data-preset]", box).forEach((b) => (b.onclick = () => { const p = ING.presets[+b.dataset.preset]; ING.kind = p.kind; ING.editing = null; ING.preset = p; viewIngest(); }));
    return;
  }
  const type = ING.types.find((t) => t.kind === ING.kind);
  const ed = ING.editing;
  const pre = ING.preset && ING.preset.kind === ING.kind ? ING.preset : null;
  const values = ed ? ed.config : pre ? pre.values : {};
  const name = ed ? ed.name : pre ? pre.name : "";
  ING.preset = null;
  const groups = {};
  type.fields.forEach((f) => (groups[f.group] = groups[f.group] || []).push(f));
  box.innerHTML = `
    <div class="row" style="margin-bottom:10px"><span style="font-size:22px">${esc(type.icon)}</span><h2 style="margin:0">${esc(type.label)}</h2><span class="spacer"></span>
      ${ed ? "" : `<button class="btn ghost sm" id="backKinds">← Choose another kind</button>`}</div>
    <p class="muted" style="margin-top:0">${esc(type.description)}</p>
    <div class="grid2">
      <form id="srcForm">
        <fieldset class="fg"><legend>Name</legend><input type="text" name="__name" value="${esc(name)}" placeholder="e.g. Board circulars site" style="width:100%" required></fieldset>
        ${Object.entries(groups).map(([g, fs]) => `<fieldset class="fg"><legend>${esc(g)}</legend><div class="formgrid">${fs.map((f) => fieldHtml(f, values, ed)).join("")}</div></fieldset>`).join("")}
        <div class="row"><button type="button" class="btn ghost" id="testBtn">Test connection</button><span class="spacer"></span>
          <button type="submit" class="btn">${ed ? "Save changes" : "Save"}</button><button type="button" class="btn accent" id="saveRun">${ed ? "Save & run" : "Save & run now"}</button></div>
      </form>
      <div id="pvBox" class="stack"><div class="empty">Use <b>Test connection</b> to see what this source would bring in, before anything is ingested.</div></div>
    </div>`;
  const back = $("#backKinds");
  if (back) back.onclick = () => { ING.kind = null; viewIngest(); };
  const form = $("#srcForm");
  const collect = () => {
    const v = {};
    type.fields.forEach((f) => {
      const el = form.elements[f.name];
      if (!el) return;
      if (f.type === "bool") v[f.name] = el.checked;
      else if (f.type === "number") { if (el.value !== "") v[f.name] = Number(el.value); }
      else if (el.value !== "") v[f.name] = el.value;
    });
    return v;
  };
  $("#testBtn").onclick = async () => {
    const pv = $("#pvBox");
    pv.innerHTML = `<div class="card"><span class="spin"></span> Connecting and reading a few items…</div>`;
    try {
      const r = await api("/api/connectors/preview", { method: "POST", json: { kind: type.kind, values: collect(), connector_id: ed ? ed.id : null } });
      pv.innerHTML = previewHtml(r);
    } catch (e) { pv.innerHTML = `<div class="banner block">${esc(e.message)}</div>`; }
  };
  const save = async (run) => {
    const n = form.elements.__name.value.trim();
    if (!n) return toast("Give the source a name");
    try {
      let jobId = null;
      if (ed) {
        await api(`/api/connectors/${ed.id}`, { method: "PUT", json: { name: n, kind: type.kind, values: collect() } });
        if (run) jobId = (await api(`/api/connectors/${ed.id}/run`, { method: "POST" })).job_id;
      } else {
        const r = await api(`/api/connectors${run ? "?run=true" : ""}`, { method: "POST", json: { name: n, kind: type.kind, values: collect() } });
        jobId = r.job_id;
      }
      toast(run ? `Saved. Job #${jobId} queued.` : "Saved");
      ING.editing = null; ING.kind = null; ING.tab = run ? "jobs" : "sources";
      viewIngest();
    } catch (e) { toast(e.message, 6000); }
  };
  form.onsubmit = (e) => { e.preventDefault(); save(false); };
  $("#saveRun").onclick = () => save(true);
}

function fieldHtml(f, values, ed) {
  const v = values[f.name] ?? f.default ?? "";
  const help = f.help ? `<span class="fhelp">${esc(f.help)}</span>` : "";
  const lab = `${esc(f.label)}${f.required ? " *" : ""}`;
  const wide = ["textarea", "json", "url"].includes(f.type) || f.name === "query";
  const cls = wide ? ' class="full"' : "";
  if (f.type === "bool") return `<label${cls}><span><input type="checkbox" name="${esc(f.name)}" ${v ? "checked" : ""}> ${lab}</span>${help}</label>`;
  if (f.type === "select") return `<label${cls}>${lab}<select name="${esc(f.name)}">${f.options.map((o) => `<option value="${esc(o)}" ${String(o) === String(v) ? "selected" : ""}>${esc(o || "(none)")}</option>`).join("")}</select>${help}</label>`;
  if (f.type === "textarea" || f.type === "json") return `<label class="full">${lab}<textarea name="${esc(f.name)}" rows="${f.type === "json" ? 2 : 4}" class="${f.type === "json" ? "mono" : ""}" placeholder="${esc(f.placeholder)}">${esc(typeof v === "object" ? JSON.stringify(v) : v)}</textarea>${help}</label>`;
  if (f.type === "secret") {
    const set = ed && ed.secrets_set.includes(f.name);
    return `<label class="full">${lab}<input type="password" autocomplete="new-password" name="${esc(f.name)}" placeholder="${set ? "•••••••• stored (leave blank to keep)" : esc(f.placeholder || "")}">${help}</label>`;
  }
  const t = f.type === "number" ? "number" : f.type === "url" ? "url" : "text";
  return `<label${cls}>${lab}<input type="${t}" name="${esc(f.name)}" value="${esc(v)}" placeholder="${esc(f.placeholder)}" ${t === "number" ? 'step="any"' : ""}>${help}</label>`;
}

function previewHtml(r) {
  if (!r.ok) return `<div class="banner block"><b>Could not read this source.</b><br>${esc(r.error)}</div>${r.log?.length ? `<pre class="log">${esc(r.log.join("\n"))}</pre>` : ""}`;
  if (!r.items.length) return `<div class="banner abst">Connected, but found nothing to ingest. Check the selection settings.</div>`;
  return `<div class="card"><div class="row"><h2 style="margin:0">Preview</h2>${pill("connected", "ok")}<span class="spacer"></span><span class="mono muted">${r.items.length}${r.more ? "+" : ""} items · ${r.elapsed_s}s · nothing ingested</span></div>
    ${r.items.map((it) => `<div class="pv"><div class="row"><b>${esc(it.title || it.filename)}</b><span class="spacer"></span>${it.parser ? pill(it.parser, "neutral") : ""}${it.error ? pill("parse error", "bad") : ""}</div>
      <div class="mono muted" style="font-size:11px;word-break:break-all">${esc(it.url || it.filename)}${it.size ? " · " + fmtBytes(it.size) : ""}${it.pages ? " · " + it.pages + " page(s)" : ""}</div>
      ${it.snippet ? `<div class="snip">${esc(it.snippet)}</div>` : ""}${it.error ? `<div class="bad" style="font-size:12px">${esc(it.error)}</div>` : ""}</div>`).join("")}
    ${r.log?.length ? `<h3>Log</h3><pre class="log">${esc(r.log.join("\n"))}</pre>` : ""}</div>`;
}

/* ------------------------------------------------------------ upload */

async function tabUpload(box) {
  ING.files = [];
  box.innerHTML = `
    <div class="grid2">
      <div class="stack">
        <div class="drop" id="drop"><div style="font-size:28px">⇪</div><b>Drop files here</b> or click to choose<div class="fhelp" style="margin-top:6px">PDF, Word, Excel, HTML, email (.eml), CSV, Markdown, text, or ZIP archives (unpacked automatically). Up to 200 MB each.</div></div>
        <input type="file" id="filePick" multiple hidden>
        <div id="fileList"></div>
      </div>
      <form id="upAccess" class="card">
        <h2>Who can read these files</h2>
        <div class="formgrid">
          <label>Sensitivity<select name="doc_class"><option>public</option><option selected>internal</option><option>confidential</option><option>secret</option></select></label>
          <label>Document type<input type="text" name="doc_type" placeholder="auto"></label>
          <label class="full">Divisions<input type="text" name="divisions" value="*"><span class="fhelp">comma separated; * = whole organisation</span></label>
          <label>Projects<input type="text" name="projects" placeholder="e.g. P-17"></label>
          <label>Need-to-know groups<input type="text" name="groups"></label>
        </div>
        <div class="row" style="margin-top:12px"><span class="muted" id="upMsg"></span><span class="spacer"></span><button class="btn accent" id="upBtn" type="submit" disabled>Upload and ingest</button></div>
      </form>
    </div>
    <div id="upJob" style="margin-top:16px"></div>`;
  const drop = $("#drop"), pick = $("#filePick");
  const render = () => {
    $("#fileList").innerHTML = ING.files.length ? `<div class="tablewrap" style="margin-top:12px"><table class="t"><thead><tr><th>File</th><th>Size</th><th></th></tr></thead><tbody>${ING.files.map((f, i) => `<tr><td>${esc(f.name)}</td><td class="mono">${fmtBytes(f.size)}</td><td><button class="btn ghost sm" data-rm="${i}" type="button">✕</button></td></tr>`).join("")}</tbody></table></div>` : "";
    $$("[data-rm]").forEach((b) => (b.onclick = () => { ING.files.splice(+b.dataset.rm, 1); render(); }));
    $("#upBtn").disabled = !ING.files.length;
    $("#upMsg").textContent = ING.files.length ? `${ING.files.length} file(s), ${fmtBytes(ING.files.reduce((a, f) => a + f.size, 0))}` : "";
  };
  const add = (list) => { for (const f of list) if (!ING.files.some((g) => g.name === f.name && g.size === f.size)) ING.files.push(f); render(); };
  drop.onclick = () => pick.click();
  pick.onchange = () => add(pick.files);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); add(e.dataTransfer.files); };
  $("#upAccess").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    ING.files.forEach((f) => fd.append("files", f, f.name));
    $("#upBtn").disabled = true;
    $("#upMsg").innerHTML = '<span class="spin"></span> Uploading…';
    try {
      const r = await fetch("/api/ingest/upload", { method: "POST", body: fd, headers: { "X-Smriti-User": S.user } });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail);
      ING.files = []; render();
      $("#upMsg").textContent = `Job #${j.job_id} queued`;
      watchJob(j.job_id, $("#upJob"));
    } catch (err) { $("#upMsg").textContent = err.message; $("#upBtn").disabled = false; }
  };
}

function watchJob(id, el) {
  stopPolling();
  const tick = async () => {
    try {
      const j = await api(`/api/jobs/${id}`);
      el.innerHTML = jobPanel(j);
      bindJobPanel(el, j);
      if (!["queued", "running", "cancelling"].includes(j.status)) { stopPolling(); refreshJobBadge(); }
    } catch (_) { stopPolling(); }
  };
  tick();
  ING.pollTimer = setInterval(tick, 1200);
}

/* ------------------------------------------------------------ jobs */

async function tabJobs(box) {
  const draw = async () => {
    const jobs = await api("/api/jobs?limit=60");
    box.innerHTML = jobs.length ? `<div class="tablewrap"><table class="t"><thead><tr><th>#</th><th>Job</th><th>Status</th><th>Progress</th><th>Started</th><th>Took</th><th>By</th></tr></thead><tbody>
      ${jobs.map((j) => { const st = j.stats || {}; const active = ["queued", "running", "cancelling"].includes(j.status); return `<tr class="click" data-job="${j.id}"><td class="mono">${j.id}</td><td><b>${esc(j.title || j.kind)}</b><div class="cs mono muted">${esc(j.kind)}</div></td>
        <td>${pill(j.status, JOB_CLS[j.status])}</td>
        <td style="min-width:200px">${active && j.status !== "queued" ? `<div class="prog ind"><div></div></div>` : ""}<span class="mono" style="font-size:11.5px">${st.found || 0} found · ${st.ingested || 0} new · ${st.new_versions || 0} upd · ${st.unchanged || 0} same${st.failed ? ` · <span class="bad">${st.failed} failed</span>` : ""}</span>${j.error ? `<div class="bad" style="font-size:12px">${esc(j.error.slice(0, 120))}</div>` : ""}</td>
        <td class="mono">${j.started_at ? esc(new Date(j.started_at * 1000).toLocaleTimeString("en-IN")) : "—"}</td><td class="mono">${fmtDur(j.started_at, j.finished_at)}</td><td>${esc(j.created_by)}</td></tr>`; }).join("")}
      </tbody></table></div>` : `<div class="empty">No ingestion jobs yet.</div>`;
    $$("[data-job]", box).forEach((tr) => (tr.onclick = () => openJob(+tr.dataset.job)));
    return jobs.some((j) => ["queued", "running", "cancelling"].includes(j.status));
  };
  const active = await draw();
  stopPolling();
  if (active) ING.pollTimer = setInterval(async () => { if (S.view !== "ingest" || ING.tab !== "jobs") return stopPolling(); if (!(await draw())) { stopPolling(); refreshJobBadge(); } }, 1500);
}

function jobPanel(j) {
  const st = j.stats || {};
  const active = ["queued", "running", "cancelling"].includes(j.status);
  return `<div class="card">
    <div class="row"><h2 style="margin:0">Job #${j.id}: ${esc(j.title || j.kind)}</h2>${pill(j.status, JOB_CLS[j.status])}<span class="spacer"></span>${active ? `<button class="btn ghost sm" data-cancel="${j.id}">Cancel</button>` : ""}</div>
    ${active ? `<div class="prog ind" style="margin:10px 0"><div></div></div>` : ""}
    <div class="row" style="margin:10px 0;gap:22px">${[["found", "found"], ["ingested", "new"], ["new_versions", "updated"], ["unchanged", "unchanged"], ["failed", "failed"], ["removed", "removed"]].map(([k, l]) => `<div><div class="stat ${k === "failed" && st[k] ? "bad" : ""}" style="font-size:20px">${st[k] || 0}</div><div class="stat-l">${l}</div></div>`).join("")}
      <div><div class="stat" style="font-size:20px">${fmtBytes(st.bytes || 0)}</div><div class="stat-l">read</div></div></div>
    ${j.error ? `<div class="banner block">${esc(j.error)}</div>` : ""}
    ${(j.items || []).length ? `<div class="tablewrap" style="max-height:340px"><table class="t"><thead><tr><th>Item</th><th>Result</th><th>Document</th></tr></thead><tbody>
      ${j.items.slice().reverse().map((it) => `<tr><td>${esc(it.title || it.key)}<div class="mono muted" style="font-size:10.5px;word-break:break-all">${esc(it.url || "")}</div>${it.note ? `<div class="fhelp">${esc(it.note)}</div>` : ""}</td>
        <td>${pill(it.status === "duplicate" ? "unchanged" : it.status, it.status === "failed" ? "bad" : it.status === "ingested" ? "ok" : it.status === "new_version" ? "violet" : "neutral")}${it.error ? `<div class="bad" style="font-size:11.5px">${esc(it.error)}</div>` : ""}</td>
        <td>${it.doc_id ? `<a href="#" data-doc="${esc(it.doc_id)}">${esc(it.doc_title || it.doc_id)}</a>${it.version ? ` <span class="mono muted">v${it.version}</span>` : ""}` : ""}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
    ${(j.log || []).length ? `<h3>Log</h3><pre class="log">${esc(j.log.join("\n"))}</pre>` : ""}
  </div>`;
}

function bindJobPanel(el, j) {
  $$("[data-doc]", el).forEach((a) => (a.onclick = (e) => { e.preventDefault(); openDoc(a.dataset.doc); }));
  const c = $(`[data-cancel="${j.id}"]`, el);
  if (c) c.onclick = async () => { await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); toast("Cancelling after the current item…"); };
}

function openJob(id) {
  const dr = $("#drawer"), body = $("#drawerBody");
  dr.hidden = false;
  dr.onclick = (e) => { if (e.target === dr) { dr.hidden = true; stopPolling(); } };
  body.innerHTML = `<div class="row"><span class="eyebrow">Ingestion job</span><span class="spacer"></span><button class="btn ghost sm" id="jClose">Close</button></div><div id="jBody" style="margin-top:10px"><span class="spin"></span></div>`;
  $("#jClose").onclick = () => { dr.hidden = true; stopPolling(); };
  watchJob(id, $("#jBody"));
}

async function refreshJobBadge() {
  if (!(canManage() || can("upload"))) { $("#jobCount").textContent = ""; return; }
  try {
    const jobs = await api("/api/jobs?limit=30");
    const n = jobs.filter((j) => ["queued", "running"].includes(j.status)).length;
    $("#jobCount").textContent = n || "";
  } catch (_) { $("#jobCount").textContent = ""; }
}
