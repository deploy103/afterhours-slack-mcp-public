"""Public configure CLI, real generated launcher, and offline MCP boundaries."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from starlette.testclient import TestClient

from server import app_factory


REPO = Path(__file__).resolve().parent
PORT = 8002


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "private space ' $(touch BAD)"

    def configure(self, *arguments):
        return subprocess.run([sys.executable, "-B", str(REPO / "configure.py"), *map(str, arguments)],
                              cwd=self.base, capture_output=True, text=True, timeout=10)

    def create(self, *arguments):
        result = self.configure("--directory", self.root, *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn((self.root / "credentials/mcp-token").read_text().strip(), result.stdout + result.stderr)
        return result

    def environment(self):
        return dict(shlex.split(line.removeprefix("export "), comments=False)[0].split("=", 1)
                    for line in (self.root / "run.sh").read_text().splitlines() if line.startswith("export "))

    def test_private_files_safe_defaults_fresh_token_and_shell_quoting(self):
        self.create()
        for path in [self.root, *self.root.rglob("*")]:
            expected = 0o700 if path.is_dir() or path.name == "run.sh" else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, path.name)
        config = json.loads((self.root / "config.json").read_text())
        self.assertIsNone(config["slack_team_id"])
        self.assertIsNone(config["slack_owner_id"])
        self.assertEqual(config["slack_rooms"], [])
        self.assertIs(config["slack_send_enabled"], False)
        self.assertIs(config["owner_alert_enabled"], False)
        for name in ("slack-token", "slack-signing-secret"):
            self.assertEqual((self.root / "credentials" / name).read_bytes(), b"")
        token = (self.root / "credentials/mcp-token").read_text().strip()
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(token.isascii())
        other = self.base / "second"
        self.assertEqual(self.configure("--directory", other).returncode, 0)
        self.assertNotEqual(token, (other / "credentials/mcp-token").read_text().strip())
        environment = self.environment()
        self.assertEqual(environment["CREDENTIALS_DIRECTORY"], str(self.root / "credentials"))
        self.assertEqual(environment["SLACK_CONFIG"], str(self.root / "config.json"))
        self.assertEqual(environment["STATE_DIRECTORY"], str(self.root / "state"))
        self.assertEqual(environment["MCP_PUBLIC_ORIGIN"], "https://mcp.example.com")
        command = shlex.split((self.root / "run.sh").read_text().splitlines()[-1])
        self.assertEqual(command, ["exec", str(REPO / ".venv/bin/python"), "-m", "uvicorn",
                                  "server:app_factory", "--factory", "--host", "127.0.0.1", "--port", "8002",
                                  "--workers", "1", "--no-proxy-headers", "--no-access-log"])
        self.assertNotIn(token, (self.root / "run.sh").read_text())

    def test_repeated_configuration_and_symlinks_never_overwrite(self):
        self.create()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertNotEqual(self.configure("--directory", self.root).returncode, 0)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        for name, target in (("alias", self.root), ("dangling", self.base / "absent")):
            alias = self.base / name
            alias.symlink_to(target)
            self.assertNotEqual(self.configure("--directory", alias).returncode, 0)
            self.assertTrue(alias.is_symlink())
        file = self.base / "file"
        file.write_text("keep")
        self.assertNotEqual(self.configure("--directory", file).returncode, 0)
        self.assertEqual(file.read_text(), "keep")
        empty = self.base / "empty"
        empty.mkdir()
        self.assertNotEqual(self.configure("--directory", empty).returncode, 0)
        self.assertEqual(list(empty.iterdir()), [])

    def test_required_absolute_directory_and_expanduser(self):
        self.assertNotEqual(self.configure().returncode, 0)
        self.assertNotEqual(self.configure("--directory", "relative").returncode, 0)
        for path in (REPO, REPO / "bootstrap-must-not-create"):
            self.assertIn("outside the source checkout", self.configure("--directory", path).stderr)
        alias = self.base / "source-alias"
        alias.symlink_to(REPO, target_is_directory=True)
        self.assertIn("outside the source checkout",
                      self.configure("--directory", alias / "bootstrap-must-not-create").stderr)
        with patch.dict(os.environ, {"HOME": str(self.base)}):
            self.assertEqual(self.configure("--directory", "~/expanded").returncode, 0)
        self.assertTrue((self.base / "expanded/run.sh").is_file())

    def test_invalid_origins_leave_no_directory(self):
        for origin in ("http://mcp.example.com", "https://mcp.example.com:444", "https://mcp.example.com/",
                       "https://user@mcp.example.com", "https://:pass@mcp.example.com", "https://mcp.example.com/path",
                       "https://mcp.example.com?q=x", "https://mcp.example.com#x", "https://mcp.example.com?",
                       "https://mcp.example.com#", "https://", "https://mcp.example.com:bad", "https://mcp.example.com:",
                       "https://mcp.example.com\n", "https://evil\\mcp.example.com", "https://[bad]", "https://*.example.com"):
            with self.subTest(origin=origin):
                self.assertNotEqual(self.configure("--directory", self.root, "--public-origin", origin).returncode, 0)
                self.assertFalse(self.root.exists())

    def test_generated_config_has_no_provider_requests_and_disabled_sends(self):
        self.create("--public-origin", "https://own.example.invalid:443")
        token = (self.root / "credentials/mcp-token").read_text().strip()
        with patch.dict(os.environ, self.environment()), \
                patch("slack_service.urlopen", side_effect=AssertionError("provider request forbidden")) as network:
            with TestClient(app_factory(), base_url="https://own.example.invalid:443") as client:
                self.assertEqual(client.get("/mcp").status_code, 401)
                client.headers["Origin"] = "https://own.example.invalid"
                status = self.rpc(client, token, "tools/call", {"name": "slack_status", "arguments": {}})
                value = status.get("structuredContent") or json.loads(status["content"][0]["text"])
                self.assertFalse(value["slack_token_present"])
                self.assertFalse(value["reply_enabled"])
                self.assertFalse(value["owner_alert_enabled"])
                self.assertEqual(client.post("/mcp", headers={"Authorization": "Bearer " + token,
                    "Host": "mcp.example.com"}, json={}).status_code, 403)
                self.assertEqual(client.post("/mcp", headers={"Authorization": "Bearer " + token,
                    "Origin": "https://evil.invalid"}, json={}).status_code, 403)
                del client.headers["Origin"]
                self.assertEqual(client.post("/events", json={}).status_code, 503)
                for name, args in (("slack_read", {"room": "CFIXTURE"}),
                                   ("slack_reply", {"event_id": "fixture", "text": "not sent"}),
                                   ("notify_owner", {"event_id": "fixture", "summary": "x", "reason": "x", "evidence_quote": "x"})):
                    self.assertTrue(self.rpc(client, token, "tools/call", {"name": name, "arguments": args})["isError"])
                network.assert_not_called()

    def test_unset_slack_ids_block_ingest_and_provider_even_with_token(self):
        self.create()
        token = (self.root / "credentials/mcp-token").read_text().strip()
        (self.root / "credentials/slack-token").write_text("synthetic-bot-token")
        (self.root / "credentials/slack-signing-secret").write_text("synthetic-signing-secret")
        for team, owner in ((None, None), ("TFIXTURE", None), (None, "UOWNER")):
            with self.subTest(team=team, owner=owner):
                (self.root / "config.json").write_text(json.dumps({"slack_team_id": team,
                    "slack_owner_id": owner, "slack_rooms": ["CFIXTURE"],
                    "slack_send_enabled": True, "owner_alert_enabled": True}))
                with patch.dict(os.environ, self.environment()), \
                        patch("slack_service.urlopen", side_effect=AssertionError("provider request forbidden")) as network:
                    with TestClient(app_factory(), base_url="https://mcp.example.com") as client:
                        read = self.rpc(client, token, "tools/call", {"name": "slack_read", "arguments": {"room": "CFIXTURE"}})
                        self.assertTrue(read["isError"])
                        self.assertIn("unconfigured", read["content"][0]["text"])
                        stamp = str(int(time.time()))
                        body = json.dumps({"type": "event_callback", "team_id": team, "event_id": "EFIXTURE",
                            "event": {"type": "message", "channel": "CFIXTURE", "user": "UHUMAN", "text": "fixture",
                                      "ts": stamp + ".000001"}}).encode()
                        signature = "v0=" + hmac.new(b"synthetic-signing-secret", b"v0:" + stamp.encode() + b":" + body,
                                                      hashlib.sha256).hexdigest()
                        self.assertEqual(client.post("/events", content=body, headers={"X-Slack-Request-Timestamp": stamp,
                            "X-Slack-Signature": signature}).status_code, 200)
                        pending = self.rpc(client, token, "tools/call", {"name": "pending_message_events", "arguments": {}})
                        value = pending.get("structuredContent") or json.loads(pending["content"][0]["text"])
                        self.assertEqual(value["events"], [])
                        network.assert_not_called()

    def rpc(self, client, token, method, params):
        response = client.post("/mcp", headers={"Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def test_generated_run_sh_launches_real_authenticated_mcp_from_other_cwd(self):
        self.create()
        log = tempfile.TemporaryFile()
        self.addCleanup(log.close)
        process = subprocess.Popen([str(self.root / "run.sh")], cwd=self.base, stdout=log, stderr=log)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            url = f"http://127.0.0.1:{PORT}"
            deadline = time.monotonic() + 12
            while True:
                self.assertIsNone(process.poll(), "generated launcher exited before readiness")
                try:
                    with opener.open(url + "/healthz", timeout=1) as response:
                        self.assertEqual(json.load(response)["status"], "ok")
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        self.fail("generated launcher did not become ready")
                    time.sleep(0.05)
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                opener.open(url + "/mcp", timeout=2)
            self.assertEqual(rejected.exception.code, 401)
            token = (self.root / "credentials/mcp-token").read_text().strip()
            for method, params in (("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                   "clientInfo": {"name": "bootstrap-fixture", "version": "1"}}),
                                   ("tools/list", {}),
                                   ("tools/call", {"name": "slack_status", "arguments": {}})):
                request = urllib.request.Request(url + "/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1,
                    "method": method, "params": params}).encode(), headers={"Authorization": "Bearer " + token,
                    "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
                with opener.open(request, timeout=3) as response:
                    result = json.load(response)["result"]
                if method == "tools/call":
                    value = result.get("structuredContent") or json.loads(result["content"][0]["text"])
                    self.assertFalse(value["slack_token_present"])
                    self.assertFalse(value["reply_enabled"])
                    self.assertFalse(value["owner_alert_enabled"])
                else:
                    self.assertIn("serverInfo" if method == "initialize" else "tools", result)
            self.assertFalse((self.base / "BAD").exists())
            log.seek(0)
            self.assertNotIn(token.encode(), log.read())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
