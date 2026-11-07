"""Claude Code token tracker.

Scans the session transcripts Claude Code writes to ~/.claude/projects,
sums token usage per session, and lets you tag each session (or a whole
project) as "work" or "private" from a local web page.

Run:  python token_tracker.py            -> opens http://127.0.0.1:8765
      python token_tracker.py --csv out.csv   -> export and exit
Tags are saved in tags.json next to this script.
"""
import argparse
import csv
import json
import os
import sys
import threading
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
HERE = Path(__file__).resolve().parent
TAGS_FILE = HERE / "tags.json"
CACHE_FILE = HERE / ".scan_cache.json"
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

_lock = threading.Lock()


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def scan_file(path):
    """Return per-file stats. Each API response is logged once per content
    block with the same usage, so dedupe on message id + request id."""
    seen = set()
    st = {k: 0 for k in TOKEN_KEYS}
    st.update(models=defaultdict(int), first=None, last=None, cwd=None, title=None,
              custom_title=None, first_prompt=None, turns=0, session_id=None)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"usage"' not in line and 'itle"' not in line and '"user"' not in line and '"summary"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            ts = d.get("timestamp")
            if ts:
                st["first"] = min(st["first"] or ts, ts)
                st["last"] = max(st["last"] or ts, ts)
            st["cwd"] = st["cwd"] or d.get("cwd")
            st["session_id"] = st["session_id"] or d.get("sessionId")
            if t == "custom-title":
                st["custom_title"] = d.get("customTitle") or d.get("title") or st["custom_title"]
            elif t in ("ai-title", "summary"):
                st["title"] = d.get("aiTitle") or d.get("title") or d.get("summary") or st["title"]
            elif t == "user" and st["first_prompt"] is None and not d.get("isMeta"):
                c = (d.get("message") or {}).get("content")
                texts = [c] if isinstance(c, str) else [
                    b.get("text") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text"]
                for text in texts:
                    text = (text or "").strip()
                    # skip injected reminders / command wrappers / compaction summaries
                    if text and not text.startswith("<") and not text.startswith("This session is being continued"):
                        st["first_prompt"] = text[:160]
                        break
            elif t == "assistant":
                m = d.get("message") or {}
                u = m.get("usage")
                if not u:
                    continue
                key = (m.get("id"), d.get("requestId"))
                if key in seen:
                    continue
                seen.add(key)
                st["turns"] += 1
                tot = 0
                for k in TOKEN_KEYS:
                    v = u.get(k) or 0
                    st[k] += v
                    tot += v
                st["models"][m.get("model") or "unknown"] += tot
    st["models"] = dict(st["models"])
    return st


def scan_all():
    """Aggregate every transcript (incl. subagent ones) into sessions.
    Unchanged files are served from a cache keyed on size+mtime."""
    cache = load_json(CACHE_FILE, {})
    new_cache = {}
    sessions = {}
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            s = path.stat()
        except OSError:
            continue
        sig = f"{s.st_size}:{s.st_mtime_ns}"
        key = str(path)
        hit = cache.get(key)
        st = hit["st"] if hit and hit["sig"] == sig else scan_file(path)
        new_cache[key] = {"sig": sig, "st": st}

        rel = path.relative_to(PROJECTS_DIR).parts
        project_dir = rel[0]
        # <project>/<session>.jsonl, or <project>/<session>/subagents/<agent>.jsonl
        sid = rel[1] if len(rel) > 2 else path.stem
        sess = sessions.setdefault(sid, {
            "id": sid, "project_dir": project_dir, "cwd": None, "title": None, "custom_title": None,
            "first_prompt": None, "first": None, "last": None, "turns": 0, "models": {},
            "subagent_files": 0, **{k: 0 for k in TOKEN_KEYS}})
        if len(rel) > 2:
            sess["subagent_files"] += 1
        for k in TOKEN_KEYS + ("turns",):
            sess[k] += st[k]
        for m, v in st["models"].items():
            sess["models"][m] = sess["models"].get(m, 0) + v
        for k in ("cwd", "title", "custom_title", "first_prompt"):
            if len(rel) == 2 and st[k]:
                sess[k] = st[k]
            sess[k] = sess[k] or st[k]
        if st["first"]:
            sess["first"] = min(sess["first"] or st["first"], st["first"])
            sess["last"] = max(sess["last"] or st["last"], st["last"])
    try:
        save_json(CACHE_FILE, new_cache)
    except OSError:
        pass

    tags = load_tags()
    out = []
    for s in sessions.values():
        if not s["turns"]:
            continue
        s["project"] = s["cwd"] or s["project_dir"]
        s["name"] = s["custom_title"] or s["title"] or s["first_prompt"] or s["id"][:8]
        s["total"] = sum(s[k] for k in TOKEN_KEYS)
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
                ((v, k) for k, v in tags["path_rules"].items()
                 if proj == norm_path(k) or proj.startswith(norm_path(k) + "\\")), (None, None))
        s["project_tag"] = tags["projects"].get(s["project"])
        # manual session tag > same-title tag > title keyword rule > project default
        s["category"] = s["session_tag"] or s["title_tag"] or s["rule_tag"] or s["project_tag"] or "untagged"
        out.append(s)
    out.sort(key=lambda s: s["last"] or "", reverse=True)
    return out


def load_tags():
    """tags.json holds manual session tags, project defaults and title
    keyword rules. Edit "title_rules" in the file to add more keywords."""
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, (HERE / "token_tracker.html").read_bytes(), "text/html")
        elif self.path == "/api/sessions":
            self._send(200, json.dumps(scan_all()))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if self.path != "/api/tag":
            return self._send(404, "{}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if body["kind"] not in ("sessions", "projects", "titles"):
                raise ValueError
            set_tag(body["kind"], body["key"], body.get("value"))
        except (ValueError, KeyError, TypeError):
            return self._send(400, '{"error":"bad request"}')
        self._send(200, '{"ok":true}')


def export_csv(path):
    rows = scan_all()
    cols = ["id", "name", "project", "category", "first", "last", "turns", *TOKEN_KEYS, "total"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} sessions to {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--csv", help="export all sessions to this CSV and exit")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if not PROJECTS_DIR.is_dir():
        sys.exit(f"No transcripts found at {PROJECTS_DIR}")
    if a.csv:
        return export_csv(a.csv)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Token tracker running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
