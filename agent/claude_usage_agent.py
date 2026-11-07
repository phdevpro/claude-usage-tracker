"""Claude usage agent.

Scans this machine's Claude Code transcripts (~/.claude/projects) and Cowork
tasks (the Claude desktop app's local-agent-mode-sessions), sums
token usage per session and sends the totals to the Claude usage server.
Only titles, project paths, timestamps and token counts are sent - never
the conversation content.

Config (first match wins): command-line flags, environment variables
CLAUDE_USAGE_SERVER / CLAUDE_USAGE_TOKEN / CLAUDE_USAGE_MACHINE, or an
agent.json next to this script: {"server": "...", "token": "...", "machine": "..."}

  python claude_usage_agent.py                 # send once (use with Task Scheduler / cron)
  python claude_usage_agent.py --interval 30   # keep running, send every 30 minutes
  python claude_usage_agent.py --dry-run       # scan and print, send nothing
"""
import argparse
import getpass
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

AGENT_VERSION = "1.1"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
# cache lives in the user profile, not next to the script, so a script shared
# via OneDrive doesn't share one cache between machines
CACHE_FILE = Path.home() / ".claude" / "usage-agent-cache.json"
CONFIG_FILE = Path(__file__).resolve().parent / "agent.json"
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def scan_file(path):
    """Return per-file stats. Each API response is logged once per content
    block with the same usage, so dedupe on message id + request id."""
    seen = set()
    st = {k: 0 for k in TOKEN_KEYS}
    st.update(models=defaultdict(int), first=None, last=None, cwd=None, title=None,
              custom_title=None, first_prompt=None, turns=0)
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


def cowork_roots():
    """Folders where the Claude desktop app keeps Cowork tasks."""
    home = Path.home()
    roots = [home / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions",  # macOS
             home / ".config" / "Claude" / "local-agent-mode-sessions"]                          # Linux
    for env in ("APPDATA", "LOCALAPPDATA"):                                                     # Windows
        if os.environ.get(env):
            roots.append(Path(os.environ[env]) / "Claude" / "local-agent-mode-sessions")
    if os.environ.get("LOCALAPPDATA"):
        roots += Path(os.environ["LOCALAPPDATA"], "Claude-Data").glob("*/local-agent-mode-sessions")
    seen, out = set(), []
    for r in roots:
        if r.is_dir() and r.resolve() not in seen:
            seen.add(r.resolve())
            out.append(r)
    return out


COWORK_ROOTS = None  # None = detect with cowork_roots(); tests set a list


def transcripts():
    """Yield (path, session id, is_main, project dir, cowork task dir or None)."""
    if PROJECTS_DIR.is_dir():
        for path in PROJECTS_DIR.rglob("*.jsonl"):
            rel = path.relative_to(PROJECTS_DIR).parts
            # <project>/<session>.jsonl, or <project>/<session>/subagents/<agent>.jsonl
            yield path, (rel[1] if len(rel) > 2 else path.stem), len(rel) == 2, rel[0], None
    for root in COWORK_ROOTS if COWORK_ROOTS is not None else cowork_roots():
        # Cowork: <root>/<account>/<org>/local_<id>/.claude/projects/<folder>/<id>.jsonl, same
        # format as Claude Code. A task resumed later holds several transcripts: count them as one.
        for task in root.glob("*/*/local_*"):
            proj = task / ".claude" / "projects"
            if not proj.is_dir():
                continue
            for path in proj.rglob("*.jsonl"):
                rel = path.relative_to(proj).parts
                yield path, task.name, len(rel) == 2, rel[0], task


