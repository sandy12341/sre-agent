# Phase 1: Starter with an existing SRE Agent

This path reuses an existing agent and deploys **only the Grubify workload** into
a separate disposable resource group. It does not own, replace, or delete the
agent, its identity, or its existing integrations.

The original [new-agent lab](README.md) remains available. Do not use
`scripts/setup.sh` for this reuse path: it is the original whole-agent setup.

## Status and approval gates

Repository preparation and offline validation are separate from live readiness.
Before provisioning, approve the subscription, workload region, cost envelope,
runtime access, and retention. Before configuration, review the private baseline
and exact configuration plan. A live demo is not ready until all three scenarios,
alert routing, approval enforcement, reset, and cleanup have been rehearsed.

The selected source revisions are recorded in [demo.json](demo.json). The Grubify
demo branch adds Azure Monitor OpenTelemetry and a Node.js 24 LTS frontend build;
the intentional memory leak is preserved. Its source workflow verifies matching
package engines and builds the complete frontend container without Azure access.
Images build from the exact commit and deploy by digest, while code
indexing uses the corresponding branch after verifying its current commit.
Never move that branch or update pins during a customer session.

## 1. Local setup and read-only baseline

Run from this lab directory on `demo/customer-ready`. Required tools: Git Bash,
Git, gh, Azure CLI, azd, and Python 3 with PyYAML. Local Docker is not required;
image builds use ACR Tasks. Verify tools before installing dependencies.

PowerShell examples below use placeholders; do not put credentials in commands.

On Windows, Git Bash must be on the PATH inherited by azd lifecycle hooks, not
just available through an absolute-path invocation. If needed, add the installed
Git Bash directory to the current PowerShell process before validation/deploy:

```powershell
$env:PATH = 'C:\Program Files\Git\bin;' + $env:PATH
bash --version
```

```powershell
$Subscription = '<approved-subscription-id>'
$AgentId = "/subscriptions/$Subscription/resourceGroups/SRE-Demo/providers/Microsoft.App/agents/<existing-agent-name>"
$State = Join-Path $env:LOCALAPPDATA 'SRE-Agent-Demo\starter-demo'
python .\scripts\existing-agent.py inventory --agent-id $AgentId --state-dir $State
```

The state directory must be outside Git and restricted to the operator. It
contains environment metadata and a redacted configuration snapshot, not tokens.
Keep it through cleanup. Inventory fails explicitly if required APIs cannot be
read. Never overwrite an earlier baseline to disguise configuration drift.
The snapshot is not a full service backup or a credential export.

Inventory includes global tool policy, existing integrations/configuration,
both runtime identities' role assignments, managed resources, mode and incident
platform. Identify other users of the agent before changing global settings.

## 2. Configure a separate workload environment

Only after approving the Azure context and budget:

```powershell
azd env new starter-demo
azd env set AZURE_SUBSCRIPTION_ID $Subscription
azd env set AZURE_LOCATION '<approved-workload-region>'
azd env set EXISTING_SRE_AGENT_RESOURCE_ID $AgentId
```

The workload group is `rg-starter-demo`, never `SRE-Demo`. The Bicep reuse branch
omits agent creation, agent identity creation, and the original subscription-RBAC
module. A pre-provision hook checks the exact agent, subscription, and separate
group boundary. It refuses to reuse a pre-existing group unless its
`starter-demo-owner` tag matches this environment. Build, configuration, probes,
fault injection and reset also enforce that ownership tag. Inspect a deployment
preview to confirm the boundary; then use
the approved Azure validation/deployment workflow to provision.

The post-provision hook builds the two pinned images and deploys them. It does
**not** configure the agent or create GitHub issues. Build failures stop the hook.
Do not invoke the original post-provision `--retry` or `--status` in reuse mode;
they are rejected rather than silently running whole-agent setup or billing a
rebuild. An explicit rebuild uses `python .\scripts\existing-agent.py build`.

The API receives its workload App Insights connection string from Bicep; it does
not reuse the agent's operational telemetry.

## 3. Approve access and additive agent configuration

Review runtime identity access to the workload before setup. Existing grants are
not lab-owned. Add only missing, explicitly approved access at workload/workspace
scope: resource/monitoring reads, log queries, and Container Apps remediation.
Record exact newly created assignment IDs in private operator records and remove
only those during cleanup. The helper does not grant roles or broaden access.
Prompt scoping is not an Azure authorization boundary: an existing identity with
broader permissions retains those permissions.

Use the trusted portal to confirm GitHub authorization for the demo Grubify fork.
The helper reuses OAuth; it never resets it or prints OAuth credentials.

```powershell
python .\scripts\existing-agent.py plan --state-dir $State
```

Review the private configuration plan. It creates namespaced knowledge sources,
three subagents, the pinned code binding, a workload-specific response plan in
Review mode, and a **disabled** recurring triage task. It does not patch global
tool permissions, model selection, agent identities, or action mode.

