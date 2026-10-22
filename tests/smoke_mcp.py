"""End-to-end check of the Family Relay MCP server without the LLM: raw JSON-RPC over Streamable HTTP (as the
browser does), speaker observations through /speaker/observe with the demo module, and the official SDK client.

    python tests/smoke_mcp.py [--base http://127.0.0.1:8790] [--household smoke]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import time
import urllib.parse
import urllib.request

PROTOCOL = "2025-11-25"
META = "dev.familyrelay/observation"


def wav(freq: float) -> bytes:
    rate, n = 16000, 8000
    data = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


class Client:
    def __init__(self, base: str, household: str):
        self.base, self.h, self.session, self.version, self.next_id = base, household, None, None, 1

    def _http(self, path, body: bytes, headers: dict) -> tuple[int, dict, str]:
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.h}", **headers})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()

    def rpc(self, method: str, params: dict | None = None, notify: bool = False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = self.next_id
            self.next_id += 1
        hd = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            hd["Mcp-Session-Id"] = self.session
        if self.version:
            hd["MCP-Protocol-Version"] = self.version
        status, headers, body = self._http("/mcp", json.dumps(msg).encode(), hd)
        if method == "initialize":
            self.session = headers.get("mcp-session-id")
        if notify:
            return status
        if "text/event-stream" in headers.get("content-type", ""):
            for block in body.split("\n\n"):
                data = "".join(l[5:].strip() for l in block.splitlines() if l.startswith("data:"))
                if data and json.loads(data).get("id") == msg["id"]:
                    return json.loads(data)
            raise RuntimeError("no response in SSE body")
        return json.loads(body)

    def observe(self, who: str, name: str, text: str, clarity="clear", lang="en-US") -> dict:
        hd = {"Content-Type": "audio/wav", "X-FR-Lang": lang, "X-FR-Source": "synthetic", "X-FR-TS": str(time.time()),
              "X-FR-Transcript": urllib.parse.quote(text), "X-FR-Demo-Voice": who, "X-FR-Demo-Clarity": clarity,
              "X-FR-Demo-Name": urllib.parse.quote(name)}
        o = json.loads(self._http("/speaker/observe", wav(150 + 40 * (ord(who[-1]) % 8)), hd)[2])
        r = o["result"]
        print(f"  observe {name:8s} {clarity:6s} -> {r['state']:9s} {r['score']:.2f} {r['speaker_id']} name={r['name']} "
              f"settled={o['person']} waiting={o['waiting']} audio={o['audio_bytes']}B")
        return o

    def call(self, obs: dict | None, name: str, args: dict | None = None) -> dict:
        params = {"name": name, "arguments": args or {}}
        if obs:
            params["_meta"] = {META: obs["observation_id"]}
        res = self.rpc("tools/call", params)["result"]
        print(f"  {name}: {res['content'][0]['text'][:200]}")
        return res

    def clock(self, body: dict) -> dict:
        return json.loads(self._http("/demo/clock", json.dumps(body).encode(), {"Content-Type": "application/json"})[2])


def main(base: str, household: str) -> None:
    c = Client(base, household)
    init = c.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {
        "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}},
        "clientInfo": {"name": "relay-smoke", "version": "1"}})["result"]
    c.version = init["protocolVersion"]
    assert c.version == PROTOCOL
    print("initialize ->", c.version, init["serverInfo"]["name"], "notify ->", c.rpc("notifications/initialized", notify=True))
    tools = c.rpc("tools/list")["result"]["tools"]
    print("tools ->", [(t["name"], t["_meta"]["ui"]["resourceUri"]) for t in tools])
    res = c.rpc("resources/list")["result"]["resources"]
    print("resources ->", [(r["uri"], r["mimeType"]) for r in res])
    c.clock({"reset": True})
    print("clock ->", c.clock({"set_local": "07:00"})["local_label"])

    fam = {"Dad": "voice-a", "Mom": "voice-b", "Sunhee": "voice-c", "Grandma": "voice-d"}
    for _ in range(2):  # everyday use: voices accumulate, no enrollment
        for n, v in fam.items():
            c.observe(v, n, "good morning")
    o = c.observe(fam["Dad"], "Dad", "Tell Sunhee to eat dinner")
    assert o["person"] == "Dad", "Dad should be settled on his 3rd utterance"
    c.call(o, "leave_message", {"recipient": "Sunhee", "message": "Eat dinner, there's kimchi stew in the fridge"})
    o = c.observe(fam["Mom"], "Mom", "Tell Grandma ...")
    c.call(o, "leave_message", {"recipient": "Grandma", "message": "Your doctor's appointment is at 3 pm"})

    c.clock({"set_local": "12:20"})
    o = c.observe(fam["Grandma"], "Grandma", "점심 뭐 먹을까", clarity="noisy", lang="ko-KR")
    r = c.call(o, "get_pending_messages")
    assert not r["structuredContent"]["messages"], "noisy voice must get nothing"
    o = c.observe(fam["Sunhee"], "Sunhee", "what time is it", clarity="far")
    r = c.call(o, "get_pending_messages")
    print("    far voice ->", o["result"]["state"], "messages:", len(r["structuredContent"]["messages"]))
    o = c.observe(fam["Grandma"], "Grandma", "지금 몇 시야", lang="ko-KR")
    r = c.call(o, "get_pending_messages")
    assert r["structuredContent"]["messages"], "clear Grandma must receive Mom's message"

    c.clock({"set_local": "18:05"})
    o = c.observe(fam["Sunhee"], "Sunhee", "what time is it")
    r = c.call(o, "get_pending_messages")
    assert r["structuredContent"]["messages"][0]["sender_name"] == "Dad"
    o = c.observe(fam["Sunhee"], "Sunhee", "tell him it was delicious")
    c.call(o, "reply_to_message", {"reply": "It was delicious, thanks!"})
    c.clock({"set_local": "19:30"})
    o = c.observe(fam["Dad"], "Dad", "did Sunhee eat")
    r = c.call(o, "ask_status", {"about": "Sunhee"})
    assert r["structuredContent"]["messages"][0]["reply_messages"], "Dad must hear the reply"
    c.call(o, "get_family_memory")
    c.call(o, "get_speaker_status")
    c.clock({"advance_minutes": 1440})
    o = c.observe(fam["Mom"], "Mom", "family memory")
    r = c.call(o, "get_family_memory")
    assert not r["structuredContent"]["live"], "everything fades after 24 h"
    print("RAW FLOW OK")


async def sdk_check(base: str) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(base + "/mcp") as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            print("SDK client ->", init.protocol_version, init.server_info.name, len(tools.tools), "tools")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8790")
    ap.add_argument("--household", default="smoke")
    a = ap.parse_args()
    main(a.base, a.household)
    asyncio.run(sdk_check(a.base))
