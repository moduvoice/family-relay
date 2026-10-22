/* MCP Apps view-side bridge (JSON-RPC 2.0 over postMessage, ext-apps spec 2026-01-26).
   A card defines window.render(structuredContent, toolResult) and optionally window.onHostContext(ctx). */
(function () {
  const pending = new Map();
  let nextId = 1;
  const DKS = (window.DKS = { hostContext: {}, toolInput: null, last: null });
  const post = (msg) => window.parent.postMessage(msg, "*");
  DKS.request = (method, params) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      post({ jsonrpc: "2.0", id, method, params });
    });
  DKS.notify = (method, params) => post({ jsonrpc: "2.0", method, params: params || {} });
  DKS.callTool = (name, args) => DKS.request("tools/call", { name, arguments: args || {} });
  DKS.say = (text) => DKS.request("ui/message", { role: "user", content: [{ type: "text", text }] });
  DKS.setMode = (mode) => DKS.request("ui/request-display-mode", { mode });

  function applyContext() {
    const ctx = DKS.hostContext || {};
    document.documentElement.dataset.mode = ctx.displayMode || "inline";
    if (ctx.theme) document.documentElement.dataset.theme = ctx.theme;
    if (window.onHostContext) window.onHostContext(ctx);
  }

  window.addEventListener("message", (ev) => {
    const m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id !== undefined && !m.method) {
      const p = pending.get(m.id);
      if (!p) return;
      pending.delete(m.id);
      m.error ? p.reject(m.error) : p.resolve(m.result);
      return;
    }
    switch (m.method) {
      case "ui/notifications/tool-input":
        DKS.toolInput = (m.params || {}).arguments || {};
        break;
      case "ui/notifications/tool-result":
        DKS.last = m.params;
        if (window.render) window.render(m.params.structuredContent || {}, m.params);
        break;
      case "ui/notifications/host-context-changed":
        Object.assign(DKS.hostContext, m.params || {});
        applyContext();
        break;
      case "ui/resource-teardown":
        post({ jsonrpc: "2.0", id: m.id, result: {} });
        break;
    }
  });

  DKS.request("ui/initialize", {
    protocolVersion: "2026-01-26",
    clientInfo: { name: "daily-korean-scene-card", version: "1.0.0" },
    capabilities: {},
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
  }).then((r) => {
    DKS.hostContext = (r && r.hostContext) || {};
    applyContext();
    DKS.notify("ui/notifications/initialized");
  });

  let lastH = 0;
  new ResizeObserver(() => {
    const h = document.documentElement.scrollHeight;
    if (h !== lastH) {
      lastH = h;
      DKS.notify("ui/notifications/size-changed", { width: document.documentElement.scrollWidth, height: h });
    }
  }).observe(document.body);

  /* shared helpers for cards */
  DKS.esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  DKS.mark = (text, span, cls) => {
    const t = String(text || "");
    if (!span || t.indexOf(span) < 0) return DKS.esc(t);
    const i = t.indexOf(span);
    return DKS.esc(t.slice(0, i)) + `<mark class="${cls}">` + DKS.esc(span) + "</mark>" + DKS.esc(t.slice(i + span.length));
  };
  DKS.markAll = (text, spans, cls) => {
    let t = String(text || "");
    const found = (spans || []).filter((s) => s && t.indexOf(s) >= 0);
    if (!found.length) return DKS.esc(t);
    // mark each span once, left to right, without overlapping
    const marks = [];
    for (const s of found) {
      const i = t.indexOf(s);
      if (!marks.some((m) => i < m[1] && i + s.length > m[0])) marks.push([i, i + s.length]);
    }
    marks.sort((a, b) => a[0] - b[0]);
    let out = "", pos = 0;
    for (const [a, b] of marks) {
      out += DKS.esc(t.slice(pos, a)) + `<mark class="${cls}">` + DKS.esc(t.slice(a, b)) + "</mark>";
      pos = b;
    }
    return out + DKS.esc(t.slice(pos));
  };
  DKS.verdictLabel = (v) => ({ correct: "Perfect", close: "Almost", incorrect: "Try again" }[v] || v);
})();
