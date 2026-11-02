/* Workflow builder: a canvas of nodes and edges for agents of kind "workflow".
   Drag nodes to move them, drag from an output port (right) onto another node
   to connect, click a node to edit it, click an edge to select it, Delete removes
   the selection. Loaded before agents.js; uses app.js helpers at call time. */
"use strict";

const WF_IC = { start: "▶", agent: "◎", condition: "⑂", set_state: "≔", code: "{}", approval: "✋", loop: "↻", end: "■", note: "✎" };
const WF_HELP = {
  agent: "Runs a classic, agentic or research agent on the prompt. Its cited answer goes into the output variable.",
  condition: "Checks each condition in order and follows the first that is true, else the last output.",
  set_state: "Sets variables from expressions, without calling a model.",
  code: "Runs Python in the sandbox. The workflow's variables are in a dict called state. Print JSON on the last line to return a value.",
  approval: "Pauses until the person running the workflow approves or declines.",
  loop: "Runs one agent prompt for each item of a list (up to 20) and collects the answers.",
  end: "Finishes the run. The output template becomes the answer.",
  note: "A comment for people reading the workflow. It never runs.",
};
const NODE_W = 196;

function wfHandles(n) {
  if (n.type === "condition") return [...(n.data.branches || []).map((b, i) => ({ h: `b${i}`, label: b.label || `if ${i + 1}` })), { h: "else", label: "else" }];
  if (n.type === "approval") return [{ h: "approved", label: "approved" }, { h: "declined", label: "declined" }];
  if (n.type === "end" || n.type === "note") return [];
  return [{ h: "out", label: "" }];
}

function wfSummary(n) {
  const d = n.data || {};
  const cut = (s, k = 46) => { s = String(s || "").replace(/\s+/g, " ").trim(); return s.length > k ? s.slice(0, k - 1) + "…" : s; };
  switch (n.type) {
    case "agent": return `${d.kind || "classic"} → ${d.output || "answer"}`;
    case "loop": return `for ${d.item_var || "item"} in ${cut(d.items, 24)}`;
    case "condition": return (d.branches || []).map((b) => cut(b.expr, 30)).join(" · ") || "no conditions";
    case "set_state": return (d.assign || []).map((a) => a.name).join(", ") || "nothing set";
    case "code": return `→ ${d.output || "code_result"}`;
    case "approval": return cut(d.message);
    case "end": return cut(d.output || "{{ last }}");
    case "note": return cut(d.text, 80);
    default: return "query, chat_history";
  }
}

const WF_DEFAULTS = {
  agent: () => ({ kind: "classic", prompt: "{{ query }}", output: "answer", tools: ["search"], instructions: "" }),
  condition: () => ({ branches: [{ label: "yes", expr: "len(answer) > 0" }] }),
  set_state: () => ({ assign: [{ name: "x", expr: "1" }] }),
  code: () => ({ code: "result = {'length': len(state['query'])}\nprint(json.dumps(result))", output: "code_result" }),
  approval: () => ({ message: "Continue with this?\n\n{{ last }}" }),
  loop: () => ({ kind: "classic", items: "split(query, ';')", item_var: "item", prompt: "{{ item }}", output: "results", tools: ["search"] }),
  end: () => ({ output: "{{ last }}" }),
  note: () => ({ text: "" }),
};

function wfBlank() {
  return {
    nodes: [
      { id: "start", type: "start", title: "Start", x: 30, y: 140, data: {} },
      { id: "agent1", type: "agent", title: "AI agent", x: 270, y: 140, data: WF_DEFAULTS.agent() },
      { id: "end", type: "end", title: "End", x: 520, y: 140, data: WF_DEFAULTS.end() },
    ],
    edges: [{ source: "start", target: "agent1", handle: "out" }, { source: "agent1", target: "end", handle: "out" }],
  };
}

