/* Family Relay simulator: browser = MCP client + MCP Apps host; every utterance's audio goes to the
   speaker module (/speaker/observe on the MCP server); /api/turn = the assistant's LLM ("Alexa+ role"). */
(() => {
  const $ = (id) => document.getElementById(id);
  const screen = $("screen");
  let FAMILY = [{ name: "Dad", voice: "voice-a", lang: "en-US", color: "#7dd3fc" }];   // replaced from scenario.json at boot
  const state = {
    cfg: null, mcp: null, messages: [], mode: "inline", busy: false, queue: [],
    card: null, speech: Promise.resolve(), clock: null, who: FAMILY[0], clarity: "clear", obs: null, lastAudio: null,
  };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ------------------------------------------------------------------ log panel */
  const logList = $("log-list");
  function log(e) {
    if (e.kind === "llm" && !$("log-llm").checked) return;
    if (e.kind === "ui" && !$("log-ui").checked) return;
    const div = document.createElement("div");
    div.className = `entry ${e.kind}`;
    const tag = { mcp: "MCP", err: "ERR", llm: "LLM", ui: "UI", spk: "VOICE" }[e.kind] || e.kind;
    div.innerHTML = `<div class="line1"><span class="t">${new Date().toISOString().slice(11, 19)}</span><span class="k ${e.kind === "err" ? "mcp" : e.kind}">${tag}</span>` +
      `<span class="dir">${esc(e.dir || "")}</span><span class="what">${esc(e.what || "")}</span><span class="info">${esc(e.info || "")}</span></div>` +
      (e.body != null ? `<pre>${esc(JSON.stringify(e.body, null, 2))}</pre>` : "");
    div.addEventListener("click", () => div.classList.toggle("open"));
    logList.appendChild(div);
    logList.scrollTop = logList.scrollHeight;
  }
  $("log-clear").onclick = () => (logList.innerHTML = "");

  /* ------------------------------------------------------------------ orb, captions, speech out */
  function orb(s) { for (const o of document.querySelectorAll(".orb")) o.className = o.className.replace(/ (listening|thinking|speaking)/g, "") + (s ? " " + s : ""); }
  function captionYou(t, who) { $("cap-you").textContent = (who ? who + ": " : "") + t; screen.classList.add("active"); }
  function captionAssistant(t) { $("cap-assistant").textContent = t; screen.classList.add("active"); }
  function tts(text, lang) {
    return new Promise((resolve) => {
      if (!$("sound").checked || !window.speechSynthesis || !text) return resolve();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = lang;
      const v = speechSynthesis.getVoices().find((x) => x.lang && x.lang.toLowerCase().startsWith(lang.slice(0, 2)));
      if (v) u.voice = v;
      let done = false;
      const fin = () => { if (!done) { done = true; resolve(); } };
      u.onend = fin; u.onerror = fin;
      setTimeout(fin, 1500 + text.length * 110);
      speechSynthesis.speak(u);
    });
  }
  function playBlob(blob) {
    return new Promise((resolve) => {
      const a = new Audio(URL.createObjectURL(blob));
      a.onended = resolve; a.onerror = resolve;
      a.play().catch(resolve);
    });
  }
  async function serverTts(text, lang) {
    const t0 = performance.now();
    const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, lang }) });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    const blob = await r.blob();
    log({ kind: "spk", dir: "TTS", what: r.headers.get("X-Engine"), info: `${blob.size} B · server ${r.headers.get("X-Ms")} ms · total ${Math.round(performance.now() - t0)} ms` });
    return blob;
  }
  function say(text, lang) {
    state.speech = state.speech.then(async () => {
      orb("speaking"); captionAssistant(text);
      if (state.cfg.tts_engine !== "browser") {
        try { const b = await serverTts(text, lang); if ($("sound").checked) await playBlob(b); orb(""); return; }
        catch (e) { log({ kind: "err", what: "TTS failed - browser voice used", info: String(e) }); }
      }
      await tts(text, lang); orb("");
    });
  }
  async function serverStt(blob, lang) {
    const t0 = performance.now();
    const ctx = (state.obs && state.obs.household_names || []).join(", ");   // vocabulary hint: the family's names
    const r = await fetch("/api/stt", { method: "POST", headers: { "Content-Type": blob.type || "application/octet-stream", "X-Lang": lang, "X-Context": encodeURIComponent(ctx) }, body: blob });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    log({ kind: "spk", dir: "STT", what: d.engine, info: `"${d.text}" · ${d.lang} · audio ${d.audio_sec || "?"} s · server ${d.ms} ms · total ${Math.round(performance.now() - t0)} ms`, body: d });
    return d;
  }

  /* ------------------------------------------------------------------ MCP Apps host */
  function hostContext(toolName) {
    return { displayMode: state.mode === "fullscreen" ? "fullscreen" : "inline", availableDisplayModes: ["inline", "fullscreen"],
      theme: "dark", locale: "en-US", timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone, platform: "web",
      containerDimensions: { width: 768, maxHeight: state.mode === "fullscreen" ? 480 : 300 },
      deviceCapabilities: { touch: true, hover: false }, toolInfo: toolName ? { tool: { name: toolName } } : undefined };
  }
  function postToCard(msg) {
    if (!state.card) return;
    if (!state.card.ready) { state.card.pending.push(msg); return; }
    log({ kind: "ui", dir: "host → card", what: msg.method || "response", body: msg });
    state.card.iframe.contentWindow.postMessage(msg, "*");
  }
  async function showCard(uri, toolName, args, result) {
    if (state.mode === "voice-only") { log({ kind: "ui", dir: "host", what: "voice-only", info: `${uri} not rendered` }); return; }
    if (!state.card || state.card.uri !== uri) {
      const res = await state.mcp.readResource(uri);
      const csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'";
      const html = res.text.replace("<head>", `<head><meta http-equiv="Content-Security-Policy" content="${csp}">`);
      const slot = $("card-slot");
      slot.innerHTML = "";
      const iframe = document.createElement("iframe");
      iframe.setAttribute("sandbox", "allow-scripts");
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
    const reply = (result) => ev.source.postMessage({ jsonrpc: "2.0", id: m.id, result }, "*");
    if (m.method !== "ui/notifications/size-changed") log({ kind: "ui", dir: "card → host", what: m.method, body: m });
    switch (m.method) {
      case "ui/initialize":
        reply({ protocolVersion: "2026-01-26", hostInfo: { name: "family-relay-simulator", version: "1.0.0" },
          hostCapabilities: { serverTools: {}, openLinks: {} }, hostContext: hostContext(state.card.toolName) });
        break;
      case "ui/notifications/initialized": {
        state.card.ready = true;
        const p = state.card.pending; state.card.pending = []; p.forEach(postToCard);
        break;
      }
      case "tools/call":
        try { reply(await state.mcp.callTool(m.params.name, m.params.arguments, "card", obsMeta())); }
        catch (e) { ev.source.postMessage({ jsonrpc: "2.0", id: m.id, error: { code: -32000, message: String(e) } }, "*"); }
        break;
      case "ui/request-display-mode":
        if (state.mode !== "voice-only") setMode(m.params.mode === "fullscreen" ? "fullscreen" : "inline");
        reply({ mode: state.mode });
        break;
      default:
        if (m.id !== undefined && m.method) reply({});
    }
  });

  /* ------------------------------------------------------------------ speaker module (audio -> who) */
  function synthWav(seed) {
    // typed turns still send real audio bytes down the same path: a short tone unique to the selected voice
    const rate = 16000, n = rate * 0.6, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
    const w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
    w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
    const f = 140 + (seed.charCodeAt(seed.length - 1) % 8) * 35;
    for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.sin((2 * Math.PI * f * i) / rate) * 6000 * Math.min(1, i / 800), true);
    return new Blob([buf], { type: "audio/wav" });
  }
  async function observe(blob, lang, source, transcript) {
    const base = state.cfg.mcp_url.replace(/\/mcp$/, "");
    const headers = { "Content-Type": blob.type || "application/octet-stream", Authorization: `Bearer ${state.cfg.household_token}`,
      "X-FR-Lang": lang, "X-FR-Source": source, "X-FR-TS": String(Date.now() / 1000), "X-FR-Transcript": encodeURIComponent(transcript || "") };
    if (state.cfg.demo_mode) Object.assign(headers, { "X-FR-Demo-Voice": state.who.voice, "X-FR-Demo-Clarity": state.clarity,
      "X-FR-Demo-Name": encodeURIComponent(state.who.name) });
    log({ kind: "spk", dir: "→", what: "POST /speaker/observe", info: `${blob.size} B ${blob.type} · ${source}`, body: { headers: { ...headers, Authorization: "Bearer ***" } } });
    const r = await fetch(base + "/speaker/observe", { method: "POST", headers, body: blob });
    const obs = await r.json();
    const res = obs.result;
    log({ kind: "spk", dir: "←", what: `${res.state} ${Number(res.score).toFixed(2)}`, info: `${res.speaker_id || "-"} ${res.name || ""}${res.discarded ? " · discarded" : ""} · ${obs.module_ms} ms`, body: obs });
    state.obs = obs;
    state.lastAudio = blob;
    renderSpeaker(obs);
    refreshVoices();
    return obs;
  }
  const obsMeta = () => (state.obs ? { "dev.familyrelay/observation": state.obs.observation_id } : null);
  function renderSpeaker(obs) {
    const r = obs.result;
    $("spk-name").textContent = obs.person || (r.discarded ? "Noise" : r.speaker_id ? "Unnamed voice" : "-");
    const st = $("spk-state"); st.textContent = r.discarded ? "discarded" : r.state; st.className = "chip " + (r.discarded ? "unknown" : r.state);
    $("spk-id").textContent = r.speaker_id || "";
    const col = r.state === "confirmed" ? "var(--good)" : r.state === "tentative" ? "var(--warn)" : "var(--bad)";
    $("spk-fill").style.width = Math.round(r.score * 100) + "%"; $("spk-fill").style.background = col;
    $("spk-audio").textContent = `audio ${obs.audio_bytes} B ${obs.mime} · sha ${r.audio_sha256 || "-"} · ${r.module || ""} · ${obs.module_ms} ms`;
    $("spk-module").textContent = r.module || "";
    const settled = !!obs.person;
    $("spk-decision").innerHTML = settled
      ? `Settled as <b>${esc(obs.person)}</b>${obs.waiting ? ` · <b>${obs.waiting}</b> message${obs.waiting > 1 ? "s" : ""} to pass on` : " · nothing waiting"}`
      : r.discarded ? "Too noisy - dropped, not added to any voice. Stays silent." : r.state === "unknown" && r.new_cluster
      ? "New voice - started a new cluster. Stays silent until it is sure." : "Not sure yet - holds messages, answers normally, retries on the next utterance.";
    $("who-chip").textContent = settled ? "● " + obs.person : "";
  }
  async function refreshVoices() {
    const base = state.cfg.mcp_url.replace(/\/mcp$/, "");
    const d = await (await fetch(base + "/speaker/voices", { headers: { Authorization: `Bearer ${state.cfg.household_token}` } })).json();
    const W = 300, H = 200, colors = {};
    d.voices.forEach((v) => { const f = FAMILY.find((x) => x.name === v.name); colors[v.speaker_id] = f ? f.color : "#9aa3b5"; });
    // the module's 2-D projection can sit in a small corner of [0,1]: stretch what is there to fill the plot
    const xs = [], ys = [];
    for (const v of d.voices) if (v.center) { xs.push(v.center[0]); ys.push(v.center[1]); }
    for (const p of d.points) { xs.push(p.x); ys.push(p.y); }
    const cur = state.obs && state.obs.result.viz_xy;
    if (cur) { xs.push(cur[0]); ys.push(cur[1]); }
    const span = (a) => { const lo = Math.min(...a), hi = Math.max(...a); return [lo, Math.max(hi - lo, 1e-3)]; };
    const [x0, xw] = xs.length ? span(xs) : [0, 1], [y0, yw] = ys.length ? span(ys) : [0, 1];
    const X = (x) => 24 + ((x - x0) / xw) * (W - 48), Y = (y) => 30 + ((y - y0) / yw) * (H - 54);
    let svg = "";
    const labels = [];
    for (const v of d.voices) {
      if (!v.center) continue;
      const r = 10 + Math.min(18, v.utterances * 2);
      svg += `<circle cx="${X(v.center[0])}" cy="${Y(v.center[1])}" r="${r}" fill="${colors[v.speaker_id]}" fill-opacity="0.12" stroke="${colors[v.speaker_id]}" stroke-dasharray="4 3" stroke-opacity="0.6"/>`;
      labels.push({ x: X(v.center[0]), y: Math.max(12, Y(v.center[1]) - r - 4), t: `${v.name || "unnamed " + v.speaker_id.slice(-3)} · ${v.utterances}`, c: colors[v.speaker_id] });
    }
    labels.sort((a, b) => a.y - b.y);   // push labels apart until no two overlap (text box ~90 x 13 px)
    for (let pass = 0, moved = true; moved && pass < 20; pass++) {
      moved = false;
      for (let i = 1; i < labels.length; i++)
        for (let j = 0; j < i; j++)
          if (Math.abs(labels[i].y - labels[j].y) < 13 && Math.abs(labels[i].x - labels[j].x) < 90) { labels[i].y = labels[j].y + 13; moved = true; }
      labels.sort((a, b) => a.y - b.y);
    }
    for (const p of d.points) {
      const x = X(p.x), y = Y(p.y);
      svg += p.discarded ? `<g stroke="#6b7690" stroke-width="2"><line x1="${x - 4}" y1="${y - 4}" x2="${x + 4}" y2="${y + 4}"/><line x1="${x - 4}" y1="${y + 4}" x2="${x + 4}" y2="${y - 4}"/></g>`
        : `<circle cx="${x}" cy="${y}" r="3.5" fill="${colors[p.speaker_id] || "#999"}" fill-opacity="${p.state === "confirmed" ? 1 : 0.45}"/>`;
    }
    for (const l of labels) svg += `<text x="${l.x}" y="${Math.min(H - 18, l.y)}" fill="${l.c}" font-size="11" font-weight="700" text-anchor="middle">${esc(l.t)}</text>`;
    if (cur) svg += `<circle cx="${X(cur[0])}" cy="${Y(cur[1])}" r="8" fill="none" stroke="#ff7a59" stroke-width="2.5"/>`;
    svg += `<text x="6" y="${H - 6}" fill="#8f99b0" font-size="10">&#x2715; noise dropped: ${d.points.filter((p) => p.discarded).length} · voices: ${d.voices.length}</text>`;
    $("spk-plot").innerHTML = svg;
    $("spk-recent").innerHTML = d.recent.slice().reverse().slice(0, 6).map((o) => {
      const cls = o.discarded ? "disc" : o.state === "confirmed" ? "ok" : o.state === "tentative" ? "mid" : "no";
      return `<div class="${cls}">${esc(o.ts_label)} ${o.discarded ? "noise " : ""}${esc(o.state)} ${Number(o.score).toFixed(2)} → ${esc(o.name || o.speaker_id || "-")} · "${esc((o.transcript || "").slice(0, 46))}"</div>`;
    }).join("");
  }

  /* ------------------------------------------------------------------ assistant loop */
  function enqueue(item) { state.queue.push(item); if (!state.busy) drain(); }
  async function drain() {
    state.busy = true;
    while (state.queue.length) {
      const t = state.queue.shift();
      try {
        const blob = t.blob || (t.clip ? await (await fetch(t.clip)).blob() : synthWav(state.who.voice));
        // real speech goes through our STT module; Web Speech text is only the fallback
        if ((t.blob || t.clip) && state.cfg.stt_engine !== "browser") {
          try { const s = await serverStt(blob, t.lang); if (s.text) t.text = s.text; }
          catch (e) { log({ kind: "err", what: "STT failed - browser transcript used", info: String(e) }); }
        }
        if (!t.text) { log({ kind: "err", what: "no transcript" }); continue; }
        captionYou((t.overheard ? "(overheard) " : "") + t.text, state.who.name);
        const obs = await observe(blob, t.lang, t.source, t.text);
        if (t.overheard) {   // family talking near the device: the speaker module learns from it, the assistant stays quiet
          $("cap-assistant").textContent = "";
          log({ kind: "spk", dir: "overheard", what: "no assistant turn", info: t.text });
          continue;
        }
        // conversation memory belongs to one settled person: a different or unsure voice starts fresh, so
        // nothing said for someone else can reach it through the LLM's context
        if (!obs.person || obs.person !== state.convPerson) {
          if (state.messages.length) log({ kind: "ui", dir: "host", what: "new conversation", info: `speaker ${obs.person || "not settled"}` });
          state.messages = [];
        }
        state.convPerson = obs.person || null;
        const time = state.clock ? state.clock.local_label : "";
        const parts = [`device: ${t.source === "mic" ? "voice" : "typed"} ${t.lang}`, time];
        if (obs.person) parts.push(`speaker: ${obs.person}`, `${obs.waiting} message${obs.waiting === 1 ? "" : "s"} waiting`);
        if (obs.household_names && obs.household_names.length) parts.push(`family names: ${obs.household_names.join(", ")}`);
        state.messages.push({ role: "user", content: [{ text: `[${parts.join(" | ")}]\n${t.text}` }] });
        await runAgent(t.lang);
      } catch (e) {
        log({ kind: "err", what: "turn failed", info: String(e) });
        captionAssistant("Sorry, something went wrong.");
      }
    }
    state.busy = false;
  }
  async function blobB64(b) { const buf = new Uint8Array(await b.arrayBuffer()); let s = ""; for (const x of buf) s += String.fromCharCode(x); return btoa(s); }
  async function runAgent(lang) {
    for (let step = 0; step < 6; step++) {
      orb("thinking");
      const body = { messages: state.messages, tools: state.mcp.tools };
      if (step === 0 && state.cfg.send_audio_to_llm && state.lastAudio) body.audio = { mime: state.lastAudio.type, b64: await blobB64(state.lastAudio) };
      const resp = await fetch("/api/turn", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const r = await resp.json();
      if (!resp.ok) throw new Error(r.error || resp.status);
      state.messages.push(r.message);
      const uses = r.tool_uses || [];
      if (r.usage && r.usage.dropped_tail_chars) log({ kind: "llm", dir: r.provider, what: "dropped text after the JSON decision", info: `${r.usage.dropped_tail_chars} chars ignored` });
      log({ kind: "llm", dir: r.provider, what: uses.length ? "tool_use " + uses.map((u) => u.name).join(", ") : "reply",
        info: `${r.model} · ${r.latency_ms} ms${r.text ? " · " + r.text.slice(0, 70) : ""}`, body: { text: r.text, tool_uses: uses, usage: r.usage } });
      if (!uses.length) { orb(""); if (r.text) say(r.text, lang); return; }
      const results = [];
      for (const u of uses) {
        let res;
        try { res = await state.mcp.callTool(u.name, u.input, "assistant", obsMeta()); }
        catch (e) { res = { isError: true, content: [{ type: "text", text: String(e) }] }; }
        const tool = state.mcp.toolByName(u.name);
        const uri = tool && tool._meta && tool._meta.ui && tool._meta.ui.resourceUri;
        const sc = res.structuredContent || {};
        if (sc.clock) updateClock(sc.clock);
        const silent = u.name === "get_pending_messages" && sc.status === "nothing";  // keep the current card
        if (uri && !res.isError && !silent) await showCard(uri, u.name, u.input, res);
        results.push({ toolResult: { toolUseId: u.toolUseId, status: res.isError ? "error" : "success",
          content: [{ text: (res.content || []).map((c) => c.text).join("\n") || "(no text)" }] } });
      }
      state.messages.push({ role: "user", content: results });
    }
  }

  /* ------------------------------------------------------------------ demo clock */
  function updateClock(c) {
    state.clock = c;
    $("clock-label").textContent = c.local_label;
    $("amb-time").textContent = c.local_label.split(", ").pop().replace(/ ?[AP]M/, "");
  }
  async function demoClock(body) {
    const base = state.cfg.mcp_url.replace(/\/mcp$/, "");
    const r = await fetch(base + "/demo/clock", { method: body ? "POST" : "GET",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.cfg.household_token}` },
      body: body ? JSON.stringify(body) : undefined });
    const c = await r.json();
    updateClock(c);
    log({ kind: "ui", dir: "demo", what: "clock", info: c.local_label, body });
    return c;
  }
  for (const b of document.querySelectorAll("[data-adv]")) b.onclick = () => demoClock({ advance_minutes: Number(b.dataset.adv) });
  $("reset").onclick = async () => { await demoClock({ reset: true }); resetScreen(); state.messages = []; refreshVoices(); };
  function resetScreen() {
    state.card = null; $("card-slot").innerHTML = ""; $("card-slot").classList.remove("has-card");
    $("cap-you").textContent = ""; $("cap-assistant").textContent = ""; screen.classList.remove("active");
  }

  /* ------------------------------------------------------------------ who / clarity / input */
  function renderPeople() {
    $("people").innerHTML = FAMILY.map((f, i) => `<button data-i="${i}" class="${i ? "" : "on"}"><i style="background:${f.color}"></i>${f.name}</button>`).join("");
    for (const b of document.querySelectorAll("#people button")) b.onclick = () => pick(Number(b.dataset.i));
  }
  function pick(i) {
    state.who = FAMILY[i];
    for (const b of document.querySelectorAll("#people button")) b.classList.toggle("on", Number(b.dataset.i) === i);
    $("lang").value = state.who.lang;
  }
  function setClarity(c) { state.clarity = c; for (const b of document.querySelectorAll("#clarity button")) b.classList.toggle("on", b.dataset.c === c); }
  for (const b of document.querySelectorAll("#clarity button")) b.onclick = () => setClarity(b.dataset.c);
  for (const b of document.querySelectorAll("#mode-seg button")) b.onclick = () => setMode(b.dataset.mode);
  $("close-fs").onclick = () => setMode("inline");
  $("typebox").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("text").value.trim();
    if (!text) return;
    $("text").value = "";
    enqueue({ text, lang: $("lang").value, source: "typed" });
  });

  /* microphone: the recording itself goes to the speaker module; Web Speech gives the transcript */
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;
  $("mic").onclick = async () => {
    if (rec) { rec.stop(); return; }
    if (!navigator.mediaDevices || !window.MediaRecorder) { log({ kind: "err", what: "microphone", info: "MediaRecorder not available" }); return; }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    const mr = new MediaRecorder(stream);
    const lang = $("lang").value;
    let transcript = "";
    let sr = null;
    if (SR) {
      sr = new SR(); sr.lang = lang; sr.interimResults = true;
      sr.onresult = (ev) => { transcript = [...ev.results].map((r) => r[0].transcript).join(" "); captionYou(transcript, state.who.name); };
      sr.start();
    }
    mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    mr.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: mr.mimeType || "audio/webm" });
      $("mic").classList.remove("rec"); $("mic-label").textContent = "Hold to talk"; orb("");
      setTimeout(() => enqueue({ text: transcript, lang, source: "mic", blob }), 300);
    };
    rec = { stop: () => { mr.stop(); if (sr) sr.stop(); rec = null; } };
    mr.start();
    $("mic").classList.add("rec"); $("mic-label").textContent = "Listening... click to stop"; orb("listening");
  };

  /* ------------------------------------------------------------------ 4-person scenario */
  let SCENARIO = [];   // loaded from scenario.json (shared with the voice-clip generator)
  async function waitIdle() { while (state.busy || state.queue.length) await new Promise((r) => setTimeout(r, 200)); await state.speech; }
  async function runStep(i) {
    const s = SCENARIO[i];
    $("scenario-step").textContent = `${i + 1}/${SCENARIO.length} ${s.who}${s.note ? " - " + s.note : ""}`;
    if (s.clock) await demoClock({ set_local: s.clock });
    if (s.advance) await demoClock({ advance_minutes: s.advance });
    pick(FAMILY.findIndex((f) => f.name === s.who));
    setClarity(s.clarity || "clear");
    // with recorded family voices (static/clips/), the step is spoken audio: STT + speaker module hear the clip
    const clip = state.clips && state.clips[i] ? `/static/clips/${state.clips[i]}` : null;
    enqueue({ text: clip ? "" : s.text, lang: s.lang || "en-US", source: clip ? "mic" : "typed", clip, overheard: !!s.overheard });
    await waitIdle();
  }
  async function playScenario() {
    $("play").disabled = true;
    await demoClock({ reset: true }); resetScreen(); state.messages = []; await refreshVoices();
    for (let i = 0; i < SCENARIO.length; i++) { await runStep(i); await new Promise((r) => setTimeout(r, 900)); }
    $("play").disabled = false;
  }
  $("play").onclick = playScenario;

  /* ------------------------------------------------------------------ boot */
  async function boot() {
    state.cfg = await (await fetch("/api/config")).json();
    const sc = await (await fetch("/static/scenario.json")).json();
    FAMILY = Object.entries(sc.family).map(([name, f]) => ({ name, ...f }));
    SCENARIO = sc.steps;
    state.who = FAMILY[0];
    renderPeople();
    state.mcp = new McpClient(state.cfg.mcp_url, state.cfg.household_token, log);
    try {
      const init = await state.mcp.connect();
      $("conn").className = "conn ok";
      $("conn-text").textContent = `${init.serverInfo.title || init.serverInfo.name} · MCP ${init.protocolVersion} · ${state.mcp.tools.length} tools · LLM ${state.cfg.provider}`;
      $("log-meta").textContent = `session ${(state.mcp.sessionId || "stateless").slice(0, 8)}`;
      await demoClock(null);
      await refreshVoices();
      if (state.cfg.has_clips) state.clips = (await (await fetch("/static/clips/manifest.json")).json()).steps;
    } catch (e) {
      $("conn").className = "conn err";
      $("conn-text").textContent = "MCP connection failed: " + e.message;
    }
  }
  window.FRSim = { state, get SCENARIO() { return SCENARIO; }, runStep, playScenario, waitIdle, demoClock, resetScreen, setMode, pick, setClarity };
  boot();
})();
