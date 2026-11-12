#!/usr/bin/env python3
"""Leash: a kill switch for AI agents, in front of the Otari gateway.

Every agent gets its own key, spending cap and tool policy. Leash watches each
request and response, and when an agent loops, overspends or tries a dangerous
action, it is cut off: blocked here and, with a master key, disabled at Otari
too, until a human releases it from the dashboard. Every action is audited.

Zero dependencies: Python 3.9+ standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULTS = {"max_usd": 1.0, "max_rpm": 30, "loop_threshold": 3, "allowed_tools": None}

# Tool-call arguments no agent may send, whatever its policy.
DANGEROUS = [
    ("recursive delete", re.compile(r"\brm\s+(?:-\w+\s+)*-\w*[rR]")),
    ("disk wipe", re.compile(r"\bmkfs\b|\bdd\s+if=.*\bof=/dev/")),
    ("database drop", re.compile(r"(?i)\b(?:drop\s+(?:table|database)|truncate\s+table)\b")),
    ("unscoped delete", re.compile(r"(?im)\bdelete\s+from\s+[\w.\"`]+\s*(?:;|$)")),
    ("pipe to shell", re.compile(r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")),
    ("force push", re.compile(r"\bgit\s+push\b.*(?:--force\b|\s-f\b)")),
    ("world-writable", re.compile(r"\bchmod\s+(?:-R\s+)?777\b")),
    ("credential access", re.compile(r"\.ssh/id_|\.aws/credentials|/etc/shadow")),
]

PRICE_IN = float(os.environ.get("LEASH_PRICE_IN", "0.15"))    # USD per 1M tokens, used
PRICE_OUT = float(os.environ.get("LEASH_PRICE_OUT", "0.60"))  # when Otari reports no cost


def arg_text(arguments):
    """Flatten tool-call arguments (a JSON string or object) into searchable text."""
    try:
        value = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        return str(arguments)
    out = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        else:
            out.append(str(v))
    walk(value)
    return "\n".join(out)


def find_danger(arguments):
    text = arg_text(arguments)
    return next((name for name, rx in DANGEROUS if rx.search(text)), None)


def tool_calls_of(message):
    calls = []
    for tc in (message or {}).get("tool_calls") or []:
        fn = tc.get("function") or {}
        calls.append((fn.get("name") or "?", fn.get("arguments") or ""))
    return calls


def similar(a, b):
    """Word overlap (Jaccard) of two argument sets; reworded retries score high."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def repeated_tool_call(messages, threshold, min_similarity=0.5):
    """(tool, count) when the agent has called one tool with the same or near-identical
    arguments `threshold` times. Real models retry with reworded arguments, so exact
    matching alone misses most loops."""
    calls = []
    for m in messages or []:
        if m.get("role") == "assistant":
            for name, args in tool_calls_of(m):
                calls.append((name, frozenset(re.findall(r"[a-z0-9]+", arg_text(args).lower()))))
    best = None
    for name, words in calls:
        count = sum(1 for n, w in calls if n == name and similar(words, w) >= min_similarity)
        if count >= threshold and (best is None or count > best[1]):
            best = (name, count)
    return best


def fingerprint(messages):
    last = (messages or [{}])[-1]
    return hashlib.sha256(json.dumps(last, sort_keys=True).encode()).hexdigest()


def cost_of(usage):
    usage = usage or {}
    if usage.get("cost_usd") is not None:
        try:
            return float(usage["cost_usd"])
        except (TypeError, ValueError):
            pass
    return (usage.get("prompt_tokens", 0) * PRICE_IN + usage.get("completion_tokens", 0) * PRICE_OUT) / 1e6


def http_json(method, url, key=None, body=None, timeout=120):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


class Agent:
    def __init__(self, name, cfg, defaults):
        policy = dict(DEFAULTS)
        policy.update(defaults)
        policy.update(cfg)
        self.name = name
        self.key = cfg["leash_key"]
        self.otari_key = cfg.get("otari_key") or os.environ.get(cfg.get("otari_key_env", ""), "")
        self.otari_key_name = cfg.get("otari_key_name", name)
        self.max_usd = float(policy["max_usd"])
        self.max_rpm = int(policy["max_rpm"])
        self.loop_threshold = int(policy["loop_threshold"])
        self.allowed_tools = policy["allowed_tools"]
        self.spent = 0.0
        self.requests = 0
        self.blocked = 0
        self.leashed = None          # reason, while the agent is cut off
        self.recent = deque()        # request times, for the rate limit
        self.last_fp = None
        self.streak = 0
        self.otari_disabled = False
        self.otari_remaining = None

    def view(self):
        return {
            "name": self.name, "status": "leashed" if self.leashed else "active", "reason": self.leashed,
            "spent_usd": round(self.spent, 6), "max_usd": self.max_usd, "requests": self.requests,
            "blocked": self.blocked, "max_rpm": self.max_rpm, "loop_threshold": self.loop_threshold,
            "allowed_tools": self.allowed_tools, "otari_disabled": self.otari_disabled,
            "otari_remaining_usd": self.otari_remaining,
        }


