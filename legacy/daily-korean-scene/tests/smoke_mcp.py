"""End-to-end smoke test of the MCP server over Streamable HTTP (no simulator, no orchestrator LLM).

Speaks raw JSON-RPC exactly like the browser client does, then also connects with the official
`mcp` SDK client as a conformance check. Grading still calls the real LLM.

    python tests/smoke_mcp.py [--url http://127.0.0.1:8790/mcp] [--learner smoke-test]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request

PROTOCOL = "2025-11-25"


class RawClient:
    def __init__(self, url: str, token: str):
        self.url, self.token, self.session, self.version, self.next_id = url, token, None, None, 1

    def _post(self, payload: dict) -> tuple[int, dict, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.token}"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")

    def request(self, method: str, params: dict | None = None) -> dict:
        rid = self.next_id
        self.next_id += 1
        status, headers, body = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if method == "initialize":
            self.session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        ctype = headers.get("content-type") or headers.get("Content-Type") or ""
        if "text/event-stream" in ctype:
            for block in body.split("\n\n"):
                data = "".join(l[5:].strip() for l in block.splitlines() if l.startswith("data:"))
                if data:
                    msg = json.loads(data)
                    if msg.get("id") == rid:
                        return msg
            raise RuntimeError(f"no response for id {rid} in SSE body: {body[:300]}")
        return json.loads(body)

    def notify(self, method: str) -> int:
        return self._post({"jsonrpc": "2.0", "method": method})[0]

    def call(self, name: str, args: dict | None = None) -> dict:
        t0 = time.time()
        msg = self.request("tools/call", {"name": name, "arguments": args or {}})
        if "error" in msg:
            raise RuntimeError(f"{name}: {msg['error']}")
        res = msg["result"]
        print(f"  [{time.time() - t0:5.1f}s] {name}{' ERROR' if res.get('isError') else ''}: {res['content'][0]['text'][:230]}")
        return res


def run_flow(url: str, learner: str) -> None:
    c = RawClient(url, learner)
    init = c.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {
        "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}},
        "clientInfo": {"name": "dks-smoke", "version": "1"}})
    c.version = init["result"]["protocolVersion"]
    print("initialize ->", c.version, init["result"]["serverInfo"], "session", c.session)
    assert c.version == PROTOCOL, c.version
    print("notifications/initialized ->", c.notify("notifications/initialized"))
    tools = c.request("tools/list")["result"]["tools"]
    print("tools/list ->", [(t["name"], (t.get("_meta") or {}).get("ui", {}).get("resourceUri")) for t in tools])
    res = c.request("resources/list")["result"]["resources"]
    print("resources/list ->", [(r["uri"], r.get("mimeType")) for r in res])
    card = c.request("resources/read", {"uri": res[0]["uri"]})["result"]["contents"][0]
    print("resources/read ->", card["uri"], card["mimeType"], len(card["text"]), "chars", card.get("_meta"))

    # reset demo state through the demo route
    base = url.rsplit("/mcp", 1)[0]
    req = urllib.request.Request(base + "/demo/clock", data=b'{"reset": true}', method="POST",
                                 headers={"Authorization": f"Bearer {learner}", "Content-Type": "application/json"})
    print("demo reset ->", json.loads(urllib.request.urlopen(req).read())["local_date"])

    print("DAY 1")
    r = c.call("start_daily_scene")["structuredContent"]
    scene_id = r["scene"]["scene_id"]
    first = True
    while r.get("prompt"):
        p = r["prompt"]
        # answer the first turn rudely-casual to exercise the LLM grader + review queue, the rest with a model answer
        answer = None
        if first:
            answer = "아이스 아메리카노 한 잔 줘" if scene_id == "cafe-order" else "이거 줘"
            first = False
        r = c.call("submit_learner_turn", {"answer_ko": answer or _model_answer(c, scene_id, p["line_id"])})["structuredContent"]
        g = r["grade"]
        print(f"     verdict={g['verdict']} by={g.get('graded_by')} corrected={g['corrected_ko']} issues={[(i['type'], i['learner_span'], i['fix_span']) for i in g['issues']]}")
        if r["status"] == "try_again":
            r = c.call("submit_learner_turn", {"answer_ko": _model_answer(c, scene_id, p["line_id"])})["structuredContent"]
    assert r["status"] == "scene_complete", r["status"]
    print("     summary", r["summary"])

    req = urllib.request.Request(base + "/demo/clock", data=b'{"advance_days": 1}', method="POST",
                                 headers={"Authorization": f"Bearer {learner}", "Content-Type": "application/json"})
    print("DAY 2 ->", json.loads(urllib.request.urlopen(req).read())["local_date"])
    q = c.call("get_review_queue")["structuredContent"]
    assert q["status"] == "review_started", q
    for card in q["cards"]:
        r = c.call("submit_review_answer", {"answer_ko": _review_answer(card)})["structuredContent"]
        print(f"     box={r['box']} next={r['next_review_day']} verdict={r['grade']['verdict']}")
    c.call("get_progress")
    c.call("browse_scene_packs")
    bad = c.call("purchase_scene_pack", {"pack": "office-life", "confirm": True})
    assert bad.get("isError"), "confirm without token must fail"
    pend = c.call("purchase_scene_pack", {"pack": "Office Life"})["structuredContent"]
    done = c.call("purchase_scene_pack", {"pack": "office-life", "confirm": True,
                                           "purchase_token": pend["purchase_token"]})["structuredContent"]
    assert done["status"] == "purchased", done
    c.call("schedule_practice", {"days": ["weekdays"], "time_local": "07:30"})
    c.call("get_progress")
    print("RAW FLOW OK")


_SCENES: dict = {}


def _model_answer(c: RawClient, scene_id: str, line_id: str) -> str:
    import pathlib
    if scene_id not in _SCENES:
        import os
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
        from dks.scenes import load_scenes
        _SCENES.update({k: v for k, v in load_scenes().items()})
    line = next(l for l in _SCENES[scene_id].lines if l["id"] == line_id)
    return line["expected"][0]


def _review_answer(card: dict) -> str:
    scene_id, line_id = card["item_id"].split(".", 1)
    return _model_answer(None, scene_id, line_id)


async def sdk_check(url: str) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            print("SDK client ->", init.protocol_version, init.server_info.name, len(tools.tools), "tools")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8790/mcp")
    ap.add_argument("--learner", default="smoke-test")
    a = ap.parse_args()
    run_flow(a.url, a.learner)
    asyncio.run(sdk_check(a.url))
