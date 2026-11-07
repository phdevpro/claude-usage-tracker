"""Tests for the agent (transcript scanning) and the server (ingest, auth, tagging)."""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp()
# server reads its config from the environment at import time
os.environ.update(DATA_DIR=TMP, AGENT_TOKEN="agent-secret", DASHBOARD_PASSWORD="dash-secret")
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "agent")]

import claude_usage_agent as agent  # noqa: E402
import server  # noqa: E402


def line(**d):
    return json.dumps(d) + "\n"


def usage(i, o, cw=0, cr=0):
    return {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}


class AgentScanTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        agent.PROJECTS_DIR = self.dir
        agent.CACHE_FILE = self.dir / "cache.json"
        proj = self.dir / "C--Dev-IG3-app"
        (proj / "s1" / "subagents").mkdir(parents=True)
        (proj / "s1.jsonl").write_text(
            line(type="user", timestamp="2026-11-01T10:00:00Z", cwd="C:\\Dev\\IG3-app",
                 message={"content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"},
                                      {"type": "text", "text": "fix the login page"}]})
            # the same API response is logged once per content block: count it once
            + line(type="assistant", timestamp="2026-11-01T10:00:05Z", requestId="r1",
                   message={"id": "m1", "model": "claude-x", "usage": usage(10, 20, 30, 40)})
            + line(type="assistant", timestamp="2026-11-01T10:00:05Z", requestId="r1",
                   message={"id": "m1", "model": "claude-x", "usage": usage(10, 20, 30, 40)})
            + line(type="custom-title", customTitle="IG3 - Login fix", sessionId="s1"),
            encoding="utf-8")
        (proj / "s1" / "subagents" / "a1.jsonl").write_text(
            line(type="assistant", timestamp="2026-11-01T10:01:00Z", requestId="r2",
                 message={"id": "m2", "model": "claude-y", "usage": usage(1, 2)}),
            encoding="utf-8")
        # a session with no API calls is skipped
        (proj / "empty.jsonl").write_text(line(type="user", message={"content": "hi"}), encoding="utf-8")

    def test_scan_dedupes_and_merges_subagents(self):
        [s] = agent.scan_all()
        self.assertEqual(s["id"], "s1")
        self.assertEqual(s["name"], "IG3 - Login fix")
        self.assertEqual(s["project"], "C:\\Dev\\IG3-app")
        self.assertEqual(s["turns"], 2)
        self.assertEqual((s["input_tokens"], s["output_tokens"]), (11, 22))
        self.assertEqual(s["subagent_files"], 1)
        self.assertEqual(s["models"], {"claude-x": 100, "claude-y": 3})
        self.assertEqual((s["first"], s["last"]), ("2026-11-01T10:00:00Z", "2026-11-01T10:01:00Z"))

    def test_first_prompt_skips_injected_text(self):
        (self.dir / "C--Dev-IG3-app" / "s1.jsonl").write_text(
            line(type="user", message={"content": [{"type": "text", "text": "<reminder/>"},
                                                   {"type": "text", "text": "fix the login page"}]})
            + line(type="assistant", requestId="r", message={"id": "m", "usage": usage(1, 1)}),
            encoding="utf-8")
        self.assertEqual(agent.scan_all()[0]["name"], "fix the login page")


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        server.save_json(server.TAGS_FILE, {"path_rules": {"C:\\Users\\me\\Documents\\Dev": "work"}})
        with server.db() as con:
            con.execute("DELETE FROM sessions")

    def call(self, path, body=None, auth=None):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None, headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    DASH = "Basic " + base64.b64encode(b"admin:dash-secret").decode()
    AGENT = "Bearer agent-secret"

    def ingest(self, machine, sessions):
        return self.call("/api/ingest", {"machine": machine, "sessions": sessions}, self.AGENT)

    def sessions(self):
        return {s["id"]: s for s in self.call("/api/sessions", auth=self.DASH)[1]}

    @staticmethod
    def sess(sid, name, project="C:\\other", **tok):
        return {"id": sid, "name": name, "project": project, "last": "2026-11-01T10:00:00Z", "turns": 1,
                **usage(tok.get("i", 1), tok.get("o", 1))}

    def test_auth(self):
        self.assertEqual(self.call("/healthz")[0], 200)
        self.assertEqual(self.call("/api/sessions")[0], 401)
        self.assertEqual(self.call("/api/sessions", auth="Basic " + base64.b64encode(b"admin:nope").decode())[0], 401)
        self.assertEqual(self.call("/api/ingest", {"machine": "m", "sessions": []}, "Bearer wrong")[0], 401)
        self.assertEqual(self.call("/api/ingest", {"machine": "m", "sessions": []})[0], 401)
        self.assertEqual(self.call("/api/sessions", auth=self.DASH)[0], 200)

    def test_ingest_upserts_per_machine(self):
        self.ingest("laptop-a", [self.sess("s1", "one", i=5)])
        self.ingest("laptop-a", [self.sess("s1", "one", i=7)])   # re-send replaces, not adds
        self.ingest("laptop-b", [self.sess("s2", "two")])
        s = self.sessions()
        self.assertEqual(set(s), {"s1", "s2"})
        self.assertEqual(s["s1"]["input_tokens"], 7)
        self.assertEqual((s["s1"]["machine"], s["s2"]["machine"]), ("laptop-a", "laptop-b"))
        machines = {m["machine"] for m in self.call("/api/machines", auth=self.DASH)[1]}
        self.assertEqual(machines, {"laptop-a", "laptop-b"})

    def test_bad_payload_rejected(self):
        self.assertEqual(self.call("/api/ingest", {"machine": "m"}, self.AGENT)[0], 400)
        self.assertEqual(self.call("/api/tag", {"kind": "evil", "key": "x", "value": "work"}, self.DASH)[0], 400)

    def test_tags_import_export(self):
        self.ingest("m", [self.sess("s1", "Hourly job")])
        status, res = self.call("/api/tags", {
            "sessions": {"s1": "private", "bad": "maybe"},   # invalid value is dropped
            "titles": {"Hourly job": "work"},
            "title_rules": {"ACME": "work"},
            "unknown_section": {"x": "work"},                # ignored
        }, self.DASH)
        self.assertEqual(status, 200)
        self.assertEqual(res["imported"]["sessions"], 1)
        tags = self.call("/api/tags", auth=self.DASH)[1]
        # merge keeps what was there (the folder rule from setUp)
        self.assertEqual(tags["path_rules"], {"C:\\Users\\me\\Documents\\Dev": "work"})
        self.assertEqual(tags["sessions"], {"s1": "private"})
        self.assertEqual(tags["title_rules"]["ACME"], "work")
        self.assertNotIn("unknown_section", tags)
        self.assertEqual(self.sessions()["s1"]["category"], "private")

        # replace drops everything not in the upload
        self.call("/api/tags?mode=replace", {"titles": {"Hourly job": "work"}}, self.DASH)
        tags = self.call("/api/tags", auth=self.DASH)[1]
        self.assertEqual((tags["sessions"], tags["path_rules"]), ({}, {}))

        self.assertEqual(self.call("/api/tags", {"sessions": ["not", "a", "dict"]}, self.DASH)[0], 400)
        self.assertEqual(self.call("/api/tags", {"sessions": {}})[0], 401)

    def test_tag_precedence(self):
        self.ingest("m", [
            self.sess("plain", "something"),
            self.sess("kw", "IG3 - Mapper"),
            self.sess("path", "notes", "C:\\Users\\me\\Documents\\Dev\\tool"),
            self.sess("path-prefix-only", "notes2", "C:\\Users\\me\\Documents\\Devices"),
            self.sess("run1", "Hourly job"), self.sess("run2", "Hourly job"),
        ])
        s = self.sessions()
        self.assertEqual(s["plain"]["category"], "untagged")
        self.assertEqual(s["kw"]["category"], "work")                 # title keyword rule
        self.assertEqual(s["path"]["category"], "work")               # folder rule
        self.assertEqual(s["path-prefix-only"]["category"], "untagged")  # not inside that folder

        self.call("/api/tag", {"kind": "titles", "key": "Hourly job", "value": "private"}, self.DASH)
        s = self.sessions()
        self.assertEqual((s["run1"]["category"], s["run2"]["category"]), ("private", "private"))

        # a manual session tag beats the title tag and the keyword rule
        self.call("/api/tag", {"kind": "sessions", "key": "run1", "value": "work"}, self.DASH)
        self.call("/api/tag", {"kind": "sessions", "key": "kw", "value": "private"}, self.DASH)
        s = self.sessions()
        self.assertEqual((s["run1"]["category"], s["run2"]["category"], s["kw"]["category"]),
                         ("work", "private", "private"))

        # clearing a tag falls back to the rules
        self.call("/api/tag", {"kind": "sessions", "key": "kw", "value": ""}, self.DASH)
        self.assertEqual(self.sessions()["kw"]["category"], "work")


if __name__ == "__main__":
    unittest.main()