class Leash:
    def __init__(self, config, otari_url, master_key="", audit_path=None):
        defaults = config.get("defaults", {})
        self.agents = {n: Agent(n, c, defaults) for n, c in config["agents"].items()}
        self.otari_url = otari_url.rstrip("/")
        self.master_key = master_key
        self.audit_path = audit_path
        self.lock = threading.RLock()
        self.events = deque(maxlen=200)
        self.key_ids = {}

    # ----- audit -----
    def record(self, agent, kind, detail, **extra):
        event = dict({"t": time.time(), "agent": agent.name, "kind": kind, "detail": detail}, **extra)
        with self.lock:
            self.events.appendleft(event)
            if self.audit_path:
                with open(self.audit_path, "a") as f:
                    f.write(json.dumps(event) + "\n")
        print("[%s] %-10s %-9s %s" % (time.strftime("%H:%M:%S"), agent.name, kind, detail), flush=True)

    # ----- Otari key control -----
    def _key_id(self, agent):
        if agent.name not in self.key_ids:
            data = http_json("GET", self.otari_url + "/keys", self.master_key, timeout=10)
            items = data if isinstance(data, list) else (data.get("data") or data.get("keys") or data.get("items") or [])
            for k in items:
                if k.get("key_name") == agent.otari_key_name:
                    self.key_ids[agent.name] = k.get("id")
        return self.key_ids.get(agent.name)

    def set_otari_key(self, agent, active):
        """Disable or re-enable the agent's key at Otari, so the gateway itself refuses it."""
        if not self.master_key:
            return
        try:
            key_id = self._key_id(agent)
            if not key_id:
                return self.record(agent, "otari", "no Otari key named %r to %s" % (agent.otari_key_name, "enable" if active else "disable"))
            http_json("PATCH", "%s/keys/%s" % (self.otari_url, key_id), self.master_key, {"is_active": active}, timeout=10)
            agent.otari_disabled = not active
            self.record(agent, "otari", "Otari key %s" % ("re-enabled" if active else "disabled at the gateway"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.record(agent, "otari", "could not %s Otari key: %s" % ("enable" if active else "disable", e))

    def refresh_budget(self, agent):
        """Ask Otari what the agent's own key may still spend."""
        if not agent.otari_key:
            return
        try:
            data = http_json("GET", self.otari_url + "/key/budget", agent.otari_key, timeout=5)
            agent.otari_remaining = data.get("remaining_usd")
        except (urllib.error.URLError, OSError, ValueError):
            pass

    # ----- the leash -----
    def trip(self, agent, reason):
        with self.lock:
            if agent.leashed:
                return
            agent.leashed = reason
        self.record(agent, "tripped", reason)
        threading.Thread(target=self.set_otari_key, args=(agent, False), daemon=True).start()

    def release(self, agent):
        with self.lock:
            was = agent.leashed
            agent.leashed = None
            agent.spent = 0.0
            agent.streak = 0
            agent.last_fp = None
            agent.recent.clear()
        self.record(agent, "released", "released by a human" + (" (was: %s)" % was if was else ""))
        self.set_otari_key(agent, True)

    def authenticate(self, header):
        token = (header or "").replace("Bearer ", "", 1).strip()
        for agent in self.agents.values():
            if token and secrets.compare_digest(token, agent.key):
                return agent
        return None

    def handle(self, agent, req):
        """Run one chat request for an agent. Returns (status, body)."""
        messages = req.get("messages") or []
        now = time.time()
        with self.lock:
            if agent.leashed:
                agent.blocked += 1
                return 403, error("leashed", "Agent %r is leashed: %s. A human must release it." % (agent.name, agent.leashed))
            while agent.recent and now - agent.recent[0] > 60:
                agent.recent.popleft()
            if len(agent.recent) >= agent.max_rpm:
                agent.blocked += 1
                self.record(agent, "throttled", "over %d requests/min" % agent.max_rpm)
                return 429, error("rate_limited", "Agent %r is over %d requests per minute." % (agent.name, agent.max_rpm))
            agent.recent.append(now)

            fp = fingerprint(messages)
            agent.streak = agent.streak + 1 if fp == agent.last_fp else 1
            agent.last_fp = fp
            looped = repeated_tool_call(messages, agent.loop_threshold)
            if looped:
                reason = "loop: called %s with the same or near-identical arguments %d times" % looped
            elif agent.streak >= agent.loop_threshold:
                reason = "loop: sent the identical request %d times in a row" % agent.streak
            elif agent.spent >= agent.max_usd:
                reason = "spend cap: $%.4f of $%.2f used" % (agent.spent, agent.max_usd)
            else:
                reason = None
        if reason:
            agent.blocked += 1
            self.trip(agent, reason)
            return 403, error("leashed", "Agent %r is leashed: %s. A human must release it." % (agent.name, reason))

        body = dict(req, stream=False)
        try:
            resp = http_json("POST", self.otari_url + "/chat/completions", agent.otari_key, body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            self.record(agent, "error", "Otari HTTP %d: %s" % (e.code, detail))
            return e.code, error("otari_error", "Otari refused the request (HTTP %d)." % e.code, detail)
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.record(agent, "error", "Otari unreachable: %s" % e)
            return 502, error("otari_unreachable", "Otari gateway unreachable.", str(e))

        calls = []
        for choice in resp.get("choices") or []:
            calls.extend(tool_calls_of(choice.get("message")))
        cost = cost_of(resp.get("usage"))
        with self.lock:
            agent.spent += cost
            agent.requests += 1

        for name, args in calls:
            if agent.allowed_tools is not None and name not in agent.allowed_tools:
                violation = "tool not allowed: %s" % name
            else:
                danger = find_danger(args)
                violation = "dangerous action blocked: %s via %s" % (danger, name) if danger else None
            if violation:
                agent.blocked += 1
                self.record(agent, "blocked", violation, tool=name, args=arg_text(args)[:300], cost=cost)
                self.trip(agent, violation)
                return 403, error("leashed", "Agent %r is leashed: %s. The tool call was not delivered." % (agent.name, violation))

        summary = ", ".join("%s(%s)" % (n, arg_text(a)[:60].replace("\n", " ")) for n, a in calls) or "text answer"
        self.record(agent, "allowed", summary, cost=cost)
        if agent.spent >= agent.max_usd:
            self.trip(agent, "spend cap: $%.4f of $%.2f used" % (agent.spent, agent.max_usd))
        return 200, resp

    def state(self):
        with self.lock:
            return {"agents": [a.view() for a in self.agents.values()], "events": list(self.events),
                    "otari_url": self.otari_url, "gateway_control": bool(self.master_key)}


def error(kind, message, detail=None):
    err = {"type": kind, "message": message}
    if detail:
        err["detail"] = detail
    return {"error": err}


def make_handler(leash, admin_token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Leash/0.1"

        def log_message(self, fmt, *args):
            pass

        def send_json(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def is_admin(self):
            return secrets.compare_digest(self.headers.get("X-Leash-Admin") or "", admin_token)

        def do_GET(self):
            if self.path in ("/", "/dashboard"):
                data = (HERE / "dashboard.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/api/state":
                if not self.is_admin():
                    return self.send_json(401, error("unauthorized", "admin token required"))
                for agent in leash.agents.values():
                    leash.refresh_budget(agent)
                self.send_json(200, leash.state())
            else:
                self.send_json(404, error("not_found", "not found"))

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] in ("kill", "release"):
                if not self.is_admin():
                    return self.send_json(401, error("unauthorized", "admin token required"))
                agent = leash.agents.get(parts[2])
                if not agent:
                    return self.send_json(404, error("not_found", "no such agent"))
                if parts[3] == "kill":
                    leash.trip(agent, "killed by a human")
                else:
                    leash.release(agent)
                return self.send_json(200, agent.view())
            if self.path not in ("/v1/chat/completions", "/chat/completions"):
                return self.send_json(404, error("not_found", "not found"))
            agent = leash.authenticate(self.headers.get("Authorization"))
            if not agent:
                return self.send_json(401, error("unauthorized", "unknown agent key"))
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                return self.send_json(400, error("bad_request", "invalid JSON body"))
            status, body = leash.handle(agent, req)
            self.send_json(status, body)
    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(HERE / "leash.json"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LEASH_PORT", "8789")))
    ap.add_argument("--audit", default=str(HERE / "audit.jsonl"), help="append-only audit log")
    ap.add_argument("--mock", action="store_true", help="demo agents against a mock Otari (no setup needed)")
    args = ap.parse_args()

    otari_url = os.environ.get("OTARI_BASE_URL", "http://localhost:8000/api/v1")
    master_key = os.environ.get("OTARI_MASTER_KEY", "")
    if args.mock:
        import mock_backends
        config = mock_backends.DEMO_CONFIG
        otari_url, master_key = mock_backends.start(config)
    else:
        config = json.loads(Path(args.config).read_text())
        otari_url = config.get("otari_url", otari_url)

    leash = Leash(config, otari_url, master_key, args.audit)
    admin_token = os.environ.get("LEASH_ADMIN_TOKEN") or secrets.token_urlsafe(18)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(leash, admin_token))
    print("Leash on http://127.0.0.1:%d/v1  (agents: %s)" % (args.port, ", ".join(leash.agents)))
    print("  dashboard: http://127.0.0.1:%d/#admin=%s" % (args.port, admin_token))
    print("  gateway:   %s  (key control %s)" % (otari_url, "on" if master_key else "off: set OTARI_MASTER_KEY"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
