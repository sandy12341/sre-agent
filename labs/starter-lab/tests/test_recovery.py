import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from test_existing_agent import FakeAgent, VALUES, demo
from test_presenter import RULE, TARGET, alert, message, thread


START = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)
REAL_PROBE = demo.probe


def execution(command=None, **fields):
    command = command or (
        "az containerapp revision restart --subscription sub --resource-group rg-starter-demo "
        "--name api --revision api--one --output none"
    )
    result = {
        "id": "write-one", "toolCallId": "call-one", "command": command, "status": "Completed",
        "originalFunctionCall": json.dumps({
            "Name": "RunAzCliWriteCommands", "Arguments": {"command": command}, "CallId": "call-one",
        }),
        "createdTimestamp": "2026-11-10T01:00:01Z", "startedTimestamp": "2026-11-10T01:00:02Z",
        "completedTimestamp": "2026-11-10T01:00:05Z",
        "executedBy": {"role": "User", "userId": "agent-default", "displayName": "SRE Agent"},
        "error": None, "expiredByTimeout": False,
    }
    result.update(fields)
    return result


def write_message(**fields):
    return {**message("write-message", "Real tool event", azCliExecution=execution(**fields)),
            "timeStamp": "2026-11-10T01:00:01Z"}


def activity(**fields):
    result = {
        "eventDataId": "event-one", "correlationId": "correlation-one",
        "resourceId": TARGET + "/revisions/api--one", "caller": "identity",
        "eventTimestamp": "2026-11-10T01:00:04Z",
        "operationName": {"value": "Microsoft.App/containerApps/revisions/restart/action"},
        "status": {"value": "Succeeded"},
    }
    result.update(fields)
    return result


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.source_name = "demo-run-existing.json"
        self.source = {
            "status": "diagnosis_complete", "faultStarted": "2026-11-10T00:00:00Z",
            "maxCartPosts": 150, "healthyCartPosts": 3, "fault": {"requests": 72, "failures": 6},
            "alertId": alert()["id"], "threadId": thread()["id"],
        }
        demo.write_json(self.state / self.source_name, self.source)
        self.source_bytes = (self.state / self.source_name).read_bytes()
        self.args = demo.argparse.Namespace(run_record=self.source_name, wait_seconds=60, approve_probe=True)
        self.values = {**VALUES, "CONTAINER_APP_NAME": "api"}
        self.agent = FakeAgent()
        self.agent.arm["properties"]["incidentManagementConfiguration"] = {"type": "AzMonitor"}
        self.current = thread()
        self.current["lastMessage"] = {
            **message("final-rca", "Actual final RCA: restart mitigated the allocation defect; code fix remains."),
            "timeStamp": "2026-11-10T01:00:10Z",
        }
        self.messages = [write_message()]
        self.events = [activity()]
        self.active = [{"name": "api--one", "properties": {"active": True, "healthState": "Healthy"}}]
        self.clock_seconds = 0
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(contextlib.redirect_stderr(self.output))
        stack.enter_context(patch.object(demo, "validate_owned_workload", return_value=("sub", "rg-starter-demo", "starter-demo")))
        stack.enter_context(patch.object(demo, "az_json", side_effect=self.azure))
        stack.enter_context(patch.object(demo.time, "sleep"))
        stack.enter_context(patch.object(demo.time, "monotonic", side_effect=itertools.count(0, 61)))
        clock = stack.enter_context(patch.object(demo, "datetime", wraps=datetime))
        clock.now.side_effect = lambda zone: START + timedelta(seconds=self.clock_seconds)
        self.probe = stack.enter_context(patch.object(demo, "probe"))
        self.reset = stack.enter_context(patch.object(demo, "reset"))
        self.fault = stack.enter_context(patch.object(demo, "fault"))
        self.agent.request = Mock(side_effect=self.request)
        self.agent.collection = Mock(side_effect=lambda path: copy.deepcopy(self.messages))
        resolved = alert()
        resolved["properties"]["essentials"].update(
            monitorCondition="Resolved", monitorConditionResolvedDateTime="2026-11-10T00:55:00Z",
        )
        self.alerts = stack.enter_context(patch.object(demo, "demo_alerts", return_value=[resolved]))

    def azure(self, *args):
        if args[:3] == ("containerapp", "revision", "list"):
            return copy.deepcopy(self.active)
        if args[:3] == ("monitor", "activity-log", "list"):
            self.clock_seconds = 10
            return copy.deepcopy(self.events)
        self.fail(f"Unexpected Azure command: {args}")

    def request(self, method, path, body=None):
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/api/v1/threads/automatic-thread")
        self.assertIsNone(body)
        return copy.deepcopy(self.current)

    def observe(self):
        return demo.watch_recovery(self.agent, self.values, self.state, self.args)

    def saved(self):
        return json.loads((self.state / ("recovery-" + self.source_name)).read_text())

    def test_scoped_agent_execution_activity_checks_and_fresh_rca(self):
        result = self.observe()
        self.assertEqual(result["status"], "service_recovery_verified")
        self.assertEqual(result["probe"]["budgetReserved"], 3)
        self.assertEqual(result["probe"]["status"], "completed")
        self.assertEqual(result["activity"]["write-one"]["eventDataId"], "event-one")
        self.assertEqual(result["approvalGateVerification"], "requires_live_denial_and_approval_test")
        self.probe.assert_called_once_with(self.values, True)
        self.reset.assert_not_called()
        self.fault.assert_not_called()
        self.assertEqual(self.agent.mutations, [])
        self.assertEqual((self.state / self.source_name).read_bytes(), self.source_bytes)
        self.assertIn("may predate restart", self.output.getvalue())

    def test_resume_reuses_verified_probe_without_additional_posts(self):
        self.observe()
        self.args.approve_probe = False
        result = self.observe()
        self.assertEqual(result["status"], "service_recovery_verified")
        self.probe.assert_called_once()
        self.assertEqual(len(result["resumes"]), 1)

    def test_cart_posts_require_separate_approval(self):
        self.args.approve_probe = False
        with self.assertRaisesRegex(RuntimeError, "require --approve-probe"):
            self.observe()
        self.probe.assert_not_called()
        self.assertEqual(self.saved()["probe"]["budgetReserved"], 0)
        self.args.approve_probe = True
        self.observe()
        self.probe.assert_called_once()

    def test_actual_probe_performs_exactly_three_posts(self):
        self.probe.side_effect = REAL_PROBE
        responses = []
        for _ in range(3):
            response = io.BytesIO(b'{"items":[{"foodItemId":1,"quantity":1}]}')
            response.status = 200
            responses.append(response)
        with patch.object(demo, "cart_request") as request, patch.object(demo, "urlopen", side_effect=responses) as http:
            self.observe()
        request.assert_called_once_with(self.values, "POST")
        self.assertEqual(http.call_count, 3)

    def test_failed_or_interrupted_probe_is_never_repeated_on_resume(self):
        self.probe.side_effect = URLError("network unavailable")
        with self.assertRaises(URLError):
            self.observe()
        self.assertEqual(self.saved()["probe"]["budgetReserved"], 3)
        self.probe.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "No automatic repeat"):
            self.observe()
        self.probe.assert_called_once()

    def test_claimed_recovery_without_write_tool_is_not_execution(self):
        self.messages = [message("claim", "I restarted it.")]
        with self.assertRaisesRegex(RuntimeError, "actual scoped agent"):
            self.observe()
        self.probe.assert_not_called()

    def test_pending_write_does_not_authorize_or_certify_recovery(self):
        self.messages = [write_message(status="PendingApproval", startedTimestamp=None, completedTimestamp=None)]
        with self.assertRaisesRegex(RuntimeError, "actual scoped agent"):
            self.observe()
        self.assertEqual(self.saved()["executions"]["write-one"]["status"], "PendingApproval")
        self.assertEqual(self.saved()["approvalGateVerification"], "requires_live_denial_and_approval_test")
        self.probe.assert_not_called()

    def test_denial_without_execution_is_preserved_and_no_probe_runs(self):
        self.messages = [write_message(
            status="Denied", startedTimestamp=None, completedTimestamp=None, error="User denied",
        )]
        with self.assertRaisesRegex(RuntimeError, "actual scoped agent"):
            self.observe()
        self.assertEqual(self.saved()["executions"]["write-one"]["status"], "Denied")
        self.probe.assert_not_called()

    def test_wrong_targets_shell_expressions_duplicates_and_unknown_flags_are_rejected(self):
        original = execution()["command"]
        commands = [
            original.replace("--name api", "--name frontend"),
            original.replace("rg-starter-demo", "SRE-Demo"),
            original.replace("--subscription sub", "--subscription other"),
            original.replace("api--one", "api--two"),
            original + "; echo done", original + " && echo done",
            original + " --name api", original + " --only-show-errors",
            original.replace("api--one", "$(get-revision)"),
            original.replace("restart", "delete"),
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ValueError):
                demo.recovery_revision(command, self.values, ["api--one"])

    def test_read_tool_or_user_message_does_not_count_as_agent_write(self):
        for change in ("read", "user"):
            candidate = write_message()
            if change == "read":
                function = json.loads(candidate["azCliExecution"]["originalFunctionCall"])
                function["Name"] = "RunAzCliReadCommands"
                candidate["azCliExecution"]["originalFunctionCall"] = json.dumps(function)
            else:
                candidate["author"] = {"role": "User"}
            self.assertIsNone(demo.recovery_execution(candidate, START, self.values, ["api--one"]))

    def test_old_call_or_mismatched_provenance_is_rejected(self):
        for fields in (
            {"createdTimestamp": "2026-11-10T00:59:59Z"},
            {"toolCallId": "different-call"},
            {"startedTimestamp": "2026-11-10T00:59:59Z"},
            {"executedBy": {"userId": "operator", "displayName": "SRE Agent"}},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                demo.recovery_execution(write_message(**fields), START, self.values, ["api--one"])

    def test_tool_error_is_not_a_completed_restart(self):
        self.messages = [write_message(error="AuthorizationFailed")]
        with self.assertRaisesRegex(RuntimeError, "AuthorizationFailed"):
            self.observe()
        self.probe.assert_not_called()

    def test_two_completed_restarts_of_same_revision_exceed_the_budget(self):
        self.messages.append(write_message(id="write-two"))
        with self.assertRaisesRegex(ValueError, "More than one"):
            self.observe()
        self.probe.assert_not_called()

    def test_extra_pending_restart_after_completion_is_not_ignored(self):
        self.messages.append(write_message(
            id="write-two", status="PendingApproval", startedTimestamp=None, completedTimestamp=None,
        ))
        with self.assertRaisesRegex(ValueError, "additional write request"):
            self.observe()
        self.probe.assert_not_called()

    def test_operator_activity_cannot_prove_agent_execution(self):
        self.events = [activity(caller="operator")]
        with self.assertRaisesRegex(RuntimeError, "agent-identity Azure"):
            self.observe()
        self.probe.assert_not_called()

    def test_wrong_resource_operation_or_time_does_not_match_activity(self):
        for fields in (
            {"resourceId": TARGET + "-frontend"},
            {"operationName": {"value": "Microsoft.App/containerApps/write"}},
            {"eventTimestamp": "2026-11-10T00:59:00Z"},
        ):
            self.events = [activity(**fields)]
            self.assertIsNone(demo.recovery_activity(
                self.agent, self.values, [{**execution(), "revision": "api--one"}],
            ))

    def test_unhealthy_or_changed_revisions_block_business_probes(self):
        self.active[0]["properties"]["healthState"] = "Unhealthy"
        with self.assertRaisesRegex(RuntimeError, "healthy active"):
            self.observe()
        self.probe.assert_not_called()
        self.active[0]["name"] = "api--two"
        with self.assertRaisesRegex(ValueError, "active revisions changed"):
            self.observe()

    def test_unresolved_alert_is_not_a_recovered_monitor(self):
        self.alerts.return_value = [alert()]
        with self.assertRaisesRegex(RuntimeError, "SAME alert"):
            self.observe()
        self.assertEqual(self.saved()["probe"]["status"], "completed")
        self.probe.assert_called_once()

    def test_wrong_automatic_incident_blocks_all_actions(self):
        self.current["source"] = "Chat"
        with self.assertRaisesRegex(ValueError, "automatic incident"):
            self.observe()
        self.probe.assert_not_called()
        self.agent.collection.assert_not_called()

    def test_review_mode_and_completed_diagnosis_are_required(self):
        self.agent.arm["properties"]["actionConfiguration"]["mode"] = "Autonomous"
        with self.assertRaisesRegex(ValueError, "Review/Azure Monitor"):
            self.observe()
        self.agent.arm["properties"]["actionConfiguration"]["mode"] = "Review"
        self.source["status"] = "failed"
        demo.write_json(self.state / self.source_name, self.source)
        with self.assertRaisesRegex(ValueError, "completed real demo diagnosis"):
            self.observe()
        self.probe.assert_not_called()

    def test_all_active_revisions_need_distinct_tool_and_activity_evidence(self):
        self.active.append({"name": "api--two", "properties": {"active": True, "healthState": "Healthy"}})
        second = write_message(command=execution()["command"].replace("api--one", "api--two"), id="write-two")
        second["id"] = "write-message-two"
        second["azCliExecution"]["toolCallId"] = "call-two"
        function = json.loads(second["azCliExecution"]["originalFunctionCall"])
        function["CallId"] = "call-two"
        second["azCliExecution"]["originalFunctionCall"] = json.dumps(function)
        self.messages.append(second)
        with self.assertRaisesRegex(RuntimeError, "agent-identity Azure"):
            self.observe()
        self.probe.assert_not_called()
        self.events.append(activity(eventDataId="event-two", resourceId=TARGET + "/revisions/api--two"))
        result = self.observe()
        self.assertEqual(len(result["activity"]), 2)
        self.probe.assert_called_once()

    def test_recovery_record_cannot_be_rebound_to_another_agent(self):
        self.observe()
        record = self.saved()
        record["agentId"] = "/subscriptions/sub/resourceGroups/other/providers/Microsoft.App/agents/other"
        demo.write_json(self.state / ("recovery-" + self.source_name), record)
        with self.assertRaisesRegex(ValueError, "record or active revisions changed"):
            self.observe()
        self.probe.assert_called_once()

    def test_incomplete_activity_inventory_never_passes(self):
        self.events = [activity()] * 1000
        with self.assertRaisesRegex(ValueError, "may be truncated"):
            self.observe()
        self.probe.assert_not_called()

    def test_existing_atomic_probe_reservation_blocks_duplicate_or_concurrent_posts(self):
        reservation = (self.state / ("recovery-" + self.source_name)).with_suffix(".probe-reservation")
        reservation.write_text('{"budgetReserved":3}')
        with self.assertRaisesRegex(RuntimeError, "already reserved"):
            self.observe()
        self.probe.assert_not_called()
        self.assertEqual(self.saved()["probe"]["budgetReserved"], 3)

    def test_stale_tool_or_reasoning_report_is_not_the_final_rca(self):
        for fields in (
            {"timeStamp": "2026-11-10T00:00:02Z"},
            {"messageType": "Reasoning", "text": "HIDDEN_REASONING"},
            {"messageType": "TrajectoryInsight"},
            {"azCliExecution": {"status": "Completed"}},
        ):
            self.current["lastMessage"] = {
                **message("report", "Not a fresh RCA"), "timeStamp": "2026-11-10T01:00:10Z", **fields,
            }
            with self.subTest(fields=fields), self.assertRaisesRegex(RuntimeError, "fresh completed final RCA"):
                self.observe()
        self.probe.assert_called_once()
        self.assertNotIn("HIDDEN_REASONING", self.output.getvalue())
        self.assertNotIn("HIDDEN_REASONING", json.dumps(self.saved()))


if __name__ == "__main__":
    unittest.main()