If the agent uses ServiceNow, switching to Azure Monitor is agent-wide and can
affect other incident handling. Pause for explicit configuration approval and
coordinate the rehearsal window. The helper refuses unapproved switching and
refuses automatic restoration if the original platform requires redacted secrets.

```powershell
# Run only after approving the exact configuration delta and temporary switch:
python .\scripts\existing-agent.py configure --state-dir $State `
  --approve-configuration --allow-platform-switch
```

Setup requires the agent already to be in Review mode. It refuses collisions,
changed baselines, moved source branches, or an existing change ledger. It records
each intended change before writing, verifies readback, and retains the ledger on
partial failure. Do not repeatedly overwrite configuration: inspect the failure,
clean up the owned run, then capture a new baseline before a new rehearsal.

Verify actual code/knowledge indexing, workload telemetry queries, tool
availability, and response-plan routing in the portal. A matching API envelope
does not prove indexing or live alert delivery.

## 4. Presenter runbook

Select the namespaced `starter-<environment>-...` agents, not unrelated agents.
Every write remains subject to Review-mode approval and live policy validation.

### Scenario 1: Operations investigation and recovery

```powershell
python .\scripts\existing-agent.py probe --approve-probe
python .\scripts\existing-agent.py fault --approve-fault --requests 150 --interval 0.5
```

The business probe performs three Add to Cart operations. Fault load is capped
at 200 requests, waits between requests, has per-request timeouts, and stops after
six observed server errors. Ctrl+C is the escape hatch. It reports observed
failures, not a fictional successful incident. Do not substitute generic
`/health` or `/api/menu` checks: those routes are absent from this pinned app.

Presenter prompt for the incident handler:

> Add to Cart is failing in the Starter workload. Investigate its logs, metrics,
> and runbook. Show evidence and uncertainty. Do not create GitHub issues or
> change resources until I approve a specific mitigation.

Independently verify Azure Monitor alert delivery to the agent; manual chat is
not proof of automatic routing. Demonstrate withholding approval before granting
it. Recovery should clear process-local cache by restarting all active API
revisions. Operator fallback:

```powershell
python .\scripts\existing-agent.py reset --approve-reset
python .\scripts\existing-agent.py probe --approve-probe
```

Restart loses demo in-memory carts. Deleting a cart does not clear the leaked
analytics buffers. Verify memory/errors and alert resolution separately.

### Scenario 2: Code-level RCA

Reset before reproducing the fault. Ask the code analyzer to correlate telemetry
with the indexed source, cite exact files/lines at the pinned revision, and
propose a detailed issue. Approve creation **only in the demo Grubify fork**.
No upstream issues, production changes, or code-fix PRs are required.

### Scenario 3: Customer-issue triage

```powershell
python .\scripts\existing-agent.py seed-issues --approve-issues
```

This creates at most four synthetic cases and skips existing exact titles. Use
the namespaced issue-triager or, if the service supports running a disabled task
on demand, run that task in the portal. If temporarily enabling is required,
approve it, run once, then disable it and record the changed state for cleanup.
The prompt restricts triage to issues with both `[Starter Demo]` and
`[Customer Issue]`. Approve labels/comments; verify classifications and rerun to
prove comments are not duplicated. Do not triage unrelated issues.

## 5. Reset, cleanup and release gate

Rehearse the fault/recovery cycle twice with fresh investigation context. Record
observation timeouts, failures, operator actions, approvals, and multiple
successful business probes. Keep cloud scenario results visibly pending until
actually measured. Capture a labeled recording only after a successful rehearsal.

After separate cleanup approval:

```powershell
python .\scripts\existing-agent.py cleanup --state-dir $State --approve-cleanup
```

Cleanup removes only ledger-owned agent additions and restores the prior incident
platform/managed-resource settings when still matching the approved change.
Externally changed objects stop cleanup instead of being overwritten or deleted.
If you deliberately changed a lab task for the rehearsal, reconcile that exact
change with the ledger before cleanup; do not bypass the guard by deleting state.
The helper never deletes a resource group, identity, or role assignment.

Verify the existing agent, ServiceNow integration, pre-existing schedules/code
bindings/global policy, and original access remain intact. Remove only tracked
new role assignments, inspect workload-only teardown scope, and then separately
approve deletion of the disposable workload. **Never delete `SRE-Demo` or run
blanket teardown against it.** Retain failed-cleanup evidence and list leftovers.
Synthetic GitHub issues remain as demo evidence unless separately approved for
closure. Tag a Phase 1 release only after live validation and cleanup pass.

## Offline validation

```powershell
python -m unittest discover -s .\tests -p 'test_*.py' -v
& 'C:\Program Files\Git\bin\bash.exe' tests/setup.tests.sh
az bicep build --file .\infra\main.bicep --outfile "$env:TEMP\starter-main.json"
```

The path-filtered workflow runs these checks without cloud credentials on Linux
and Windows. Mock tests prove safety/failure behavior, not live service behavior.
