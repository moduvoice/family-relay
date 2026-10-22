/* Minimal MCP client for the browser: JSON-RPC 2.0 over Streamable HTTP (spec 2025-11-25).
   POST every message to the endpoint with Accept: application/json, text/event-stream; the server answers
   with JSON or an SSE stream. Mcp-Session-Id from initialize and MCP-Protocol-Version go on later requests. */
class McpClient {
  constructor(url, token, onLog) {
    this.url = url;
    this.token = token;
    this.onLog = onLog || (() => {});
    this.sessionId = null;
    this.protocolVersion = null;
    this.serverInfo = null;
    this.serverCapabilities = null;
    this.nextId = 1;
    this.tools = [];
    this.resourceCache = new Map();
  }

  headers() {
    const h = { "Content-Type": "application/json", Accept: "application/json, text/event-stream" };
    if (this.token) h.Authorization = `Bearer ${this.token}`;
    if (this.sessionId) h["Mcp-Session-Id"] = this.sessionId;
    if (this.protocolVersion) h["MCP-Protocol-Version"] = this.protocolVersion;
    return h;
  }

  async _post(message, origin) {
    const t0 = performance.now();
    this.onLog({ kind: "mcp", dir: "→", what: message.method, info: summarize(message.params), body: message, origin });
    const resp = await fetch(this.url, { method: "POST", headers: this.headers(), body: JSON.stringify(message) });
    const ms = Math.round(performance.now() - t0);
    if (message.method === "initialize") this.sessionId = resp.headers.get("Mcp-Session-Id");
    if (message.id === undefined) {
      this.onLog({ kind: "mcp", dir: "←", what: `${resp.status} ${message.method}`, info: `${ms} ms`, body: null });
      return null;
    }
    if (!resp.ok) {
      const text = await resp.text();
      this.onLog({ kind: "err", dir: "←", what: `HTTP ${resp.status}`, info: text.slice(0, 120), body: text });
      throw new Error(`MCP HTTP ${resp.status}: ${text}`);
    }
    const ctype = resp.headers.get("Content-Type") || "";
    let reply;
    if (ctype.includes("text/event-stream")) reply = await this._readSse(resp, message.id);
    else reply = await resp.json();
    const transport = ctype.includes("text/event-stream") ? "SSE" : "JSON";
    this.onLog({
      kind: reply.error || (reply.result && reply.result.isError) ? "err" : "mcp", dir: "←",
      what: `${message.method}`, info: `${ms} ms · ${transport} · ${summarizeResult(reply)}`, body: reply,
    });
    if (reply.error) throw new Error(`${reply.error.code}: ${reply.error.message}`);
    return reply.result;
  }

  async _readSse(resp, id) {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (value) buf += dec.decode(value, { stream: true });
      let cut;
      while ((cut = buf.search(/\r?\n\r?\n/)) >= 0) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut).replace(/^\r?\n\r?\n/, "");
        const data = block.split(/\r?\n/).filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
        if (!data) continue;
        const msg = JSON.parse(data);
        if (msg.id === id && (msg.result !== undefined || msg.error !== undefined)) {
          reader.cancel().catch(() => {});
          return msg;
        }
        this.onLog({ kind: "mcp", dir: "←", what: msg.method || "message", info: "server notification", body: msg });
      }
      if (done) throw new Error("SSE stream ended without a response");
    }
  }

  request(method, params, origin) {
    return this._post({ jsonrpc: "2.0", id: this.nextId++, method, params: params || {} }, origin);
  }

  async connect() {
    const init = await this.request("initialize", {
      protocolVersion: "2025-11-25",
      capabilities: { extensions: { "io.modelcontextprotocol/ui": { mimeTypes: ["text/html;profile=mcp-app"] } } },
      clientInfo: { name: "dks-alexa-plus-simulator", title: "Alexa+ role simulator", version: "1.0.0" },
    });
    this.protocolVersion = init.protocolVersion;
    this.serverInfo = init.serverInfo;
    this.serverCapabilities = init.capabilities;
    await this._post({ jsonrpc: "2.0", method: "notifications/initialized" });
    const list = await this.request("tools/list", {});
    this.tools = list.tools;
    return init;
  }

  toolByName(name) {
    return this.tools.find((t) => t.name === name);
  }

  callTool(name, args, origin) {
    return this.request("tools/call", { name, arguments: args || {} }, origin);
  }

  async readResource(uri) {
    if (this.resourceCache.has(uri)) return this.resourceCache.get(uri);
    const r = await this.request("resources/read", { uri });
    const content = r.contents[0];
    this.resourceCache.set(uri, content);
    return content;
  }
}

function summarize(params) {
  if (!params) return "";
  if (params.name) return `${params.name} ${JSON.stringify(params.arguments || {})}`;
  if (params.uri) return params.uri;
  if (params.protocolVersion) return `protocolVersion ${params.protocolVersion}`;
  return "";
}

function summarizeResult(reply) {
  if (reply.error) return `error ${reply.error.message}`;
  const r = reply.result || {};
  if (r.tools) return `${r.tools.length} tools`;
  if (r.contents) return `${r.contents[0].mimeType} ${r.contents[0].text.length} chars`;
  if (r.protocolVersion) return `${r.serverInfo.name} ${r.protocolVersion}`;
  if (r.content) return (r.isError ? "isError " : "") + ((r.content[0] || {}).text || "").slice(0, 90);
  return "";
}
