"""Local AI-reply playground — ONLY test what the agent would post.

POST /chat   {"kind": "comment" | "dm", "text": "...", "history": [["user","..."], ...], "thread": "opt-id"}
              -> {"reply", "category", "personality", "source": "nvidia-nim" | "bank"}

Reuses the agent's exact reply brain (src/ai.py + src/engagement.py categorize/
fallback bank), so what you test here is what comments/DMs would actually get.
The NIM call is the same OpenAI-compatible chat-completions "messages" API the
engagement workflow uses (integrate.api.nvidia.com/v1).

Your NVIDIA key lives in config.local.json (gitignored, never sent to any
client, never returned by any endpoint). Binds 127.0.0.1 only — never expose.
Run:  python3 server.py   ->   http://127.0.0.1:8787
"""
import json
import pathlib

from http.server import BaseHTTPRequestHandler, HTTPServer

from src import ai, engagement

ROOT = pathlib.Path(__file__).resolve().parent
PORT = 8787


def load_local(key_default: str) -> tuple[str, str]:
    """(api_key, model) from config.local.json (gitignored). Model falls back
    to the agent's default. Missing file = bank-only mode, no NIM call."""
    local = ROOT / "config.local.json"
    if local.exists():
        try:
            cfg = json.loads(local.read_text(encoding="utf-8"))
            return str(cfg.get("api_key", "")).strip(), \
                str(cfg.get("model", "")).strip() or ai.MODEL
        except ValueError:
            pass
    return "", ai.MODEL


API_KEY, MODEL = load_local("")
if MODEL != ai.MODEL and MODEL:
    ai.MODEL = MODEL  # dev tool: point the agent's own client at the local model id


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict, ctype: str = "application/json"):
        data = json.dumps(body).encode() if ctype == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = ROOT / "viewer" / "index.html"
            self._send(200, page.read_bytes() if page.exists()
                       else "viewer/index.html missing".encode(), "text/html")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/chat":
            self._send(404, {"error": "not found"})
            return
        try:
            body = json.loads(self.rfile.read(int(
                self.headers.get("Content-Length", 0))) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "body must be JSON"})
            return
        kind = body.get("kind")           # "comment" | "dm"
        text = str(body.get("text", "")).strip()
        if kind not in ("comment", "dm") or not text:
            self._send(400, {"error": 'need {"kind": "comment"|"dm", "text": "..."}'})
            return

        seed = str(body.get("thread") or text)
        bank = engagement.load_bank()
        cat = engagement.categorize(bank, text)
        personality = ai.personality_for(seed, cat["key"])
        history = body.get("history")
        history = [list(h) for h in history if isinstance(h, (list, tuple))
                   and len(h) == 2] if isinstance(history, list) else None

        # Same call the engagement workflow makes; never raises — None on any failure.
        reply = ai.generate_reply(
            personality, "auto", text,
            history=history, api_key=API_KEY or None)
        source = "nvidia-nim"
        if not reply:
            source = "bank"
            field = "dm_replies" if kind == "dm" else "comment_replies"
            rotation = engagement._pick_replies(cat, field, seed)
            reply = rotation[0] if rotation else ""
            if not reply:
                self._send(502, {"error": "NIM failed and the bank is empty"})
                return
        reply = ai.tidy(reply, kind="comment" if kind == "comment" else "dm")
        self._send(200, {"reply": reply, "category": cat["key"],
                         "personality": personality, "source": source})


if __name__ == "__main__":
    note = ("AI enabled" if API_KEY and API_KEY != "PUT-YOUR-KEY-HERE"
            else "no key in config.local.json — replies come from the bank")
    s = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"AI-reply playground on http://127.0.0.1:{PORT} — {note} ({MODEL})")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
