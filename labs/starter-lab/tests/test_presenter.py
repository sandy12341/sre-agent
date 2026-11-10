import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from test_existing_agent import FakeAgent, VALUES, demo


START = datetime(2026, 11, 10, tzinfo=timezone.utc)
RULE = "/subscriptions/sub/resourceGroups/rg-starter-demo/providers/Microsoft.Insights/metricAlerts/alert-http-5xx-starter-demo"
TARGET = "/subscriptions/sub/resourceGroups/rg-starter-demo/providers/Microsoft.App/containerApps/api"
REAL_FAULT = demo.fault


def alert(stamp="2026-11-10T00:00:01Z", name="new-alert"):
    return {"id": "/alerts/" + name, "name": name, "properties": {"essentials": {
        "alertRule": RULE, "targetResource": TARGET, "startDateTime": stamp, "monitorCondition": "Fired",
    }}}


def message(name, text, **fields):
    return {"id": name, "text": text, "timeStamp": "2026-11-10T00:00:02Z",
            "author": {"role": "SREAgent"}, "isComplete": True, **fields}


def thread():
    return {"id": "automatic-thread", "title": "[Sev3] demo alert", "source": "Incident",
            "incidentSource": {"incidentType": "AzMonitor", "incidentId": RULE},
            "status": {"incidentStatus": {"incidentId": "new-alert"}},
            "incidentDetails": {"incidentCreatedTime": "2026-11-10T00:00:01Z",
                                "investigationStatus": "Complete"},
            "lastMessage": message("report", "Observed OutOfMemoryException in the actual cart logs.")}


class PresenterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.values = {**VALUES, "CONTAINER_APP_NAME": "api", "FRONTEND_APP_URL": "https://frontend.test"}
        self.args = demo.argparse.Namespace(
            approve_probe=True, approve_fault=True, requests=150, interval=0.5,
            wait_seconds=60, non_interactive=False, command="demo", run_record=None,
        )
        self.agent = FakeAgent()
        self.agent.arm["properties"]["incidentManagementConfiguration"] = {"type": "AzMonitor"}
        self.generated = dict(demo.generated_objects(self.values))
        self.messages = [
            message("reasoning", "HIDDEN_REASONING", messageType="Reasoning"),
            message("alert-envelope", "```incident-alert\nPRIVATE_ALERT_METADATA\n```"),
            message("query", "Querying actual workload logs", azCliExecution={"status": "Completed", "error": None}),
            thread()["lastMessage"],
        ]
        self.agent.request = Mock(side_effect=self.request)
        self.agent.collection = Mock(side_effect=lambda path: self.messages if path.endswith("/messages") else [thread()])
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))
        self.stack.enter_context(patch.object(demo, "validate_owned_workload", return_value=("sub", "rg-starter-demo", "starter-demo")))
        clock = self.stack.enter_context(patch.object(demo, "datetime", wraps=datetime))
        clock.now.return_value = START
        self.stack.enter_context(patch.object(demo, "az_json", return_value={"properties": {
            "enabled": True, "windowSize": "PT5M", "evaluationFrequency": "PT1M", "scopes": [TARGET],
            "criteria": {"allOf": [{
                "metricName": "Requests", "operator": "GreaterThan", "threshold": 5,
                "timeAggregation": "Total", "dimensions": [{
                    "name": "statusCodeCategory", "operator": "Include", "values": ["5xx"],
                }],
            }]},
        }}))
        self.alerts = self.stack.enter_context(patch.object(demo, "demo_alerts", side_effect=[[], [alert()]]))
        self.probe = self.stack.enter_context(patch.object(demo, "probe"))
        self.fault = self.stack.enter_context(patch.object(demo, "fault", return_value={"requests": 73, "failures": 6}))
        self.prompt = self.stack.enter_context(patch("builtins.input", return_value=""))
        self.reset = self.stack.enter_context(patch.object(demo, "reset"))

    def request(self, method, path, body=None):
        self.assertEqual(method, "GET")
        return thread() if path == "/api/v1/threads/automatic-thread" else self.generated[path]

    def run_demo(self):
        return demo.present_demo(self.agent, self.values, self.state, self.args)

    def test_guided_flow_shows_failure_real_handoff_and_diagnosis_without_writes(self):
        result = self.run_demo()
        self.assertEqual(result["status"], "diagnosis_complete")
        self.assertEqual(result["threadId"], "automatic-thread")
        self.assertEqual(result["completedToolEvents"], 1)
        self.fault.assert_called_once_with(self.values, True, 147, 0.5)
        self.assertEqual(self.prompt.call_count, 3)
        self.reset.assert_not_called()
        output = self.output.getvalue()
        for expected in ("[1/5] HEALTHY", "[2/5] FAILURE", "[3/5] DETECTION",
                         "[4/5] SRE AT WORK", "[5/5] DIAGNOSIS", "Querying actual workload logs",
                         "OutOfMemoryException", "https://frontend.test", "https://portal.azure.com/"):
            self.assertIn(expected, output)
        self.assertNotIn("HIDDEN_REASONING", output)
        self.assertNotIn("PRIVATE_ALERT_METADATA", output)
        record = json.loads(next(self.state.glob("demo-run-*.json")).read_text())
        self.assertNotIn("HIDDEN_REASONING", json.dumps(record))
        self.assertEqual(record["maxCartPosts"], 150)
        self.assertEqual(record["healthyCartPosts"], 3)
        self.assertTrue(all(call.args[0] == "GET" for call in self.agent.request.call_args_list))

    def test_non_interactive_rehearsal_does_not_prompt(self):
        self.args.non_interactive = True
        self.run_demo()
        self.prompt.assert_not_called()

    def test_read_only_watch_resumes_existing_run_without_any_cart_traffic(self):
        self.args.command = "watch-demo"
        self.args.approve_probe = self.args.approve_fault = False
        self.args.run_record = "demo-run-existing.json"
        demo.write_json(self.state / self.args.run_record, {
            "started": START.isoformat(), "faultStarted": START.isoformat(), "status": "failed",
            "error": "previous console encoding error", "maxCartPosts": 150, "healthyCartPosts": 3,
            "fault": {"requests": 72, "failures": 6}, "alertId": alert()["id"],
            "events": [{"id": "query", "time": "2026-11-10T00:00:02Z", "text": "Querying actual workload logs"}],
        })
        self.alerts.side_effect = None
        self.alerts.return_value = [alert()]
        result = self.run_demo()
        self.assertEqual(result["status"], "diagnosis_complete")
        self.assertEqual(result["fault"]["requests"], 72)
        self.assertEqual(result["completedToolEvents"], 1)
        self.assertEqual(result["watchResumes"][0]["priorStatus"], "failed")
        self.probe.assert_not_called()
        self.fault.assert_not_called()
        self.reset.assert_not_called()
        self.prompt.assert_not_called()
        self.assertIn("READ-ONLY RESUME", self.output.getvalue())

    def test_watch_rejects_outside_state_paths_and_unverified_traffic(self):
        self.args.command = "watch-demo"
        self.args.run_record = "../demo-run-outside.json"
        with self.assertRaisesRegex(ValueError, "inside --state-dir"):
            self.run_demo()
        self.args.run_record = "demo-run-invalid.json"
        demo.write_json(self.state / self.args.run_record, {"fault": None})
        with self.assertRaisesRegex(ValueError, "no bounded fault"):
            self.run_demo()
        self.probe.assert_not_called()
        self.fault.assert_not_called()

    def test_console_handles_actual_sre_unicode_on_windows_encodings(self):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252")
        with patch.object(demo.sys, "stdout", console):
            demo.configure_console()
            print("\u2705 diagnosis \u2014 evidence")
            console.flush()
        self.assertIn("\u2705 diagnosis".encode("utf-8"), raw.getvalue())
        console.detach()

    def test_approval_and_budget_are_checked_before_traffic(self):
        for update in ({"approve_fault": False}, {"approve_probe": False}, {"requests": 151},
                       {"requests": 8}, {"interval": 0.1}, {"wait_seconds": 1201}):
            with self.subTest(update=update):
                args = demo.argparse.Namespace(**{**vars(self.args), **update})
                with self.assertRaises(ValueError):
                    demo.present_demo(self.agent, self.values, self.state, args)
        self.probe.assert_not_called()
        self.fault.assert_not_called()

    def test_active_old_alert_blocks_a_new_fault(self):
        self.alerts.side_effect = None
        self.alerts.return_value = [alert("2026-11-09T00:00:00Z")]
        with self.assertRaisesRegex(ValueError, "previous demo alert"):
            self.run_demo()
        self.probe.assert_not_called()
        self.fault.assert_not_called()

    def test_no_observed_failures_is_not_a_successful_demo(self):
        self.fault.return_value = {"requests": 147, "failures": 0}
        with self.assertRaisesRegex(RuntimeError, "Six server errors were not observed"):
            self.run_demo()
        record = json.loads(next(self.state.glob("demo-run-*.json")).read_text())
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("alertId", record)
        self.assertNotIn("DEMO COMPLETE", self.output.getvalue())
        self.reset.assert_not_called()

    def test_an_approval_request_is_not_auto_approved(self):
        self.messages.insert(0, message("approval", "Restart requested", approval={"status": "Pending"}))
        with self.assertRaisesRegex(RuntimeError, "will not answer or approve"):
            self.run_demo()
        self.assertTrue(all(call.args[0] == "GET" for call in self.agent.request.call_args_list))
        self.reset.assert_not_called()

    def test_incomplete_or_evidenceless_diagnosis_cannot_pass(self):
        for messages, status in (([thread()["lastMessage"]], "Complete"), (self.messages, "Investigating")):
            with self.subTest(status=status):
                current = thread()
                current["incidentDetails"]["investigationStatus"] = status
                self.agent.request.side_effect = lambda method, path, body=None: (
                    current if path == "/api/v1/threads/automatic-thread" else self.generated[path]
                )
                self.agent.collection.side_effect = lambda path: messages if path.endswith("/messages") else [current]
                self.alerts.side_effect = [[], [alert()]]
                with patch.object(demo.time, "monotonic", side_effect=[0, 0, 0, 61]), \
                        self.assertRaisesRegex(RuntimeError, "Timed out waiting"):
                    self.run_demo()
        self.assertNotIn("DEMO COMPLETE", self.output.getvalue())

    def test_a_completed_tool_message_is_not_a_final_diagnosis(self):
        current = thread()
        current["lastMessage"] = message("memory", "Memory Search: Found 1 result",
                                         memorySearchResult={"totalResults": 1})
        self.agent.request.side_effect = lambda method, path, body=None: (
            current if path == "/api/v1/threads/automatic-thread" else self.generated[path]
        )
        with patch.object(demo.time, "monotonic", side_effect=[0, 0, 0, 61]), \
                self.assertRaisesRegex(RuntimeError, "Timed out waiting"):
            self.run_demo()
        self.assertNotIn("DEMO COMPLETE", self.output.getvalue())

    def test_network_failure_stops_and_records_failure_without_reset(self):
        self.fault.side_effect = RuntimeError("network failure")
        with self.assertRaisesRegex(RuntimeError, "network failure"):
            self.run_demo()
        self.assertEqual(json.loads(next(self.state.glob("demo-run-*.json")).read_text())["status"], "failed")
        self.reset.assert_not_called()

    def test_fresh_alert_and_incident_correlation_reject_old_or_manual_chats(self):
        self.assertIsNone(demo.new_demo_alert([alert("2026-11-09T00:00:00Z")], RULE, TARGET, START))
        self.assertIsNone(demo.new_demo_alert([alert()], RULE, TARGET + "-other", START))
        for update in ({"source": "Chat"}, {"incidentSource": {"incidentType": "ServiceNow"}},
                       {"status": {"incidentStatus": {"incidentId": "old-alert"}}}):
            with self.subTest(update=update):
                self.assertIsNone(demo.correlated_demo_thread([{**thread(), **update}], alert(), RULE, START))
        self.assertEqual(demo.correlated_demo_thread([thread()], alert(), RULE.lower(), START)["id"], "automatic-thread")
        display_name_alert = {**alert(), "name": "a friendly rule name, not the alert instance"}
        self.assertEqual(demo.correlated_demo_thread(
            [thread()], display_name_alert, RULE, START,
        )["id"], "automatic-thread")

    def test_fault_stops_at_exactly_six_errors_and_reports_actual_request_count(self):
        with patch.object(demo, "cart_request"), patch.object(demo.time, "sleep") as sleep, \
                patch.object(demo, "urlopen") as opening:
            opening.side_effect = [HTTPError("https://demo.test", 500, "failure", {}, None) for _ in range(6)]
            self.assertEqual(REAL_FAULT(self.values, True, 147, 0.5), {"requests": 6, "failures": 6})
            self.assertEqual(opening.call_count, 6)
            self.assertEqual(sleep.call_count, 5)
        self.assertIn("6/6 server errors", self.output.getvalue())

    def test_real_fault_network_error_never_counts_as_an_observed_http_failure(self):
        with patch.object(demo, "cart_request"), patch.object(demo, "urlopen", side_effect=URLError("offline")) as opening:
            with self.assertRaisesRegex(RuntimeError, "network failure"):
                REAL_FAULT(self.values, True, 147, 0.5)
            opening.assert_called_once()

    def test_non_review_agent_is_rejected_without_changing_mode(self):
        self.agent.arm["properties"]["actionConfiguration"]["mode"] = "Autonomous"
        self.agent.patch = Mock()
        with self.assertRaisesRegex(ValueError, "existing Review mode"):
            self.run_demo()
        self.probe.assert_not_called()
        self.agent.patch.assert_not_called()