def scan_all():
    """Aggregate every transcript (incl. subagent ones) into sessions.
    Unchanged files are served from a cache keyed on size+mtime."""
    cache = load_json(CACHE_FILE, {})
    new_cache = {}
    sessions = {}
    for path, sid, is_main, project_dir, task in transcripts():
        try:
            s = path.stat()
        except OSError:
            continue
        sig = f"{s.st_size}:{s.st_mtime_ns}"
        hit = cache.get(str(path))
        st = hit["st"] if hit and hit["sig"] == sig else scan_file(path)
        new_cache[str(path)] = {"sig": sig, "st": st}

        sess = sessions.setdefault(sid, {
            "id": sid, "project_dir": project_dir, "task": task, "cwd": None, "title": None, "custom_title": None,
            "first_prompt": None, "first": None, "last": None, "turns": 0, "models": {},
            "subagent_files": 0, **{k: 0 for k in TOKEN_KEYS}})
        if not is_main:
            sess["subagent_files"] += 1
        for k in TOKEN_KEYS + ("turns",):
            sess[k] += st[k]
        for m, v in st["models"].items():
            sess["models"][m] = sess["models"].get(m, 0) + v
        for k in ("cwd", "title", "custom_title", "first_prompt"):
            if is_main and st[k]:
                sess[k] = st[k]
            sess[k] = sess[k] or st[k]
        if st["first"]:
            sess["first"] = min(sess["first"] or st["first"], st["first"])
            sess["last"] = max(sess["last"] or st["last"], st["last"])
    try:
        save_json(CACHE_FILE, new_cache)
    except OSError:
        pass

    out = []
    for s in sessions.values():
        if not s["turns"]:
            continue
        source, project = "code", s["cwd"] or s["project_dir"]
        if s["task"]:
            # the task's metadata (next to its folder) has the title shown in the app and the
            # folder the user picked; the transcript's own cwd is a path inside the app's sandbox.
            # Only these two fields are read - not the prompt, email or anything else in there.
            meta = load_json(Path(str(s["task"]) + ".json"), {})
            folders = meta.get("userSelectedFolders") or []
            source, project = "cowork", (folders[0] if folders and isinstance(folders[0], str) else "Cowork")
            s["custom_title"] = meta.get("title") or s["custom_title"]
        out.append({
            "id": s["id"],
            "source": source,
            "project": project,
            "name": s["custom_title"] or s["title"] or s["first_prompt"]
                    or ("Untitled Cowork task" if s["task"] else s["id"][:8]),
            "first": s["first"], "last": s["last"], "turns": s["turns"],
            "models": s["models"], "subagent_files": s["subagent_files"],
            **{k: s[k] for k in TOKEN_KEYS},
        })
    return out


def send(server, token, machine, sessions):
    body = json.dumps({"machine": machine, "user": getpass.getuser(), "agent_version": AGENT_VERSION,
                       "sessions": sessions}).encode("utf-8")
    req = urllib.request.Request(server.rstrip("/") + "/api/ingest", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token,
        # Cloudflare (error 1010) blocks the default Python-urllib user agent
        "User-Agent": f"claude-usage-agent/{AGENT_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def run_once(cfg, dry_run):
    t0 = time.time()
    sessions = scan_all()
    took = time.time() - t0
    if not sessions:
        # not an error: Claude Code / Cowork just haven't been used on this machine yet
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] nothing to send yet: no Claude Code transcripts at {PROJECTS_DIR} and no Cowork tasks")
        return True
    if dry_run:
        total = sum(sum(s[k] for k in TOKEN_KEYS) for s in sessions)
        cowork = sum(1 for s in sessions if s["source"] == "cowork")
        print(f"[{cfg['machine']}] {len(sessions)} sessions ({cowork} Cowork), {total:,} tokens (scan {took:.1f}s) - dry run, nothing sent")
        return True
    try:
        res = send(cfg["server"], cfg["token"], cfg["machine"], sessions)
    except urllib.error.HTTPError as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Server rejected the upload: HTTP {e.code} {e.read()[:200]!r}", file=sys.stderr)
        return False
    except (urllib.error.URLError, OSError) as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Could not reach {cfg['server']}: {e}", file=sys.stderr)
        return False
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] sent {res.get('received')} sessions from {cfg['machine']} (scan {took:.1f}s)")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="e.g. http://my-docker-host:8080")
    ap.add_argument("--token", help="the server's AGENT_TOKEN")
    ap.add_argument("--machine", help="name shown in the dashboard (default: computer name)")
    ap.add_argument("--interval", type=float, help="keep running and send every N minutes")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", help="append output to this file (for scheduled runs with pythonw, which has no console)")
    a = ap.parse_args()
    if a.log:
        # on a machine where Claude Code never ran, ~/.claude doesn't exist yet
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(a.log, "a", encoding="utf-8", buffering=1)

    file_cfg = load_json(CONFIG_FILE, {})
    cfg = {
        "server": a.server or os.environ.get("CLAUDE_USAGE_SERVER") or file_cfg.get("server"),
        "token": a.token or os.environ.get("CLAUDE_USAGE_TOKEN") or file_cfg.get("token"),
        "machine": a.machine or os.environ.get("CLAUDE_USAGE_MACHINE") or file_cfg.get("machine") or socket.gethostname(),
    }
    if not a.dry_run and not (cfg["server"] and cfg["token"]):
        sys.exit("Set the server URL and token (--server/--token, env vars, or agent.json)")

    if not a.interval:
        sys.exit(0 if run_once(cfg, a.dry_run) else 1)
    while True:
        run_once(cfg, a.dry_run)
        time.sleep(a.interval * 60)


if __name__ == "__main__":
    main()
