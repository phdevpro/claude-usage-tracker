"""Claude usage server.

Agents on each machine POST their per-session token totals to /api/ingest;
this server stores them in SQLite and serves the dashboard where sessions
are tagged as work or private.

Environment:
  AGENT_TOKEN         required - shared secret agents send as "Authorization: Bearer <token>"
  DASHBOARD_PASSWORD  optional - protects the dashboard with HTTP Basic auth
  DASHBOARD_USER      optional - Basic auth user name (default "admin")
  DATA_DIR            optional - where usage.db and tags.json live (default /data)
  PORT                optional - default 8080
"""
import base64
import hmac
import json
import math
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_FILE = DATA_DIR / "usage.db"
TAGS_FILE = DATA_DIR / "tags.json"
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
DASH_USER = os.environ.get("DASHBOARD_USER", "admin")
DASH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
MAX_BODY = 20 * 1024 * 1024

_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS sessions (
            machine TEXT NOT NULL, id TEXT NOT NULL, last TEXT, data TEXT NOT NULL,
            received_at TEXT NOT NULL, PRIMARY KEY (machine, id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS machines (
            machine TEXT PRIMARY KEY, user TEXT, agent_version TEXT, last_seen TEXT, sessions INTEGER)""")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_tags():
    """tags.json: manual session tags, same-title tags, project defaults,
    title keyword rules and folder rules. Rules are edited in the file."""
    tags = load_json(TAGS_FILE, {})
    tags.setdefault("sessions", {})
    tags.setdefault("projects", {})
    tags.setdefault("titles", {})
    tags.setdefault("title_rules", {"IG3": "work"})
    tags.setdefault("path_rules", {})
    return tags


def norm_path(p):
    return (p or "").replace("/", "\\").rstrip("\\").lower()


def set_tag(kind, key, value):
    with _lock:
        tags = load_tags()
        bucket = tags.setdefault(kind, {})
        if value in ("work", "private"):
            bucket[key] = value
        else:
            bucket.pop(key, None)
        save_json(TAGS_FILE, tags)


SETTINGS_FILE = DATA_DIR / "settings.json"
# Relative price of each token type, following Anthropic's pricing ratios
# (output 5x input, cache write 1.25x, cache read 0.1x). Used only to split
# the subscription cost fairly, since cache reads are most of the raw tokens.
DEFAULT_WEIGHTS = {"input_tokens": 1, "output_tokens": 5, "cache_creation_input_tokens": 1.25,
                   "cache_read_input_tokens": 0.1}


def load_settings():
    """settings.json: currency, subscription price per month (each entry
    applies from its month until the next one) and token weights."""
    st = load_json(SETTINGS_FILE, {})
    st.setdefault("currency", "EUR")
    st.setdefault("plans", [])
    st["weights"] = {**DEFAULT_WEIGHTS, **st.get("weights", {})}
    return st


def save_settings(data):
    if not isinstance(data, dict):
        raise ValueError
    currency = str(data.get("currency") or "EUR").strip()[:8]
    plans = []
    for p in data.get("plans") or []:
        frm, cost = str(p["from"]), float(p["monthly_cost"])
        datetime.strptime(frm, "%Y-%m")  # raises ValueError on bad month
        if not math.isfinite(cost) or cost < 0:
            raise ValueError
        plans.append({"from": frm, "monthly_cost": round(cost, 2)})
    if len({p["from"] for p in plans}) != len(plans):
        raise ValueError("duplicate month")
    weights = {k: float(v) for k, v in (data.get("weights") or {}).items() if k in TOKEN_KEYS}
    if any(not math.isfinite(v) or v < 0 for v in weights.values()):
        raise ValueError
    st = {"currency": currency, "plans": sorted(plans, key=lambda p: p["from"]),
          "weights": {**DEFAULT_WEIGHTS, **weights}}
    with _lock:
        save_json(SETTINGS_FILE, st)
    return st


def plan_cost(plans, month):
    """Subscription price in force for a YYYY-MM month (0 before the first entry)."""
    cost = 0
    for p in plans:  # sorted by "from"
        if p["from"] <= month:
            cost = p["monthly_cost"]
    return cost


def add_costs(sessions, settings):
    """Split each month's subscription price across that month's sessions in
    proportion to their weighted tokens. A session counts in the month of its
    last activity."""
    w = settings["weights"]
    month_units = {}
    for s in sessions:
        s["units"] = sum(s.get(k, 0) * w[k] for k in TOKEN_KEYS)
        s["month"] = (s.get("last") or "")[:7]
        month_units[s["month"]] = month_units.get(s["month"], 0) + s["units"]
    for s in sessions:
        total = month_units[s["month"]]
        price = plan_cost(settings["plans"], s["month"]) if s["month"] else 0
        s["cost"] = round(price * s["units"] / total, 4) if total else 0
    return sessions


TAG_SECTIONS = ("sessions", "projects", "titles", "title_rules", "path_rules")


def import_tags(data, mode):
    """Merge (default) or replace tags.json with an uploaded one. Only known
    sections and "work"/"private" values are accepted."""
    if not isinstance(data, dict) or mode not in ("merge", "replace"):
        raise ValueError
    clean = {}
    for section in TAG_SECTIONS:
        items = data.get(section, {})
        if not isinstance(items, dict):
            raise ValueError(section)
        clean[section] = {str(k): v for k, v in items.items() if v in ("work", "private")}
    with _lock:
        tags = load_tags() if mode == "merge" else {}
        for section in TAG_SECTIONS:
            tags.setdefault(section, {}).update(clean[section])
        save_json(TAGS_FILE, tags)
    return {s: len(clean[s]) for s in TAG_SECTIONS}


def ingest(payload):
    machine = str(payload["machine"])[:100]
    sessions = payload["sessions"]
    if not machine or not isinstance(sessions, list):
        raise ValueError
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as con:
        con.executemany(
            "INSERT INTO sessions (machine, id, last, data, received_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(machine, id) DO UPDATE SET last=excluded.last, data=excluded.data, received_at=excluded.received_at",
            [(machine, str(s["id"]), s.get("last"), json.dumps(s), now) for s in sessions])
        count = con.execute("SELECT COUNT(*) FROM sessions WHERE machine=?", (machine,)).fetchone()[0]
        con.execute(
            "INSERT INTO machines (machine, user, agent_version, last_seen, sessions) VALUES (?,?,?,?,?) "
            "ON CONFLICT(machine) DO UPDATE SET user=excluded.user, agent_version=excluded.agent_version, "
            "last_seen=excluded.last_seen, sessions=excluded.sessions",
            (machine, payload.get("user"), payload.get("agent_version"), now, count))
    return len(sessions)


def all_sessions():
    tags = load_tags()
    path_rules = [(norm_path(k), k, v) for k, v in tags["path_rules"].items()]
    out = []
    with db() as con:
        rows = con.execute("SELECT machine, data FROM sessions").fetchall()
    for row in rows:
        s = json.loads(row["data"])
        s["machine"] = row["machine"]
        s["total"] = sum(s.get(k, 0) for k in TOKEN_KEYS)
        s["session_tag"] = tags["sessions"].get(s["id"])
        # one tag for every session sharing this exact title (e.g. scheduled runs)
        s["title_tag"] = tags["titles"].get(s["name"])
        # title keyword rules, e.g. "IG3" in the session title -> work
        s["rule_tag"], s["rule_keyword"] = next(
            ((v, k) for k, v in tags["title_rules"].items() if k.lower() in s["name"].lower()), (None, None))
        # folder rules: any session whose project lives under that folder
        if not s["rule_tag"]:
            proj = norm_path(s["project"])
            s["rule_tag"], s["rule_keyword"] = next(
                ((v, k) for n, k, v in path_rules if proj == n or proj.startswith(n + "\\")), (None, None))
        s["project_tag"] = tags["projects"].get(s["project"])
        # manual session tag > same-title tag > keyword/folder rule > project default
        s["category"] = s["session_tag"] or s["title_tag"] or s["rule_tag"] or s["project_tag"] or "untagged"
        out.append(s)
    out.sort(key=lambda s: s.get("last") or "", reverse=True)
    return add_costs(out, load_settings())


def machines():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM machines ORDER BY last_seen DESC")]


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeUsage/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _dashboard_ok(self):
        if not DASH_PASSWORD:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                user = pw = ""
            if hmac.compare_digest(user, DASH_USER) & hmac.compare_digest(pw, DASH_PASSWORD):
                return True
        self._send(401, '{"error":"auth required"}', headers=[("WWW-Authenticate", 'Basic realm="Claude usage"')])
        return False

    def _agent_ok(self):
        auth = self.headers.get("Authorization", "")
        if AGENT_TOKEN and hmac.compare_digest(auth, "Bearer " + AGENT_TOKEN):
            return True
        self._send(401, '{"error":"bad agent token"}')
        return False

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_BODY:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, '{"ok":true}')
        if not self._dashboard_ok():
            return
        if self.path == "/":
            self._send(200, (HERE / "static" / "index.html").read_bytes(), "text/html")
        elif self.path == "/api/sessions":
            self._send(200, json.dumps(all_sessions()))
        elif self.path == "/api/machines":
            self._send(200, json.dumps(machines()))
        elif self.path == "/api/settings":
            self._send(200, json.dumps(load_settings()))
        elif self.path == "/api/tags":
            self._send(200, json.dumps(load_tags(), indent=2, ensure_ascii=False),
                       headers=[("Content-Disposition", 'attachment; filename="tags.json"')])
        else:
            self._send(404, "{}")

    def do_POST(self):
        if self.path == "/api/ingest":
            if not self._agent_ok():
                return
            try:
                n = ingest(self._body())
            except (ValueError, KeyError, TypeError):
                return self._send(400, '{"error":"bad payload"}')
            return self._send(200, json.dumps({"ok": True, "received": n}))
        if self.path == "/api/tag":
            if not self._dashboard_ok():
                return
            try:
                body = self._body()
                if body["kind"] not in ("sessions", "projects", "titles"):
                    raise ValueError
                set_tag(body["kind"], str(body["key"]), body.get("value"))
            except (ValueError, KeyError, TypeError):
                return self._send(400, '{"error":"bad request"}')
            return self._send(200, '{"ok":true}')
        if self.path == "/api/settings":
            if not self._dashboard_ok():
                return
            try:
                st = save_settings(self._body())
            except (ValueError, KeyError, TypeError):
                return self._send(400, '{"error":"invalid settings: months must be YYYY-MM, costs and weights >= 0"}')
            return self._send(200, json.dumps(st))
        if self.path in ("/api/tags", "/api/tags?mode=replace"):
            if not self._dashboard_ok():
                return
            try:
                counts = import_tags(self._body(), "replace" if self.path.endswith("replace") else "merge")
            except (ValueError, TypeError):
                return self._send(400, '{"error":"not a valid tags.json"}')
            return self._send(200, json.dumps({"ok": True, "imported": counts}))
        self._send(404, "{}")


def main():
    if not AGENT_TOKEN:
        sys.exit("AGENT_TOKEN is not set - refusing to start (agents could not authenticate)")
    if not DASH_PASSWORD:
        print("WARNING: DASHBOARD_PASSWORD not set - dashboard is open to anyone who can reach it", flush=True)
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    print(f"Claude usage server on :{port}, data in {DATA_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