/* Mounts the editor into `box`. Returns { get } for the current graph. */
function workflowEditor(box, graph, cat) {
  const g = JSON.parse(JSON.stringify(graph && graph.nodes ? graph : wfBlank()));
  let sel = null; // {node: id} | {edge: index}
  let drag = null;
  const types = (cat.workflow?.nodes || []).filter((t) => t.type !== "start");
  box.innerHTML = `
    <div class="wf-bar">${types.map((t) => `<button type="button" class="btn ghost sm" data-add="${t.type}">${WF_IC[t.type] || "+"} ${esc(t.label)}</button>`).join("")}
      <span class="spacer"></span><span class="fhelp">Drag from ● to connect</span></div>
    <div class="wf-canvas" tabindex="0" aria-label="Workflow canvas"><svg class="wf-edges"></svg><div class="wf-nodes"></div></div>
    <div class="wf-insp" id="wfInsp"></div>
    <details class="wf-ref"><summary>Variables and expressions</summary>
      <p class="fhelp">Every run has <code>query</code> and <code>chat_history</code>. Agent steps write their output variable and <code>last</code>; every node writes <code>node_&lt;id&gt;</code>.
      Templates use <code>{{ expression }}</code>. Conditions and Set state use expressions such as <code>len(answer) &gt; 0 and contains(answer, 'clause')</code>.
      Functions: ${(cat.workflow?.functions || []).map((f) => `<code>${esc(f)}</code>`).join(" ")}. A run stops after ${cat.workflow?.max_steps || 50} steps.</p></details>`;
  const canvas = $(".wf-canvas", box), svg = $(".wf-edges", box), layer = $(".wf-nodes", box), insp = $("#wfInsp", box);
  const byId = (id) => g.nodes.find((n) => n.id === id);
  const portXY = (n, h) => {
    const hs = wfHandles(n); const i = Math.max(0, hs.findIndex((x) => x.h === h));
    return [n.x + NODE_W, n.y + 44 + i * 20];
  };
  const inXY = (n) => [n.x, n.y + 30];
  const curve = ([x1, y1], [x2, y2]) => { const dx = Math.max(40, Math.abs(x2 - x1) / 2); return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`; };

  function draw(withInspector = true) {
    const w = Math.max(900, ...g.nodes.map((n) => n.x + NODE_W + 60)), h = Math.max(380, ...g.nodes.map((n) => n.y + 150));
    layer.style.width = svg.style.width = w + "px"; layer.style.height = svg.style.height = h + "px";
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.innerHTML = g.edges.map((e, i) => {
      const s = byId(e.source), t = byId(e.target); if (!s || !t) return "";
      const on = sel && sel.edge === i;
      return `<path class="wf-edge ${on ? "on" : ""}" data-edge="${i}" d="${curve(portXY(s, e.handle), inXY(t))}"/>`;
    }).join("") + (drag && drag.line ? `<path class="wf-edge draft" d="${curve(drag.from, drag.to)}"/>` : "");
    layer.innerHTML = g.nodes.map((n) => `
      <div class="wfn t-${n.type} ${sel && sel.node === n.id ? "on" : ""}" data-node="${esc(n.id)}" style="left:${n.x}px;top:${n.y}px;width:${NODE_W}px">
        ${n.type !== "start" && n.type !== "note" ? `<span class="wf-in"></span>` : ""}
        <div class="wfn-h"><span class="wfn-ic">${WF_IC[n.type]}</span><b>${esc(n.title)}</b></div>
        <div class="wfn-s">${esc(wfSummary(n))}</div>
        <div class="wfn-ports">${wfHandles(n).map((p) => `<div class="wf-port" data-port="${esc(p.h)}">${esc(p.label)}<span class="wf-dot"></span></div>`).join("")}</div>
      </div>`).join("");
    if (withInspector) inspector();
  }

  function inspector() {
    const n = sel && sel.node ? byId(sel.node) : null;
    if (sel && sel.edge !== undefined) {
      insp.innerHTML = `<div class="row"><b>Connection</b><span class="spacer"></span><button type="button" class="btn ghost sm" id="wfDelE">Remove connection</button></div>`;
      $("#wfDelE", insp).onclick = () => { g.edges.splice(sel.edge, 1); sel = null; draw(); };
      return;
    }
    if (!n) { insp.innerHTML = `<p class="fhelp">Click a step to edit it.</p>`; return; }
    const d = n.data;
    const field = (label, key, val, kind = "text", rows = 3) => kind === "area"
      ? `<label class="full">${label}<textarea data-k="${key}" rows="${rows}" class="${key === "code" ? "mono" : ""}">${esc(val ?? "")}</textarea></label>`
      : `<label>${label}<input type="text" data-k="${key}" value="${esc(val ?? "")}" class="${/expr|items|output|item_var/.test(key) ? "mono" : ""}"></label>`;
    const kindSel = () => `<label>Agent type<select data-k="kind">${["classic", "agentic", "research"].map((k) => `<option ${d.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select></label>`;
    const toolPicks = () => `<div class="full"><div class="fhelp">Tools (agentic and research)</div><div class="chips">${[...AG.cat.tools.builtin.map((t) => [t.kind, t.label]), ...AG.cat.configured.map((t) => [t.ref, t.name])].map(([ref, label]) => `<label class="chip pick"><input type="checkbox" data-tool="${esc(ref)}" ${(d.tools || ["search"]).includes(ref) ? "checked" : ""}> ${esc(label)}</label>`).join("")}</div></div>`;
    let body = "";
    if (n.type === "agent" || n.type === "loop") {
      body = (n.type === "loop" ? field("Items (expression giving a list)", "items", d.items) + field("Item variable", "item_var", d.item_var) : "")
        + kindSel() + field("Output variable", "output", d.output) + field("Prompt", "prompt", d.prompt, "area") + field("Instructions", "instructions", d.instructions, "area", 2) + toolPicks();
    } else if (n.type === "condition") {
      body = `<div class="full">${(d.branches || []).map((b, i) => `<div class="row wf-branch"><input type="text" data-b="${i}" data-bk="label" value="${esc(b.label)}" placeholder="label" style="width:110px"><input type="text" class="mono" data-b="${i}" data-bk="expr" value="${esc(b.expr)}" placeholder="len(answer) > 0" style="flex:1"><button type="button" class="btn ghost sm" data-delb="${i}">✕</button></div>`).join("")}
        <button type="button" class="btn ghost sm" id="wfAddB">+ Condition</button><div class="fhelp">The first true condition wins; otherwise the else output is taken.</div></div>`;
    } else if (n.type === "set_state") {
      body = `<div class="full">${(d.assign || []).map((a, i) => `<div class="row wf-branch"><input type="text" class="mono" data-a="${i}" data-ak="name" value="${esc(a.name)}" placeholder="variable" style="width:130px"> = <input type="text" class="mono" data-a="${i}" data-ak="expr" value="${esc(a.expr)}" placeholder="expression" style="flex:1"><button type="button" class="btn ghost sm" data-dela="${i}">✕</button></div>`).join("")}
        <button type="button" class="btn ghost sm" id="wfAddA">+ Variable</button></div>`;
    } else if (n.type === "code") body = field("Output variable", "output", d.output) + field("Python", "code", d.code, "area", 8);
    else if (n.type === "approval") body = field("Message shown to the person", "message", d.message, "area");
    else if (n.type === "end") body = field("Answer template", "output", d.output, "area");
    else if (n.type === "note") body = field("Note", "text", d.text, "area");
    else body = `<p class="fhelp full">Every run starts here with <code>query</code> (the question) and <code>chat_history</code>.</p>`;
    insp.innerHTML = `<div class="row"><b>${WF_IC[n.type]} ${esc(n.type.replace("_", " "))}</b> <span class="mono muted">${esc(n.id)}</span><span class="spacer"></span>${n.type !== "start" ? `<button type="button" class="btn ghost sm" id="wfDelN">Delete step</button>` : ""}</div>
      <p class="fhelp">${esc(WF_HELP[n.type] || "")}</p>
      <div class="formgrid"><label>Title<input type="text" data-title value="${esc(n.title)}"></label>${body}</div>`;
    const redraw = () => { const keep = document.activeElement && document.activeElement.closest("#wfInsp") ? document.activeElement : null; drawNodesOnly(); if (keep) keep.focus(); };
    $$("[data-title]", insp).forEach((el) => (el.oninput = () => { n.title = el.value; redraw(); }));
    $$("[data-k]", insp).forEach((el) => (el.oninput = el.onchange = () => { d[el.dataset.k] = el.value; redraw(); }));
    $$("[data-tool]", insp).forEach((el) => (el.onchange = () => { d.tools = $$("[data-tool]:checked", insp).map((x) => x.dataset.tool); }));
    $$("[data-b]", insp).forEach((el) => (el.oninput = () => { d.branches[+el.dataset.b][el.dataset.bk] = el.value; drawNodesOnly(); }));
    $$("[data-a]", insp).forEach((el) => (el.oninput = () => { d.assign[+el.dataset.a][el.dataset.ak] = el.value; drawNodesOnly(); }));
    $$("[data-delb]", insp).forEach((el) => (el.onclick = () => {
      const i = +el.dataset.delb; d.branches.splice(i, 1);
      // Re-point branch edges after the removed one.
      g.edges = g.edges.filter((e) => !(e.source === n.id && e.handle === `b${i}`)).map((e) => {
        const m = e.source === n.id && /^b(\d+)$/.exec(e.handle); return m && +m[1] > i ? { ...e, handle: `b${+m[1] - 1}` } : e;
      });
      draw();
    }));
    $$("[data-dela]", insp).forEach((el) => (el.onclick = () => { d.assign.splice(+el.dataset.dela, 1); draw(); }));
    const addB = $("#wfAddB", insp); if (addB) addB.onclick = () => { d.branches.push({ label: "", expr: "" }); draw(); };
    const addA = $("#wfAddA", insp); if (addA) addA.onclick = () => { d.assign.push({ name: "", expr: "" }); draw(); };
    const delN = $("#wfDelN", insp); if (delN) delN.onclick = () => { removeNode(n.id); };
  }

  // Redraws nodes and edges without rebuilding the inspector (keeps typing focus).
  const drawNodesOnly = () => draw(false);

  function removeNode(id) {
    g.nodes = g.nodes.filter((n) => n.id !== id);
    g.edges = g.edges.filter((e) => e.source !== id && e.target !== id);
    sel = null; draw();
  }

  function addNode(type) {
    let i = 1; while (byId(`${type.replace("_", "")}${i}`)) i++;
    const id = `${type.replace("_", "")}${i}`;
    const sx = canvas.scrollLeft + 40 + (g.nodes.length % 4) * 30, sy = canvas.scrollTop + 30 + (g.nodes.length % 5) * 24;
    const label = (AG.cat.workflow?.nodes || []).find((t) => t.type === type)?.label || type;
    g.nodes.push({ id, type, title: label, x: sx, y: sy, data: WF_DEFAULTS[type] ? WF_DEFAULTS[type]() : {} });
    sel = { node: id }; draw();
  }

  $$("[data-add]", box).forEach((b) => (b.onclick = () => addNode(b.dataset.add)));

  const pt = (e) => { const r = layer.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  layer.addEventListener("pointerdown", (e) => {
    const port = e.target.closest(".wf-port"), nodeEl = e.target.closest(".wfn");
    if (!nodeEl) { sel = null; draw(); return; }
    const n = byId(nodeEl.dataset.node);
    if (port) {
      e.preventDefault();
      drag = { kind: "link", source: n.id, handle: port.dataset.port, from: portXY(n, port.dataset.port), to: pt(e), line: true };
    } else {
      const [px, py] = pt(e);
      drag = { kind: "move", id: n.id, dx: px - n.x, dy: py - n.y, moved: false };
      sel = { node: n.id }; draw();
    }
    try { layer.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
  });
  layer.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const [px, py] = pt(e);
    if (drag.kind === "move") {
      const n = byId(drag.id); n.x = Math.max(0, Math.round((px - drag.dx) / 10) * 10); n.y = Math.max(0, Math.round((py - drag.dy) / 10) * 10);
      drag.moved = true; drawNodesOnly();
    } else { drag.to = [px, py]; drawNodesOnly(); }
  });
  layer.addEventListener("pointerup", (e) => {
    if (!drag) return;
    if (drag.kind === "link") {
      const [px, py] = pt(e);
      const target = g.nodes.find((n) => px >= n.x && px <= n.x + NODE_W && py >= n.y && py <= n.y + 90 && n.id !== drag.source && n.type !== "start" && n.type !== "note");
      if (target) {
        g.edges = g.edges.filter((x) => !(x.source === drag.source && x.handle === drag.handle));
        g.edges.push({ source: drag.source, target: target.id, handle: drag.handle });
      }
    }
    drag = null; draw();
  });
  svg.addEventListener("click", (e) => { const p = e.target.closest("[data-edge]"); if (p) { sel = { edge: +p.dataset.edge }; draw(); } });
  canvas.addEventListener("keydown", (e) => {
    if ((e.key === "Delete" || e.key === "Backspace") && sel && e.target === canvas) {
      e.preventDefault();
      if (sel.edge !== undefined) { g.edges.splice(sel.edge, 1); sel = null; draw(); }
      else if (sel.node && byId(sel.node)?.type !== "start") removeNode(sel.node);
    }
  });
  draw();
  return { get: () => JSON.parse(JSON.stringify(g)) };
}
