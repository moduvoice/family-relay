"""MCP Apps (ui://) card documents: shared CSS + host bridge + one view per card, inlined into one HTML file."""
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent


def build_card(key: str) -> str:
    css = (_DIR / "base.css").read_text(encoding="utf-8")
    bridge = (_DIR / "bridge.js").read_text(encoding="utf-8")
    view = (_DIR / f"{key}.html").read_text(encoding="utf-8")
    return (
        "<!doctype html><html lang=\"en\" data-mode=\"inline\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        # bridge first: view scripts use window.DKS at load; the bridge only calls window.render later
        f"<style>{css}</style></head><body><script>{bridge}</script>{view}</body></html>"
    )
