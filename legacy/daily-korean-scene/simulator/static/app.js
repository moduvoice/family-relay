/* Alexa+ role simulator: browser = MCP client + MCP Apps host; /api/turn = the assistant's LLM. */
(() => {
  const $ = (id) => document.getElementById(id);
  const screen = $("screen");
  const state = {
    cfg: null, mcp: null, messages: [], mode: "inline", busy: false, queue: [],
    card: null, // {uri, iframe, ready, pending:[msgs], toolName}
    speech: Promise.resolve(), clock: null, voices: [],
  };

  /* ------------------------------------------------------------------ log panel */
  const logList = $("log-list");
  function log(e) {
    if (e.kind === "llm" && !$("log-llm").checked) return;
    if (e.kind === "ui" && !$("log-ui").checked) return;
    const div = document.createElement("div");
    div.className = `entry ${e.kind}`;
    const t = new Date().toISOString().slice(11, 19);
    const tag = { mcp: "MCP", err: "ERR", llm: "LLM", ui: "UI" }[e.kind] || e.kind;
    div.innerHTML = `<div class="line1"><span class="t">${t}</span><span class="k ${e.kind === "err" ? "mcp" : e.kind}">${tag}</span>` +
      `<span class="dir">${esc(e.dir || "")}</span><span class="what">${esc(e.what || "")}</span><span class="info">${esc(e.info || "")}</span></div>` +
      (e.body !== undefined && e.body !== null ? `<pre>${esc(JSON.stringify(e.body, null, 2))}</pre>` : "");
    div.addEventListener("click", () => div.classList.toggle("open"));
    logList.appendChild(div);
    logList.scrollTop = logList.scrollHeight;
  }
  $("log-clear").onclick = () => (logList.innerHTML = "");
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ------------------------------------------------------------------ orb & captions */
  function orb(stateName) {
    for (const o of document.querySelectorAll(".orb")) o.className = o.className.replace(/ (listening|thinking|speaking)/g, "") + (stateName ? " " + stateName : "");
  }
  function activate() { screen.classList.add("active"); }
  function captionYou(text) { $("cap-you").textContent = text; activate(); }
  function captionAssistant(text) { $("cap-assistant").textContent = text; activate(); }

  /* ------------------------------------------------------------------ speech output */
  function loadVoices() { state.voices = speechSynthesis ? speechSynthesis.getVoices() : []; }
  if (window.speechSynthesis) { loadVoices(); speechSynthesis.onvoiceschanged = loadVoices; }
  function ttsOnce(text, lang) {
    return new Promise((resolve) => {
      if (!$("sound").checked || !window.speechSynthesis || !text) return resolve();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = lang;
      const v = state.voices.find((x) => x.lang && x.lang.toLowerCase().startsWith(lang.slice(0, 2).toLowerCase()));
      if (v) u.voice = v;
      u.rate = lang.startsWith("ko") ? 0.9 : 1.0;
      let done = false;
      const fin = () => { if (!done) { done = true; resolve(); } };
      u.onend = fin; u.onerror = fin;
      setTimeout(fin, 1500 + text.length * 120);
      speechSynthesis.speak(u);
    });
  }
  function playAudio(url) {
    return new Promise((resolve) => {
      if (!$("sound").checked) return resolve();
      const a = new Audio(url);
      a.onended = resolve; a.onerror = resolve;
      a.play().catch(resolve);
    });
  }
  function enqueueSpeech(fn) {
    state.speech = state.speech.then(async () => { orb("speaking"); try { await fn(); } finally { orb(""); } });
    return state.speech;
  }
  function speakLines(lines) {
    for (const l of lines || []) {
      enqueueSpeech(() => {
        if (l.audio_url) return playAudio(l.audio_url);
        if (l.speaker === "narrator") return ttsOnce(l.en, "en-US");
        return ttsOnce(l.ko, "ko-KR");
      });
    }
  }
  function sayAssistant(text) {
    enqueueSpeech(async () => { captionAssistant(text); await ttsOnce(text, "en-US"); });
  }

  /* ------------------------------------------------------------------ MCP Apps host */
  function hostContext(toolName) {
    return {
      displayMode: state.mode === "fullscreen" ? "fullscreen" : "inline",
      availableDisplayModes: ["inline", "fullscreen"],
      theme: "dark", locale: "en-US", timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone, platform: "web",
      containerDimensions: { width: 768, maxHeight: state.mode === "fullscreen" ? 480 : 300 },
      deviceCapabilities: { touch: true, hover: false },
      toolInfo: toolName ? { tool: { name: toolName } } : undefined,
    };
  }
  function cspFor(meta) {
    const csp = (meta && meta.ui && meta.ui.csp) || {};
    const res = (csp.resourceDomains || []).join(" ");
    const con = (csp.connectDomains || []).join(" ") || "'none'";
    return `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: ${res}; media-src ${res}; font-src data: ${res}; connect-src ${con}`;
  }
  function postToCard(msg) {
    if (!state.card) return;
    if (!state.card.ready) { state.card.pending.push(msg); return; }
    log({ kind: "ui", dir: "host → card", what: msg.method || "response", info: msg.method === "ui/notifications/tool-result" ? (msg.params.structuredContent || {}).status || "" : "", body: msg });
    state.card.iframe.contentWindow.postMessage(msg, "*");
  }
  async function showCard(uri, toolName, args, result) {
    const slot = $("card-slot");
    if (state.mode === "voice-only") {
      log({ kind: "ui", dir: "host", what: "voice-only", info: `${uri} not rendered (no screen)` });
      return;
    }
    if (!state.card || state.card.uri !== uri) {
      const res = await state.mcp.readResource(uri);
      const html = res.text.replace("<head>", `<head><meta http-equiv="Content-Security-Policy" content="${cspFor(res._meta)}">`);
      slot.innerHTML = "";
      const iframe = document.createElement("iframe");
      iframe.setAttribute("sandbox", "allow-scripts");
      iframe.setAttribute("title", uri);
      iframe.srcdoc = html;
      slot.appendChild(iframe);
      slot.classList.add("has-card");
      state.card = { uri, iframe, ready: false, pending: [], toolName };
    }
    state.card.toolName = toolName;
    postToCard({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: args } });
    postToCard({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: result });
  }
  function setMode(mode) {
    state.mode = mode;
    screen.dataset.mode = mode;
    for (const b of document.querySelectorAll("#mode-seg button")) b.classList.toggle("on", b.dataset.mode === mode);
    if (state.card && state.card.ready && mode !== "voice-only")
      postToCard({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: hostContext(state.card.toolName) });
  }
  window.addEventListener("message", async (ev) => {
    if (!state.card || ev.source !== state.card.iframe.contentWindow) return;
    const m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    const reply = (result) => { log({ kind: "ui", dir: "host → card", what: `result #${m.id}`, info: "", body: result }); ev.source.postMessage({ jsonrpc: "2.0", id: m.id, result }, "*"); };
    const fail = (message) => ev.source.postMessage({ jsonrpc: "2.0", id: m.id, error: { code: -32000, message } }, "*");
    if (m.method !== "ui/notifications/size-changed") log({ kind: "ui", dir: "card → host", what: m.method, info: m.params && m.params.name ? m.params.name : "", body: m });
    switch (m.method) {
      case "ui/initialize":
        reply({ protocolVersion: "2026-01-26", hostInfo: { name: "dks-alexa-plus-simulator", version: "1.0.0" },
          hostCapabilities: { serverTools: {}, serverResources: {}, openLinks: {} }, hostContext: hostContext(state.card.toolName) });
        break;
      case "ui/notifications/initialized": {
        state.card.ready = true;
        const p = state.card.pending; state.card.pending = [];
        p.forEach(postToCard);
        break;
      }
      case "tools/call":
        try {
          const r = await state.mcp.callTool(m.params.name, m.params.arguments, "card");
          reply(r);
          const text = (r.content || []).map((c) => c.text).join(" ");
          enqueueTurn(`[card] The user tapped a button on the card, which called ${m.params.name}. Result: ${text}`, null);
        } catch (e) { fail(String(e)); }
        break;
      case "ui/request-display-mode":
        if (state.mode !== "voice-only") setMode(m.params.mode === "fullscreen" ? "fullscreen" : "inline");
        reply({ mode: state.mode });
        break;
      case "ui/message": {
        reply({});
        const text = (m.params.content || []).map((c) => c.text).join(" ");
        enqueueTurn(`[card] ${text}`, text);
        break;
      }
      case "ui/open-link":
        reply({});
        break;
      default:
        if (m.id !== undefined && m.method) fail(`unsupported ${m.method}`);
    }
  });

  /* ------------------------------------------------------------------ assistant loop */
  function enqueueTurn(taggedText, caption) {
    state.queue.push({ taggedText, caption });
    if (!state.busy) drain();
  }
  async function drain() {
    state.busy = true;
    while (state.queue.length) {
      const t = state.queue.shift();
      if (t.caption) captionYou(t.caption);
      state.messages.push({ role: "user", content: [{ text: t.taggedText }] });
      try { await runAgent(); } catch (e) { log({ kind: "err", what: "assistant error", info: String(e) }); captionAssistant("Sorry, something went wrong."); }
    }
    state.busy = false;
  }
  async function runAgent() {
    for (let step = 0; step < 6; step++) {
      orb("thinking");
      const t0 = performance.now();
      const resp = await fetch("/api/turn", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: state.messages, tools: state.mcp.tools }),
      });
      const r = await resp.json();
      if (!resp.ok) throw new Error(r.error || resp.status);
      state.messages.push(r.message);
      const uses = r.tool_uses || [];
      log({ kind: "llm", dir: "assistant", what: uses.length ? "tool_use " + uses.map((u) => u.name).join(", ") : "reply",
        info: `${r.model.split(".").slice(-1)[0]} · ${r.latency_ms} ms${r.text ? " · " + r.text.slice(0, 80) : ""}`,
        body: { text: r.text, tool_uses: uses, usage: r.usage, stop_reason: r.stop_reason } });
      if (!uses.length) {
        orb("");
        if (r.text) sayAssistant(r.text);
        return;
      }
      if (r.text) captionAssistant(r.text);
      const results = [];
      for (const u of uses) {
        let res;
        try {
          res = await state.mcp.callTool(u.name, u.input, "assistant");
        } catch (e) {
          res = { isError: true, content: [{ type: "text", text: String(e) }] };
        }
        await present(u.name, u.input, res);
        results.push({ toolResult: { toolUseId: u.toolUseId, status: res.isError ? "error" : "success",
          content: [{ text: (res.content || []).map((c) => c.text).join("\n") || "(no text)" }] } });
      }
      state.messages.push({ role: "user", content: results });
    }
  }
  async function present(name, args, res) {
    const tool = state.mcp.toolByName(name);
    const uri = tool && tool._meta && tool._meta.ui && tool._meta.ui.resourceUri;
    const sc = res.structuredContent || {};
    if (sc.clock) updateClock(sc.clock);
    if (uri && !res.isError) await showCard(uri, name, args, res);
    if (sc.lines && sc.lines.length) speakLines(sc.lines);
  }

  /* ------------------------------------------------------------------ demo clock */
  function updateClock(c) {
    state.clock = c;
    const d = new Date(c.local_date + "T12:00:00");
    $("clock-date").textContent = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
    $("clock-time").textContent = fmtTime(c.local_time);
    $("amb-time").textContent = fmtTime(c.local_time).replace(/ ?[AP]M/, "");
  }
  function fmtTime(hm) { const [h, m] = hm.split(":").map(Number); return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; }
  async function demoClock(body) {
    const base = state.cfg.mcp_url.replace(/\/mcp$/, "");
    const r = await fetch(base + "/demo/clock", { method: body ? "POST" : "GET",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.cfg.learner_token}` },
      body: body ? JSON.stringify(body) : undefined });
    const c = await r.json();
    updateClock(c);
    return c;
  }
  $("next-day").onclick = async () => {
    const c = await demoClock({ advance_days: 1 });
    log({ kind: "ui", dir: "demo", what: "clock +1 day", info: c.local_date });
    resetScreen();
  };
  $("reset").onclick = async () => {
    await demoClock({ reset: true });
    state.messages = [];
    log({ kind: "ui", dir: "demo", what: "learner reset", info: state.cfg.learner_token });
    resetScreen();
  };
  function resetScreen() {
    state.messages = [];
    state.card = null;
    $("card-slot").innerHTML = "";
    $("card-slot").classList.remove("has-card");
    $("cap-you").textContent = ""; $("cap-assistant").textContent = "";
    screen.classList.remove("active");
    if (state.mode === "fullscreen") setMode("inline");
  }

  /* ------------------------------------------------------------------ input */
  $("typebox").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("text").value.trim();
    if (!text) return;
    $("text").value = "";
    enqueueTurn(`[typed] ${text}`, text);
  });
  for (const b of document.querySelectorAll("#chips button")) b.onclick = () => enqueueTurn(`[voice en-US] ${b.dataset.say}`, b.dataset.say);
  for (const b of document.querySelectorAll("#mode-seg button")) b.onclick = () => setMode(b.dataset.mode);
  $("close-fs").onclick = () => setMode("inline");

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  function setupMic(btn, lang) {
    if (!SR) { btn.disabled = true; btn.title = "Web Speech API not available - use Chrome, or type below"; return; }
    let rec = null;
    btn.onclick = () => {
      if (rec) { rec.stop(); return; }
      rec = new SR();
      rec.lang = lang; rec.interimResults = true; rec.maxAlternatives = 1;
      btn.classList.add("rec"); orb("listening"); speechSynthesis && speechSynthesis.cancel();
      let finalText = "";
      rec.onresult = (ev) => {
        let interim = "";
        for (const r of ev.results) { if (r.isFinal) finalText = r[0].transcript; else interim += r[0].transcript; }
        captionYou(finalText || interim);
      };
      rec.onend = () => {
        btn.classList.remove("rec"); orb(""); rec = null;
        if (finalText) enqueueTurn(`[voice ${lang}] ${finalText}`, finalText);
      };
      rec.onerror = (e) => log({ kind: "err", what: "speech recognition", info: e.error });
      rec.start();
    };
  }
  setupMic($("mic-en"), "en-US");
  setupMic($("mic-ko"), "ko-KR");

  /* ------------------------------------------------------------------ boot */
  async function boot() {
    state.cfg = await (await fetch("/api/config")).json();
    state.mcp = new McpClient(state.cfg.mcp_url, state.cfg.learner_token, log);
    const conn = $("conn");
    try {
      const init = await state.mcp.connect();
      conn.className = "conn ok";
      $("conn-text").textContent = `${init.serverInfo.title || init.serverInfo.name} · MCP ${init.protocolVersion} · ${state.mcp.tools.length} tools`;
      $("log-meta").textContent = `session ${(state.mcp.sessionId || "stateless").slice(0, 8)} · ${init.protocolVersion}`;
      await demoClock(null);
    } catch (e) {
      conn.className = "conn err";
      $("conn-text").textContent = "MCP connection failed: " + e.message;
    }
  }
  window.DKSSim = { state, enqueueTurn, setMode };
  boot();
})();
