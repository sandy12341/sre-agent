import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "existing-agent.py"
SPEC = importlib.util.spec_from_file_location("existing_agent", SCRIPT)
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)

AGENT_ID = "/subscriptions/sub/resourceGroups/SRE-Demo/providers/Microsoft.App/agents/existing"
VALUES = {
    "EXISTING_SRE_AGENT_RESOURCE_ID": AGENT_ID,
    "AZURE_SUBSCRIPTION_ID": "sub",
    "AZURE_ENV_NAME": "starter-demo",
    "AZURE_RESOURCE_GROUP": "rg-starter-demo",
    "LOG_ANALYTICS_WORKSPACE_ID": "/subscriptions/sub/resourceGroups/rg-starter-demo/providers/Microsoft.OperationalInsights/workspaces/logs",
    "APP_INSIGHTS_RESOURCE_ID": "/subscriptions/sub/resourceGroups/rg-starter-demo/providers/Microsoft.Insights/components/app",
}


class FakeAgent:
    def __init__(self):
        self.id = AGENT_ID
        self.name = "existing"
        self.subscription = "sub"
        self.arm = {
            "id": AGENT_ID, "identity": {"principalId": "identity"}, "location": "westus2",
            "properties": {
                "actionConfiguration": {"mode": "review"},
                "incidentManagementConfiguration": {
                    "type": "ServiceNow", "connectionName": "existing",
                    "apiConnectionName": "existing-service-now-connection",
                },
                "knowledgeGraphConfiguration": {
                    "identity": "identity", "managedResources": ["/subscriptions/sub"],
                },
            },
        }
        self.collections = {route: [] for route in demo.COLLECTIONS.values()}
        self.global_settings = {"permissions": {"ask": ["RunAzCliWriteCommands"]}}
        self.mutations = []

    def read_arm(self):
        return copy.deepcopy(self.arm)

    def collection(self, path):
        return copy.deepcopy(self.collections[path])

    def patch(self, properties):
        self.mutations.append(("PATCH", properties))
        self.arm["properties"].update(copy.deepcopy(properties))

    def request(self, method, path, body=None):
        if path == "/api/v2/agent/settings/global":
            return self.global_settings
        if path == "/api/v2/github/domains":
            return {"values": [{"name": "github_com"}]}
        route, name = path.rsplit("/", 1)
        current = next((item for item in self.collections[route] if item["name"] == name), None)
        if method == "GET":
            if current is None:
                raise RuntimeError("Not found")
            return copy.deepcopy(current)
        self.mutations.append((method, path))
        if method == "PUT":
            stored = copy.deepcopy(body)
            if stored.get("type") == "ExtendedAgent" and stored.get("owner") == "":
                stored.pop("owner")
            if stored.get("properties", {}).get("dataConnectorType") == "KnowledgeFile":
                stored["tags"] = None
                metadata = stored["properties"]["extendedProperties"]
                metadata["metadata.uploadedFilename"] = metadata.pop("fileName")
                metadata["fileSize"] = len(demo.base64.b64decode(metadata.pop("fileContent")))
            self.collections[route].append(stored)
        elif method == "DELETE":
            self.collections[route].remove(current)
        return {}


def baseline(agent):
    return demo.redact({
        "agent": agent.read_arm(), "globalSettings": copy.deepcopy(agent.global_settings),
        "collections": {key: agent.collection(route) for key, route in demo.COLLECTIONS.items()},
    })


class ExistingAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.agent = FakeAgent()
        self.original = baseline(self.agent)
        demo.write_json(self.state / "baseline.json", self.original)
        original_run = demo.run
        self.original_run = original_run
        def execute(*args):
            if args[0] == "gh":
                return json.dumps({"sha": demo.MANIFEST["applicationCommit"]})
            if args[:3] == ("az", "group", "show"):
                return json.dumps({"tags": {"starter-demo-owner": VALUES["AZURE_ENV_NAME"]}})
            return original_run(*args)
        self.command_patch = patch.object(demo, "run", side_effect=execute)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def configure(self):
        demo.configure(self.agent, VALUES, self.state, True, True)

    def test_arm_patch_accepts_empty_response_and_waits_for_completion(self):
        agent = demo.Agent.__new__(demo.Agent)
        agent.arm_url = "https://management.azure.com/example"
        desired = {"incidentManagementConfiguration": {"type": "AzMonitor"}}
        pending = {"properties": {**desired, "provisioningState": "InProgress"}}
        completed = {"properties": {**desired, "provisioningState": "Succeeded"}}
        with patch.object(demo, "run", return_value=""), \
                patch.object(agent, "read_arm", side_effect=[pending, completed]) as read, \
                patch.object(demo.time, "sleep") as sleep:
            agent.patch(desired)
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(5)
        self.assertEqual(agent.arm, completed)

    def test_arm_patch_surfaces_terminal_failure(self):
        agent = demo.Agent.__new__(demo.Agent)
        agent.arm_url = "https://management.azure.com/example"
        with patch.object(demo, "run", return_value=""), \
                patch.object(agent, "read_arm", return_value={
                    "properties": {"provisioningState": "Failed"},
                }), self.assertRaisesRegex(RuntimeError, "Failed"):
            agent.patch({"incidentManagementConfiguration": {"type": "AzMonitor"}})

    def test_arm_patch_timeout_retains_an_explicit_failure(self):
        agent = demo.Agent.__new__(demo.Agent)
        agent.arm_url = "https://management.azure.com/example"
        with patch.object(demo, "run", return_value=""), \
                patch.object(agent, "read_arm", return_value={
                    "properties": {"provisioningState": "InProgress"},
                }), patch.object(demo.time, "monotonic", side_effect=[0, 301]), \
                self.assertRaisesRegex(RuntimeError, "retain the change ledger"):
            agent.patch({"incidentManagementConfiguration": {"type": "AzMonitor"}})

    def test_service_now_without_restoration_credentials_blocks_all_writes(self):
        self.agent.arm["properties"]["incidentManagementConfiguration"]["apiConnectionName"] = ""
        demo.write_json(self.state / "baseline.json", baseline(self.agent))
        with self.assertRaisesRegex(ValueError, "no restorable credential"):
            self.configure()
        self.assertEqual(self.agent.mutations, [])
        self.assertFalse((self.state / "changes.json").exists())

    def test_knowledge_readback_verifies_upload_metadata_not_write_only_content(self):
        body = demo.generated_objects(VALUES)[0][1]
        actual = copy.deepcopy(body)
        actual["tags"] = None
        metadata = actual["properties"]["extendedProperties"]
        metadata["metadata.uploadedFilename"] = metadata.pop("fileName")
        metadata["fileSize"] = len(demo.base64.b64decode(metadata.pop("fileContent")))
        self.assertTrue(demo.object_matches(body, actual))
        metadata["fileSize"] += 1
        self.assertFalse(demo.object_matches(body, actual))
        metadata["fileSize"] -= 1
        metadata["metadata.uploadedFilename"] = "different.md"
        self.assertFalse(demo.object_matches(body, actual))

    def test_only_empty_subagent_owner_can_be_omitted_by_service(self):
        body = {"name": "demo", "type": "ExtendedAgent", "owner": "", "properties": {}}
        actual = {key: value for key, value in body.items() if key != "owner"}
        self.assertTrue(demo.object_matches(body, actual))
        body["owner"] = "nonempty-owner"
        self.assertFalse(demo.object_matches(body, actual))

    def test_owned_repo_allows_clone_lifecycle_but_not_source_changes(self):
        applied = {
            "type": "CodeRepo", "properties": {
                "url": "https://github.com/example/demo", "branch": "demo",
                "cloneStatus": "Cloning", "localPath": None, "latestCommit": None,
            },
        }
        current = copy.deepcopy(applied)
        current["properties"].update({
            "cloneStatus": "Ready", "localPath": "service-generated",
            "latestCommit": demo.MANIFEST["applicationCommit"],
            "lastSuccessfulSync": "service-generated", "errorMessage": "",
        })
        self.assertTrue(demo.unchanged_owned_object(applied, current))
        current["properties"]["latestCommit"] = "different-commit"
        self.assertFalse(demo.unchanged_owned_object(applied, current))
        current["properties"]["latestCommit"] = demo.MANIFEST["applicationCommit"]
        current["properties"]["branch"] = "different-branch"
        self.assertFalse(demo.unchanged_owned_object(applied, current))

    def test_knowledge_405_requires_verified_persisted_upload(self):
        original_request = self.agent.request

        def request(method, path, body=None):
            result = original_request(method, path, body)
            if method == "PUT" and body.get("properties", {}).get("dataConnectorType") == "KnowledgeFile":
                raise RuntimeError("Knowledge upload returned HTTP 405") from demo.HTTPError(
                    path, 405, "Method Not Allowed", {}, None,
                )
            return result

        with patch.object(self.agent, "request", side_effect=request), \
                patch.object(demo.sys.stderr, "write") as warning:
            self.configure()
        self.assertTrue(any("WARNING:" in call.args[0] for call in warning.call_args_list))
        ledger = json.loads((self.state / "changes.json").read_text())
        self.assertTrue(all(entry["applied"] is not None for entry in ledger["objects"]))

    def test_knowledge_405_cannot_hide_a_mismatched_upload(self):
        original_request = self.agent.request

        def request(method, path, body=None):
            result = original_request(method, path, body)
            if method == "PUT" and body.get("properties", {}).get("dataConnectorType") == "KnowledgeFile":
                self.agent.collections[demo.COLLECTIONS["connectors"]][-1]["properties"]["extendedProperties"]["fileSize"] += 1
                raise RuntimeError("Knowledge upload returned HTTP 405") from demo.HTTPError(
                    path, 405, "Method Not Allowed", {}, None,
                )
            return result

        with patch.object(self.agent, "request", side_effect=request), \
                patch.object(demo.sys.stderr, "write"), \
                self.assertRaisesRegex(RuntimeError, "Configuration verification failed"):
            self.configure()

    def test_knowledge_delete_405_requires_verified_absence(self):
        self.configure()
        original_request = self.agent.request

        def request(method, path, body=None):
            result = original_request(method, path, body)
            if method == "DELETE" and path.startswith(demo.COLLECTIONS["connectors"] + "/"):
                raise RuntimeError("Knowledge delete returned HTTP 405") from demo.HTTPError(
                    path, 405, "Method Not Allowed", {}, None,
                )
            return result

        with patch.object(self.agent, "request", side_effect=request), \
                patch.object(demo.sys.stderr, "write") as warning:
            demo.cleanup(self.agent, self.state, True)
        self.assertTrue(any("WARNING:" in call.args[0] for call in warning.call_args_list))
        self.assertTrue(json.loads((self.state / "changes.json").read_text())["cleanupVerified"])

    def test_knowledge_delete_405_cannot_hide_a_remaining_object(self):
        self.configure()
        original_request = self.agent.request

        def request(method, path, body=None):
            if method == "DELETE" and path.startswith(demo.COLLECTIONS["connectors"] + "/"):
                raise RuntimeError("Knowledge delete returned HTTP 405") from demo.HTTPError(
                    path, 405, "Method Not Allowed", {}, None,
                )
            return original_request(method, path, body)

        with patch.object(self.agent, "request", side_effect=request), \
                patch.object(demo.sys.stderr, "write"), \
                self.assertRaisesRegex(RuntimeError, "Deletion not yet verified"):
            demo.cleanup(self.agent, self.state, True)

    def test_exact_agent_id_required(self):
        for invalid in ("", "existing", AGENT_ID + "/children", "https://example.test"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                demo.agent_parts(invalid)

    def test_workload_cannot_be_agent_group(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            demo.validate_workload({**VALUES, "AZURE_RESOURCE_GROUP": "sre-demo"}, AGENT_ID)

    def test_subscription_and_group_mismatch_rejected(self):
        for key, value in (("AZURE_SUBSCRIPTION_ID", "other"), ("AZURE_RESOURCE_GROUP", "arbitrary")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                demo.validate_workload({**VALUES, key: value}, AGENT_ID)

    def test_existing_unowned_workload_group_blocks_mutation(self):
        with patch.object(demo, "az_json", return_value={"tags": {"other": "owner"}}):
            with self.assertRaisesRegex(ValueError, "not marked as owned"):
                self.configure()
        self.assertEqual(self.agent.mutations, [])

    def test_private_state_cannot_be_inside_worktree(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            demo.state_directory(demo.ROOT / "state")

    def test_secrets_redacted_recursively(self):
        result = demo.redact({"connectionKey": "secret", "nested": [{"password": "hidden"}],
                              "permissions": {"ask": ["write"]}, "empty": ""})
        self.assertNotIn("hidden", json.dumps(result))
        self.assertEqual(result["connectionKey"], "[REDACTED]")
        self.assertEqual(result["permissions"]["ask"], ["write"])

    def test_unknown_collection_shape_is_not_empty_success(self):
        with self.assertRaises(ValueError):
            demo.items({"error": "authorization failed"})
        with self.assertRaises(ValueError):
            demo.items(None)

    def test_only_service_generated_incident_revision_is_ignored(self):
        self.assertEqual(demo.incident_configuration({"type": "ServiceNow", "oboUser": "sreagent-config-revision:1"}),
                         {"type": "ServiceNow"})
        self.assertEqual(demo.incident_configuration({"type": "ServiceNow", "oboUser": "actual-user"}),
                         {"type": "ServiceNow", "oboUser": "actual-user"})

    def test_moved_source_branch_blocks_configuration(self):
        with patch.object(demo, "run", side_effect=lambda *args: json.dumps({"sha": "moved"})
                          if args[0] == "gh" else json.dumps({"tags": {"starter-demo-owner": VALUES["AZURE_ENV_NAME"]}})
                          if args[:3] == ("az", "group", "show") else self.original_run(*args)):
            with self.assertRaisesRegex(ValueError, "source branch moved"):
                self.configure()
        self.assertEqual(self.agent.mutations, [])

    def test_installed_azd_dotenv_format_is_supported_without_shell_expansion(self):
        output = '# local environment\nNAME="starter-demo"\nID="/subscriptions/sub/resourceGroups/SRE-Demo"\nVALUE="literal $HOME"\n'
        with patch.object(demo, "run", return_value=output) as execute:
            self.assertEqual(demo.env_values(), {
                "NAME": "starter-demo", "ID": "/subscriptions/sub/resourceGroups/SRE-Demo",
                "VALUE": "literal $HOME",
            })
        execute.assert_called_once_with("azd", "env", "get-values")

    def test_ambiguous_environment_output_is_rejected(self):
        for output in ('KEY="value"\nKEY="other"', "not dotenv", "1INVALID=value"):
            with self.subTest(output=output), patch.object(demo, "run", return_value=output), self.assertRaises(ValueError):
                demo.env_values()

    def test_generated_objects_are_scoped_review_only_and_schedule_disabled(self):
        objects = demo.generated_objects(VALUES)
        names = [body["name"] for _, body in objects]
        self.assertEqual(len(names), len(set(names)))
        for route, body in objects:
            self.assertTrue(body["name"].startswith("starter-starter-demo-"))
            if route.startswith(demo.COLLECTIONS["agents"] + "/"):
                instructions = body["properties"]["instructions"]
                self.assertIn("rg-starter-demo", instructions)
                self.assertIn(demo.MANIFEST["applicationRepository"], instructions)
                self.assertNotIn("Perform all actions autonomously", instructions)
            if route.startswith(demo.COLLECTIONS["filters"] + "/"):
                self.assertEqual(body["properties"]["agentMode"], "Review")
                self.assertEqual(body["properties"]["titleContains"], "alert-http-5xx-starter-demo")
            if route.startswith(demo.COLLECTIONS["tasks"] + "/"):
                self.assertEqual(body["properties"]["status"], "Paused")
                self.assertNotIn("isEnabled", body["properties"])
                self.assertEqual(body["properties"]["agentMode"], "Review")

    def test_configuration_requires_explicit_approval(self):
        with self.assertRaisesRegex(ValueError, "approve-configuration"):
            demo.configure(self.agent, VALUES, self.state, False, False)
        self.assertEqual(self.agent.mutations, [])

    def test_platform_switch_requires_separate_approval(self):
        with self.assertRaisesRegex(ValueError, "temporary-switch"):
            demo.configure(self.agent, VALUES, self.state, True, False)
        self.assertEqual(self.agent.mutations, [])

    def test_baseline_changes_block_writes(self):
        self.agent.arm["properties"]["actionConfiguration"]["mode"] = "autonomous"
        with self.assertRaisesRegex(ValueError, "settings changed"):
            self.configure()
        self.assertEqual(self.agent.mutations, [])

    def test_global_policy_changes_block_writes(self):
        self.agent.global_settings["permissions"]["ask"] = []
        with self.assertRaisesRegex(ValueError, "global settings"):
            self.configure()
        self.assertEqual(self.agent.mutations, [])

    def test_name_collision_cannot_overwrite_existing_configuration(self):
        route, body = demo.generated_objects(VALUES)[0]
        self.agent.collections[route.rsplit("/", 1)[0]].append(body)
        demo.write_json(self.state / "baseline.json", baseline(self.agent))
        with self.assertRaisesRegex(ValueError, "collision"):
            self.configure()
        self.assertEqual(self.agent.mutations, [])

    def test_setup_cleanup_restores_platform_and_preserves_existing_objects(self):
        existing_task = {"name": "existing-task", "properties": {"status": "Active"}}
        self.agent.collections[demo.COLLECTIONS["tasks"]].append(existing_task)
        demo.write_json(self.state / "baseline.json", baseline(self.agent))
        self.configure()
        self.assertEqual(self.agent.arm["properties"]["incidentManagementConfiguration"]["type"], "AzMonitor")
        with self.assertRaisesRegex(ValueError, "approve-cleanup"):
            demo.cleanup(self.agent, self.state, False)
        demo.cleanup(self.agent, self.state, True)
        self.assertEqual(self.agent.arm["properties"], self.original["agent"]["properties"])
        self.assertEqual(self.agent.collections[demo.COLLECTIONS["tasks"]], [existing_task])
        self.assertTrue(json.loads((self.state / "changes.json").read_text())["cleanupVerified"])

    def test_cleanup_refuses_to_delete_externally_modified_objects(self):
        self.configure()
        task = self.agent.collections[demo.COLLECTIONS["tasks"]][0]
        task["properties"]["status"] = "Active"
        with self.assertRaisesRegex(ValueError, "changed after setup"):
            demo.cleanup(self.agent, self.state, True)

    def test_configuration_retry_cannot_overwrite_ownership(self):
        self.configure()
        mutations = len(self.agent.mutations)
        with self.assertRaisesRegex(ValueError, "ledger already exists"):
            self.configure()
        self.assertEqual(len(self.agent.mutations), mutations)

    def test_fault_requires_approval_and_bounded_input(self):
        with self.assertRaisesRegex(ValueError, "approve-fault"):
            demo.fault(VALUES, False, 150, 0.5)
        for count, interval in ((201, 0.5), (0, 0.5), (10, 0.1)):
            with self.subTest(count=count, interval=interval), self.assertRaisesRegex(ValueError, "bounds"):
                demo.fault(VALUES, True, count, interval)

    def test_business_probe_and_reset_require_explicit_approval(self):
        with self.assertRaisesRegex(ValueError, "approve-probe"):
            demo.probe(VALUES, False)
        with self.assertRaisesRegex(ValueError, "approve-reset"):
            demo.reset(VALUES, False)

    def test_build_uses_pinned_source_and_immutable_digests(self):
        calls = []
        def execute(*args):
            calls.append(args)
            if args[1:3] == ("group", "show"):
                return json.dumps({"tags": {"starter-demo-owner": VALUES["AZURE_ENV_NAME"]}})
            if args[1:3] == ("acr", "show"):
                return "demo.azurecr.io"
            if args[1:4] == ("acr", "repository", "show"):
                return "sha256:" + "a" * 64
            return ""
        values = {**VALUES, "AZURE_CONTAINER_REGISTRY_NAME": "registry",
                  "CONTAINER_APP_NAME": "api", "FRONTEND_APP_NAME": "frontend",
                  "CONTAINER_APP_URL": "https://api.test", "FRONTEND_APP_URL": "https://frontend.test"}
        with patch.object(demo, "run", side_effect=execute):
            demo.build(values)
        builds = [call for call in calls if call[1:3] == ("acr", "build")]
        self.assertEqual(len(builds), 2)
        self.assertTrue(all(any(f"#{demo.MANIFEST['applicationCommit']}:" in arg for arg in call) for call in builds))
        updates = [call for call in calls if call[1:3] == ("containerapp", "update")]
        self.assertEqual(len(updates), 2)
        self.assertTrue(all(any("@sha256:" in arg for arg in call) for call in updates))
        self.assertFalse(any("rest" in call for call in calls))


@unittest.skipUnless(os.environ.get("STARTER_COMPILED_TEMPLATE"), "Run after Bicep compilation to verify ownership conditions")
class CompiledInfrastructureTests(unittest.TestCase):
    def test_existing_agent_mode_excludes_agent_identity_and_subscription_grants(self):
        template = json.loads(Path(os.environ["STARTER_COMPILED_TEMPLATE"]).read_text(encoding="utf-8-sig"))
        self.assertEqual(template["variables"]["createAgent"], "[empty(parameters('existingAgentResourceId'))]")
        resources = {resource["name"]: resource for resource in template["resources"]}
        self.assertEqual(resources["subscription-rbac"]["condition"], "[variables('createAgent')]")
        deployment = resources["resources-deployment"]["properties"]
        self.assertEqual(deployment["parameters"]["createAgent"]["value"], "[variables('createAgent')]")
        nested = {resource["name"]: resource for resource in deployment["template"]["resources"]}
        self.assertEqual(nested["identity"]["condition"], "[parameters('createAgent')]")
        self.assertEqual(nested["sre-agent"]["condition"], "[parameters('createAgent')]")


if __name__ == "__main__":
    unittest.main()
