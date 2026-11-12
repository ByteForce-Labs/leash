"""A mock Otari gateway for demos and tests, with four demo agents.

The mock model's behaviour is keyed off the conversation so the demo agents can
misbehave on cue: "search the docs" makes it call the same search tool every
time, "clean up" makes it run `rm -rf`, "notify" makes it call a tool the agent
is not allowed, and "big report" makes each answer expensive. The management
routes (list keys, disable a key, key budget) mirror Otari's.
"""
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MASTER_KEY = "mk-mock-master"

DEMO_CONFIG = {
    "defaults": {"max_usd": 0.05, "max_rpm": 60, "loop_threshold": 3},
    "agents": {
        "helper": {"leash_key": "lk-demo-helper", "otari_key": "tk-mock-helper"},
        "looper": {"leash_key": "lk-demo-looper", "otari_key": "tk-mock-looper"},
        "rogue": {"leash_key": "lk-demo-rogue", "otari_key": "tk-mock-rogue",
                  "allowed_tools": ["search", "run_shell"]},
        "spender": {"leash_key": "lk-demo-spender", "otari_key": "tk-mock-spender", "max_usd": 0.03},
    },
}


def tool_call(name, args):
    return {"id": "call_" + secrets.token_hex(4), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def reply(convo):
    """The mock model's answer: (message, usage)."""
    text = convo.lower()
    usage = {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160, "cost_usd": "0.000042"}
    if "big report" in text:
        usage = {"prompt_tokens": 9000, "completion_tokens": 12000, "total_tokens": 21000, "cost_usd": "0.008550"}
        return {"role": "assistant", "content": "Here is a very long report... " * 50}, usage
    if "clean up" in text:
        return {"role": "assistant", "content": None,
                "tool_calls": [tool_call("run_shell", {"cmd": "rm -rf ./build /"})]}, usage
    if "notify" in text:
        return {"role": "assistant", "content": None,
                "tool_calls": [tool_call("send_email", {"to": "everyone@company.com", "body": "Done"})]}, usage
    if "search the docs" in text:
        return {"role": "assistant", "content": None,
                "tool_calls": [tool_call("search", {"query": "otari budget api"})]}, usage
    return {"role": "assistant", "content": "Done: " + convo.strip().splitlines()[-1][:80]}, usage


def start(config):
    """Start the mock Otari on a free port. Returns (base_url, master_key)."""
    keys = {}
    for i, (name, agent) in enumerate(config["agents"].items(), 1):
        keys[agent["otari_key"]] = {"id": "key_%d" % i, "key_name": name, "is_active": True, "spent": 0.0}
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def bearer(self):
            return (self.headers.get("Authorization") or "").replace("Bearer ", "", 1)

        def body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}

        def do_GET(self):
            if self.path == "/api/v1/keys":
                if self.bearer() != MASTER_KEY:
                    return self.send(401, {"detail": "master key required"})
                return self.send(200, [{k: v for k, v in rec.items() if k != "spent"} for rec in keys.values()])
            if self.path == "/api/v1/key/budget":
                rec = keys.get(self.bearer())
                if not rec or not rec["is_active"]:
                    return self.send(401, {"detail": "invalid or disabled key"})
                return self.send(200, {"budgets_enforced": True, "remaining_usd": round(1.0 - rec["spent"], 6)})
            self.send(404, {"detail": "not found"})

        def do_PATCH(self):
            if self.bearer() != MASTER_KEY:
                return self.send(401, {"detail": "master key required"})
            key_id = self.path.rsplit("/", 1)[-1]
            body = self.body()
            with lock:
                for rec in keys.values():
                    if rec["id"] == key_id:
                        rec["is_active"] = bool(body.get("is_active", rec["is_active"]))
                        return self.send(200, {k: v for k, v in rec.items() if k != "spent"})
            self.send(404, {"detail": "no such key"})

        def do_POST(self):
            if self.path != "/api/v1/chat/completions":
                return self.send(404, {"detail": "not found"})
            rec = keys.get(self.bearer())
            if not rec or not rec["is_active"]:
                return self.send(401, {"detail": "invalid or disabled key"})
            req = self.body()
            convo = "\n".join(m.get("content") or "" for m in req.get("messages", []) if m.get("role") == "user")
            message, usage = reply(convo)
            with lock:
                rec["spent"] += float(usage["cost_usd"])
            self.send(200, {"id": "chatcmpl-mock", "object": "chat.completion", "model": req.get("model"),
                            "choices": [{"index": 0, "message": message,
                                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                            "usage": usage})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d/api/v1" % srv.server_address[1], MASTER_KEY
