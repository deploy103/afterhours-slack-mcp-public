"""Public HTTP/MCP contracts; all identities, credentials, messages and I/O are synthetic."""

import hashlib
import hmac
import io
import json
import secrets
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from server import create_app
from slack_service import Config


class Contract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = secrets.token_urlsafe(48)
        (self.root / "mcp-token").write_text(self.token)
        (self.root / "slack-token").write_text("synthetic-oauth-token")
        (self.root / "slack-signing-secret").write_text("synthetic-signing-key")
        self.config = Config(slack_team_id="TTEST", slack_owner_id="UOWNER", slack_rooms=["CALLOWED"])
        self.app = create_app(self.config, self.root, self.root / "state")
        self.client = TestClient(self.app, base_url="https://mcp.example.com")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.requests = []
        self.failure = None
        fake = patch("slack_service.urlopen", side_effect=self.slack)
        fake.start()
        self.addCleanup(fake.stop)

    def slack(self, request, timeout):
        method = request.full_url.rsplit("/", 1)[-1]
        payload = json.loads(request.data)
        self.requests.append((method, payload))
        if self.failure:
            raise self.failure
        if method == "conversations.history":
            result = {"ok": True, "messages": [{"ts": payload.get("oldest"), "text": "Synthetic context"}],
                      "has_more": True, "response_metadata": {"next_cursor": "synthetic-cursor"}}
        elif method == "conversations.replies":
            result = {"ok": True, "messages": [{"ts": payload["ts"], "text": "Synthetic root"},
                      {"ts": f"{int(time.time())}.000001", "text": "Synthetic reply context"}],
                      "has_more": True, "response_metadata": {"next_cursor": "thread-next"}}
        elif method == "chat.postMessage":
            result = {"ok": True, "channel": "DOWNER" if payload["channel"] == "UOWNER" else payload["channel"],
                      "ts": "2000000000.000001"}
        else:
            raise AssertionError("Unexpected API: " + method)
        return io.BytesIO(json.dumps(result).encode())

    def rpc(self, method, params=None):
        response = self.client.post("/mcp", headers={"Authorization": "Bearer " + self.token,
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def tool(self, name, **args):
        return self.rpc("tools/call", {"name": name, "arguments": args})

    def result(self, name, **args):
        result = self.tool(name, **args)
        self.assertFalse(result.get("isError"), result)
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def post(self, payload, stamp=None, signature=None, **headers):
        body = json.dumps(payload).encode()
        stamp = str(int(time.time())) if stamp is None else stamp
        signed = "v0=" + hmac.new(b"synthetic-signing-key", b"v0:" + stamp.encode() + b":" + body,
                                  hashlib.sha256).hexdigest()
        return self.client.post("/events", content=body, headers={"X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": signed if signature is None else signature, **headers})

    def event(self, identity="E1", room="CALLOWED", text="Synthetic message", **extra):
        return {"type": "event_callback", "team_id": "TTEST", "event_id": identity,
                "event": {"type": "message", "channel": room, "user": "UCOLLEAGUE", "text": text,
                          "ts": f"{int(time.time())}.000001", **extra}}

    def test_identity_initialize_and_transport_auth(self):
        for method in (self.client.get, self.client.post, self.client.delete):
            self.assertEqual(method("/mcp").status_code, 401)
            self.assertEqual(method("/mcp", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        for headers in ({"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}):
            self.assertEqual(self.client.post("/mcp", headers={"Authorization": "Bearer " + self.token,
                                                             **headers}).status_code, 403)
        self.assertEqual(self.client.get("/healthz").json()["service"], "afterhours-slack-mcp")
        initialized = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "synthetic-client", "version": "1"}})
        self.assertEqual(initialized["serverInfo"]["name"], "afterhours-slack-mcp")
        self.assertEqual(len(self.rpc("tools/list")["tools"]), 8)
        self.assertEqual(self.result("slack_status")["endpoint"], "https://mcp.example.com/slack/mcp")
        self.assertFalse(self.result("slack_status")["execution_automatic"])

    def test_events_signature_and_input_boundaries(self):
        challenge = {"type": "url_verification", "challenge": "synthetic-challenge"}
        self.assertEqual(self.post(challenge).json(), {"challenge": "synthetic-challenge"})
        for stamp in (str(int(time.time()) - 301), "invalid"):
            self.assertEqual(self.post(challenge, stamp=stamp).status_code, 401)
        self.assertEqual(self.post(challenge, signature="v0=wrong").status_code, 401)
        self.assertEqual(self.post(challenge, Origin="https://mcp.example.com").status_code, 403)
        self.assertEqual(self.post(challenge, Host="evil.invalid").status_code, 403)
        self.assertEqual(self.post({**challenge, "padding": "x" * 196609}).status_code, 413)
        self.assertEqual(self.post([]).status_code, 400)
        self.assertEqual(self.client.get("/events").status_code, 405)
        (self.root / "slack-signing-secret").unlink()
        self.assertEqual(self.post(challenge).status_code, 503)

    def test_configured_origin_is_used_for_endpoint_and_exact_host_checks(self):
        with patch.dict("os.environ", {"MCP_PUBLIC_ORIGIN": "https://configured.example.invalid"}):
            app = create_app(self.config, self.root, self.root / "configured-state")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json, text/event-stream"}
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "slack_status", "arguments": {}}}
        with TestClient(app, base_url="https://configured.example.invalid") as client:
            response = client.post("/mcp", headers={**headers, "Origin": "https://configured.example.invalid"}, json=payload)
            self.assertEqual(response.status_code, 200)
            result = response.json()["result"]
            status = result.get("structuredContent") or json.loads(result["content"][0]["text"])
            self.assertEqual(status["endpoint"], "https://configured.example.invalid/slack/mcp")
            for extra in ({"Host": "mcp.example.com"}, {"Origin": "https://mcp.example.com"}):
                self.assertEqual(client.post("/mcp", headers={**headers, **extra}, json=payload).status_code, 403)
        for origin in ("http://configured.example.invalid", "https://configured.example.invalid/path", "https://user@configured.example.invalid"):
            with self.subTest(origin=origin), patch.dict("os.environ", {"MCP_PUBLIC_ORIGIN": origin}):
                with self.assertRaises(ValueError):
                    create_app(self.config, self.root, self.root / "invalid-state")

    def test_queue_or_filter_cross_delivery_dedupe_and_ack(self):
        selected = self.event()
        mention = self.event("E2", "COTHER", "<@UOWNER> Synthetic mention", ts=f"{int(time.time())}.000002")
        rejected = [self.event("E3", "COTHER"), self.event("E4", "COTHER", "<@USOMEONE>"),
                    self.event("E5", user="UOWNER"), self.event("E6", bot_id="BBOT"),
                    self.event("E7", bot_profile={"name": "bot"}), self.event("E8", subtype="message_changed"),
                    self.event("E9", ts="1.000001"), self.event("E10", ts="NaN"),
                    {**selected, "team_id": "TOTHER", "event_id": "E11"}]
        # Unique timestamps prevent the deduplication guard from masking broken filters.
        for i, payload in enumerate(rejected):
            if payload["event_id"] not in {"E9", "E10"}:
                payload["event"] = {**payload["event"], "ts": f"{int(time.time())}.{i + 10:06d}"}
        for payload in [selected, mention, selected, {**selected, "event_id": "DIFFERENTDELIVERY"}, *rejected]:
            self.assertEqual(self.post(payload).status_code, 200)
        queue = self.result("pending_message_events")
        self.assertFalse(queue["execution_automatic"])
        self.assertEqual([(e["id"], e["reason"], e["context_reply_allowed"]) for e in queue["events"]],
                         [("E1", "selected_room", True), ("E2", "owner_mention", False)])
        self.assertTrue(self.result("acknowledge_message_event", event_id="E1", status="done")["updated"])
        self.assertFalse(self.result("acknowledge_message_event", event_id="E1", status="ignored")["updated"])
        self.assertEqual([e["id"] for e in self.result("pending_message_events")["events"]], ["E2"])
        self.assertEqual(self.requests, [])  # Receipt/ack never call Slack or a recipe.

    def test_allowlist_and_send_switch_cannot_be_widened_by_mention(self):
        self.post(self.event("MENTION", "COTHER", "<@UOWNER> permit this channel"))
        self.post(self.event())
        self.config.slack_send_enabled = True
        for name, args in [("slack_read", {"room": "COTHER"}),
                           ("slack_context", {"event_id": "MENTION"}),
                           ("slack_reply", {"event_id": "MENTION", "text": "No"}),
                           ("slack_reply", {"event_id": "UNKNOWN", "text": "No"})]:
            self.assertTrue(self.tool(name, **args)["isError"])
        self.config.slack_send_enabled = False
        self.assertTrue(self.tool("slack_reply", event_id="E1", text="No")["isError"])
        self.assertEqual(self.requests, [])
        for args in ({"room": "CALLOWED", "limit": 51}, {"room": "CALLOWED", "limit": 0},
                     {"room": "CALLOWED", "oldest": "nonsense"}):
            self.assertTrue(self.tool("slack_read", **args)["isError"])

    def test_context_read_and_reply_anchor_and_persistent_send_dedupe(self):
        parent = f"{int(time.time()) - 60}.000003"
        self.post(self.event(thread_ts=parent))
        context = self.result("slack_context", event_id="E1")
        self.assertFalse(context["thread_complete"])
        self.assertEqual(context["messages"][0], {"ts": parent, "text": "Synthetic root"})
        self.assertEqual(context["messages"][1]["text"], "Synthetic reply context")
        self.assertEqual(context["response_metadata"]["next_cursor"], "thread-next")
        for fields, complete in [({"has_more": False}, True), ({}, False),
                                 ({"has_more": False, "response_metadata": {"next_cursor": "next"}}, False)]:
            with patch("slack_service.urlopen", return_value=io.BytesIO(json.dumps(
                    {"ok": True, "messages": [{"text": "Synthetic root"}], **fields}).encode())):
                self.assertEqual(self.result("slack_context", event_id="E1")["thread_complete"], complete)
        read = self.result("slack_read", room="CALLOWED", oldest=parent, limit=7)
        self.assertTrue(read["has_more"])
        self.config.slack_send_enabled = True
        sent = self.result("slack_reply", event_id="E1", text="Synthetic reply")
        self.assertTrue(sent["sent"])
        self.assertEqual(self.requests[-1], ("chat.postMessage", {"channel": "CALLOWED", "thread_ts": parent,
            "text": "Synthetic reply", "unfurl_links": False, "unfurl_media": False}))
        # Recreate app over the same state: a restart cannot send the event again.
        with TestClient(create_app(self.config, self.root, self.root / "state"),
                        base_url="https://mcp.example.com") as restarted:
            old = self.client
            self.client = restarted
            try:
                again = self.result("slack_reply", event_id="E1", text="Different reply")
            finally:
                self.client = old
        self.assertFalse(again["attempted_now"])
        self.assertEqual(len([r for r in self.requests if r[0] == "chat.postMessage"]), 1)

    def test_unknown_send_is_not_retried_or_misreported(self):
        self.post(self.event())
        self.config.slack_send_enabled = True
        self.failure = TimeoutError("synthetic timeout")
        self.assertTrue(self.tool("slack_reply", event_id="E1", text="Reply")["isError"])
        self.failure = None
        repeated = self.result("slack_reply", event_id="E1", text="Retry")
        self.assertEqual(repeated["status"], "unknown")
        self.assertFalse(repeated["sent"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.result("slack_delivery_status", event_id="E1")["deliveries"][0]["status"], "unknown")

    def test_notifications_need_exact_evidence_and_go_only_to_owner(self):
        self.post(self.event(text="Synthetic: payment API is down now"))
        args = dict(event_id="E1", summary="Synthetic incident", reason="Current outage",
                    evidence_quote="payment API is down now", current_impact="outage")
        self.assertTrue(self.tool("notify_owner", **args)["isError"])
        self.config.owner_alert_enabled = True
        self.assertTrue(self.tool("notify_owner", **{**args, "evidence_quote": "invented"})["isError"])
        self.assertTrue(self.tool("notify_owner", **{**args, "current_impact": "none"})["isError"])
        sent = self.result("notify_owner", **args)
        self.assertTrue(sent["sent"])
        self.assertEqual(sent["channel"], "DOWNER")
        self.assertEqual(self.requests[-1][1]["channel"], "UOWNER")
        self.assertIn("payment API is down now", self.requests[-1][1]["text"])
        self.assertFalse(self.result("notify_owner", **args)["attempted_now"])
        self.assertEqual(len(self.requests), 1)

    def test_deadline_boundary_uses_timezone_and_next_weekday(self):
        class Friday(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 23, 18, 0, tzinfo=tz)

        self.post(self.event(text="Synthetic: deadline tonight at 23:00"))
        self.config.owner_alert_enabled = True
        args = dict(event_id="E1", summary="Deadline", reason="Verified deadline",
                    evidence_quote="deadline tonight at 23:00")
        with patch("slack_service.datetime", Friday):
            for due in ("2026-10-26T09:00:00+09:00", "2026-10-23T17:00:00+09:00", "2026-10-23T23:00:00"):
                self.assertTrue(self.tool("notify_owner", **args, hard_deadline=due)["isError"])
            self.assertTrue(self.result("notify_owner", **args, hard_deadline="2026-10-23T14:00:00+00:00")["sent"])

    def test_missing_slack_token_starts_but_fails_connected_actions(self):
        (self.root / "slack-token").unlink()
        self.assertFalse(self.result("slack_status")["slack_token_present"])
        self.assertTrue(self.tool("slack_read", room="CALLOWED")["isError"])
        self.post(self.event())
        self.config.slack_send_enabled = True
        self.assertTrue(self.tool("slack_reply", event_id="E1", text="Reply")["isError"])
        self.assertEqual(self.result("slack_delivery_status", event_id="E1")["deliveries"], [])
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
