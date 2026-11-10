# Grubify Application Architecture

## Overview

Grubify is a food ordering web application deployed on **Azure Container Apps**.
The customer-demo path uses a .NET 9 ASP.NET Core API and a separate React
frontend. Source repository, branch and exact commit come from the approved demo
manifest and the connected repository binding, not historical upstream examples.
Investigate only the workload and repository named in the subagent instructions.

---

## Infrastructure

| Component | Azure Service | Details |
|-----------|---------------|---------|
| **API** | Azure Container Apps | .NET 9 ASP.NET Core, port 8080, external HTTPS ingress |
| **Frontend** | Azure Container Apps | React static build served on port 80; Node.js 24 LTS is the build runtime |
| **Container Environment** | Container Apps Environment | Linked to Log Analytics |
| **Logs** | Log Analytics Workspace | Console logs via `ContainerAppConsoleLogs_CL` |
| **Telemetry** | Application Insights | API OpenTelemetry exports requests, exceptions and dependencies to the workload's workspace-backed instance |
| **Images** | Azure Container Registry | Both images built from the pinned source commit and deployed by immutable digest |
| **Agent** | Existing Azure SRE Agent | Reused separately from the workload; Review mode and existing identities preserved |
| **Alerts** | Azure Monitor | API HTTP 5xx metric alert with a minimal lab action group |

The reuse path does not create workload managed identities. Existing agent read
grants are preserved; any additional role requires explicit scope approval.
Instructions are not an Azure authorization boundary. Do not query the agent's
own operational telemetry instead of the named workload workspace/App Insights.

---

## Application Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/restaurants` | GET | List all restaurants |
| `/api/fooditems` | GET | List all food items / menu |
| `/api/orders/{id}` | GET | Get an order |
| `/api/orders/user/{userId}` | GET | List a user's orders |
| `/api/orders` | POST | Create a new order |
| `/api/cart/{userId}` | GET | Get a user's cart |
| `/api/cart/{userId}/items` | POST | Add item to user's cart (**memory leak trigger**) |
| `/api/cart/{userId}/items/{itemId}` | PUT / DELETE | Update or remove a cart item |
| `/api/cart/{userId}` | DELETE | Clear cart items; does not release the analytics cache |
| `/weatherforecast` | GET | Test endpoint — returns sample weather data |

There is no `/health` or `/admin/chaos` endpoint in the pinned API. A GET to `/`
returns a missing-route 404; use `/api/restaurants` and `/api/cart/{userId}` for
application checks.

---

## Fault Injection — Memory Leak via Cart API

`GrubifyApi/Controllers/CartController.cs` allocates a 10 MiB byte array on every
cart POST and retains it in a static `RequestDataCache` list without eviction.
The separate in-memory cart dictionary is not the allocation causing the fault.
Allocation can throw `System.OutOfMemoryException` and return HTTP 500.

An application allocation failure does not prove a platform OOM kill, container
restart, CPU saturation or that the 1 Gi memory limit was reached. Correlate
exception stacks, real metrics and revision events. The cache's logged size is
allocation arithmetic, not measured process working set.

Only the operator may run the approved bounded fault from the Starter lab folder:
```powershell
python .\scripts\existing-agent.py fault --approve-fault --requests 150 --interval 0.5
```

The helper stops after six server errors, even before the request cap. Never run
an unbounded load loop. An explicitly approved API-only revision restart clears
the process-local cache and cart state; wait for restart completion and verify
recovery. Restart is a mitigation, not a fix for the intentional leak.
---

## Source Code

- **Demo source:** [sandy12341/grubify](https://github.com/sandy12341/grubify),
  branch `demo/starter-source`; verify HEAD against [demo.json](../demo.json).
- **API language:** C# / .NET 9.
- **Frontend:** React, with Node.js 24 LTS used during image build.
- **Container images:** environment-specific ACR images pinned by digest, not a
  floating historical GHCR `latest` tag.

### Key Files

| File | Purpose |
|------|---------|
| `GrubifyApi/Program.cs` | ASP.NET Core startup, CORS and Azure Monitor OpenTelemetry |
| `GrubifyApi/Controllers/CartController.cs` | Cart routes and intentional retained allocation |
| `GrubifyApi/Controllers/RestaurantsController.cs` | Restaurant routes |
| `GrubifyApi/Controllers/FoodItemsController.cs` | Menu routes |
| `GrubifyApi/Controllers/OrdersController.cs` | Order routes |
| `GrubifyApi/Dockerfile` | API container build |
| `grubify-frontend/Dockerfile` | Frontend container build |

---

## Scaling Configuration

| Setting | Value |
|---------|-------|
| API min / max replicas | 1 / 5 |
| API CPU / memory | 0.5 cores / 1 Gi |
| Frontend min / max replicas | 1 / 3 |
| Frontend CPU / memory | 0.25 cores / 0.5 Gi |

These are template settings, not proof of the current replica count or resource
usage. No explicit HTTP concurrency threshold is declared in the template.
Read live configuration before proposing a change; scaling is approval-gated.

---

## Monitoring & Alerting

### Log Analytics Queries

**Error logs:**
```kql
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1h)
| where ContainerAppName_s == "<apiName>"
| where Log_s contains "error" or Log_s contains "Error" or Log_s contains "500"
| summarize ErrorCount = count() by bin(TimeGenerated, 5m)
| order by TimeGenerated desc
```

**Container app console output:**
```kql
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1h)
| where ContainerAppName_s == "<apiName>"
| project TimeGenerated, Log_s, ContainerName_s
| order by TimeGenerated desc
| take 50
```

### Alert Rules

| Alert | Trigger | Severity |
|-------|---------|----------|
| HTTP 5xx errors | > 5 requests with 5xx status in 5 min | Sev3 |

A single alert keeps things clean — the SRE Agent investigates the root cause (memory leak, OOM, code bug, etc.) regardless of which symptom triggered it.

Check the enabled metric alert, approved monitored resource scope, Azure Monitor
incident platform and matching namespaced response plan. The template's minimal
action group does not configure an external receiver; its presence alone is not
proof of SRE delivery. Verify a real alert creates an automatic incident handled
in Review mode; do not substitute a manually created conversation.

---

## Known Failure Modes

| Failure | Symptoms | Root Cause |
|---------|----------|------------|
| Retained allocation | Cart POST 500s and `OutOfMemoryException` at the allocation | Intentional unbounded static analytics cache |
| Missing route | 404 on an unsupported path | Incorrect probe path, not proof the API is down |
| Telemetry absent | Empty queries | Check source, schema and ingestion delay before concluding no activity |

Other hypotheses require evidence; do not label them as known failures of this
source revision.

---

## Troubleshooting Quick Reference

1. **Check responses:** GET `/api/restaurants` and `/api/cart/<demoUser>` on the API
2. **Check logs:** Query the named workload workspace, scoped to the API app
3. **Check metrics:** Azure Monitor → Container App → Requests, CPU, Memory
4. **Check source:** Verify the connected checkout's commit, then inspect the cart allocation
5. **Propose recovery:** Ask approval for an API-only restart; verify completion,
   successful business operations and the monitor condition separately
