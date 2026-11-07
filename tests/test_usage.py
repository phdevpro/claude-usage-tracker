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
        agent.COWORK_ROOTS = []  # keep the real machine's Cowork tasks out of these tests
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

    def test_cowork_tasks(self):
        root = Path(tempfile.mkdtemp()) / "local-agent-mode-sessions"
        org = root / "acct" / "org"
        task = org / "local_abc"
        proj = task / ".claude" / "projects" / "C--sandbox-outputs"
        (proj / "t2" / "subagents").mkdir(parents=True)
        # a resumed task has two transcripts plus a subagent: one session in total
        (proj / "t1.jsonl").write_text(
            line(type="user", timestamp="2026-11-02T09:00:00Z", cwd="C:\\sandbox\\outputs", message={"content": "plan the videos"})
            + line(type="assistant", timestamp="2026-11-02T09:00:10Z", requestId="r1",
                   message={"id": "c1", "model": "claude-x", "usage": usage(100, 10)}), encoding="utf-8")
        (proj / "t2.jsonl").write_text(
            line(type="assistant", timestamp="2026-11-03T09:00:00Z", requestId="r2",
                 message={"id": "c2", "model": "claude-x", "usage": usage(50, 5)}), encoding="utf-8")
        (proj / "t2" / "subagents" / "a.jsonl").write_text(
            line(type="assistant", timestamp="2026-11-03T09:05:00Z", requestId="r3",
                 message={"id": "c3", "model": "claude-y", "usage": usage(1, 1)}), encoding="utf-8")
        (org / "local_abc.json").write_text(json.dumps({
            "title": "IG3 - Video planning", "userSelectedFolders": ["C:\\Projects\\Formazione"],
            "emailAddress": "someone@example.com", "initialMessage": "secret prompt"}), encoding="utf-8")
        # a task with no metadata file and no readable prompt
        bare = org / "local_bare" / ".claude" / "projects" / "p"
        bare.mkdir(parents=True)
        (bare / "x.jsonl").write_text(line(type="assistant", timestamp="2026-11-04T09:00:00Z", requestId="r",
                                           message={"id": "b", "usage": usage(1, 1)}), encoding="utf-8")
        agent.COWORK_ROOTS = [root]
        try:
            s = {x["id"]: x for x in agent.scan_all()}
        finally:
            agent.COWORK_ROOTS = []
        self.assertEqual(s["s1"]["source"], "code")
        c = s["local_abc"]
        self.assertEqual((c["source"], c["name"], c["project"]), ("cowork", "IG3 - Video planning", "C:\\Projects\\Formazione"))
        self.assertEqual((c["turns"], c["input_tokens"], c["subagent_files"]), (3, 151, 1))
        self.assertEqual((c["first"], c["last"]), ("2026-11-02T09:00:00Z", "2026-11-03T09:05:00Z"))
        # only title and folder are taken from the metadata
        self.assertNotIn("secret prompt", json.dumps(c))
        self.assertNotIn("someone@example.com", json.dumps(c))
        self.assertEqual((s["local_bare"]["name"], s["local_bare"]["project"]), ("Untitled Cowork task", "Cowork"))

    def test_fresh_machine_without_claude_dir(self):
        # Claude Code never ran here: no ~/.claude at all, and the log lives inside it
        home = Path(tempfile.mkdtemp())
        log = home / ".claude" / "usage-agent.log"
        agent.PROJECTS_DIR = home / ".claude" / "projects"
        argv, out, err = sys.argv, sys.stdout, sys.stderr
        sys.argv = ["agent", "--server", "http://127.0.0.1:9", "--token", "t", "--log", str(log)]
        try:
            with self.assertRaises(SystemExit) as ex:
                agent.main()
        finally:
            sys.stdout.close()
            sys.argv, sys.stdout, sys.stderr = argv, out, err
        self.assertEqual(ex.exception.code, 0)
        self.assertIn("nothing to send yet", log.read_text(encoding="utf-8"))

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
        server.SETTINGS_FILE.unlink(missing_ok=True)
        with server.db() as con:
            con.execute("DELETE FROM sessions")
            con.execute("DELETE FROM machines")

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
    def sess(sid, name, project="C:\\other", last="2026-11-01T10:00:00Z", **tok):
        return {"id": sid, "name": name, "project": project, "last": last, "turns": 1,
                **usage(tok.get("i", 1), tok.get("o", 1), tok.get("cw", 0), tok.get("cr", 0))}

    def test_report_page_requires_login(self):
        req = urllib.request.Request(self.base + "/report?month=2026-11", headers={"Authorization": self.DASH})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b"size: 105mm 148mm", r.read())
        self.assertEqual(self.call("/report?month=2026-11")[0], 401)

    def test_settings_defaults_and_validation(self):
        st = self.call("/api/settings", auth=self.DASH)[1]
        self.assertEqual((st["currency"], st["plans"]), ("EUR", []))
        self.assertEqual(st["weights"]["cache_read_input_tokens"], 0.1)
        for bad in ({"plans": [{"from": "2026-13", "monthly_cost": 10}]},
                    {"plans": [{"from": "2026-01", "monthly_cost": -1}]},
                    {"plans": [{"from": "2026-01", "monthly_cost": 1}, {"from": "2026-01", "monthly_cost": 2}]},
                    {"plans": [{"from": "2026-01"}]},
                    {"weights": {"output_tokens": -5}}):
            self.assertEqual(self.call("/api/settings", bad, self.DASH)[0], 400, bad)
        self.assertEqual(self.call("/api/settings", {"plans": []})[0], 401)
        # unknown weight keys are dropped, plans come back sorted
        status, st = self.call("/api/settings", {"currency": "usd", "weights": {"bogus": 3},
            "plans": [{"from": "2026-09", "monthly_cost": 180}, {"from": "2026-01", "monthly_cost": 90}]}, self.DASH)
        self.assertEqual(status, 200)
        self.assertEqual([p["from"] for p in st["plans"]], ["2026-01", "2026-09"])
        self.assertNotIn("bogus", st["weights"])

    def test_cost_split_per_month(self):
        self.call("/api/settings", {"plans": [{"from": "2026-10", "monthly_cost": 100},
                                              {"from": "2026-11", "monthly_cost": 200}]}, self.DASH)
        self.ingest("m", [
            # October (EUR 100): 10 input units vs 2 output x5 = 10 units -> 50/50
            self.sess("oct-in", "a", last="2026-10-05T10:00:00Z", i=10, o=0),
            self.sess("oct-out", "b", last="2026-10-20T10:00:00Z", i=0, o=2),
            # November (EUR 200): 1000 cache reads x0.1 = 100 units vs 100 input -> 50/50
            self.sess("nov-cache", "c", last="2026-11-02T10:00:00Z", i=0, o=0, cr=1000),
            self.sess("nov-in", "IG3 work", last="2026-11-03T10:00:00Z", i=100, o=0),
            # before the first price entry: no cost
            self.sess("sep", "d", last="2026-09-30T10:00:00Z", i=50, o=0),
        ])
        s = self.sessions()
        self.assertEqual((s["oct-in"]["cost"], s["oct-out"]["cost"]), (50, 50))
        self.assertEqual((s["nov-cache"]["cost"], s["nov-in"]["cost"]), (100, 100))
        self.assertEqual(s["sep"]["cost"], 0)
        # each month's sessions add up to that month's price
        self.assertAlmostEqual(sum(v["cost"] for v in s.values() if v["month"] == "2026-11"), 200)

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
