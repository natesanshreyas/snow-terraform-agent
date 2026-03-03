# Snow → Terraform Provisioning Agent

End-to-end ITSM automation agent that reads a ServiceNow infrastructure request
ticket, generates Terraform configuration from GitHub module examples, evaluates
the generated IaC with three LLM judges (security, compliance, quality), opens a
pull request, and updates the ticket — all orchestrated via three MCP servers.

## Architecture

```
ServiceNow ticket (RITM)
        │
        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  APIM Gateway  (auth, rate limiting, routing)           │
  └─────────────────────┬───────────────────────────────────┘
                        │
                        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Azure Service Bus  (queue message with ticket ID)      │
  └─────────────────────┬───────────────────────────────────┘
                        │
                        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  ASB Consumer  (worker process picks up message)        │
  │  Blob State Store  (tracks job status)                  │
  └─────────────────────┬───────────────────────────────────┘
                        │
                        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Provisioning Agent  (LLM reasoning loop)               │
  │                                                         │
  │  [snow MCP]   → read ticket, validate, update w/ PR URL │
  │  [azure MCP]  → inventory resource groups (naming)      │
  │  [github MCP] → find repo → read .tf examples           │
  │                → create branch → push HCL → open PR     │
  └─────────────────────┬───────────────────────────────────┘
                        │
                        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Terraform Evaluator  (3 LLM judges)                    │
  │  Security │ Compliance │ Quality  — each scores 1-5     │
  │  Pass threshold ≥ 3  │  Up to 2 retries on failure      │
  └─────────────────────┬───────────────────────────────────┘
                        │
                        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Azure AI Foundry  (telemetry + evaluation logging)     │
  └─────────────────────────────────────────────────────────┘
                        │
                        ▼
   GitHub PR + SNOW work note  →  Awaiting human review
```

### MCP Servers

Three MCP servers are started simultaneously at request time:

| Server | Default command | Handles |
|---|---|---|
| `snow` | `npx -y @servicenow/now-ai-kit@latest` | Read/update SNOW records |
| `github` | `npx -y @modelcontextprotocol/server-github` | Repo, files, branches, PRs |
| `azure` | `@azure/mcp` (see env) | Azure resource inventory |

### Dual Execution Modes

| Mode | Trigger | How it works |
|---|---|---|
| **Async** (production) | `POST /api/provision` when ASB is configured | Enqueues to Azure Service Bus → ASB Consumer picks up → runs agent → stores result in Blob |
| **Sync** (development) | `POST /api/provision` when ASB is not configured | Runs agent inline and returns result directly |

## Quick Start

```bash
cd snow-terraform-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your ServiceNow, GitHub, and Azure OpenAI credentials

# Start the server
uvicorn src.main:app --host 0.0.0.0 --port 8020 --reload
```

Open **http://localhost:8020** in a browser, enter a ServiceNow RITM number, and click **Run Provisioning**.

## API

```
POST /api/provision
  Body: {"ticket_id": "RITM0001234", "max_iterations": 15}
  Async mode → {"job_id": "...", "status": "queued"}
  Sync mode  → {"ticket_id": "...", "pr_url": "...", "summary": "...", ...}

GET  /api/provision/{job_id}/status
  Returns: {"status": "running|completed|failed", "result": {...}}

GET  /health  →  {"status": "ok"}
GET  /        →  UI (ui.html)
```

## Workflow

1. `snow__*` — read ticket: extract `short_description`, `approval_state`, `cost_center`
2. Validate `approval_state == "approved"` — returns `blocked` if not
3. `azure__*` — list resource groups for naming context
4. `github__*` — find the `terraform-modules-demo` repository
5. `github__*` — read an example `.tf` file matching the requested resource type
6. LLM generates `main.tf` + `variables.tf` using the example as a template
7. **Terraform Evaluator** — 3 LLM judges (security, compliance, quality) each score 1–5
   - All scores ≥ 3 → pass, continue to step 8
   - Any score < 3 → LLM regenerates HCL (up to 2 retries), then back to step 7
8. `github__*` — create branch `feature/provision-{ticket_id}`
9. `github__*` — push `main.tf`
10. `github__*` — push `variables.tf`
11. `github__*` — open pull request with ticket context in the description
12. `snow__*` — add work note to ticket with PR URL
13. Evaluation scores + telemetry logged to Azure AI Foundry

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | ✓ | Azure OpenAI endpoint |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | ✓ | Model deployment (e.g. `gpt-4.1`) |
| `AZURE_OPENAI_USE_AZURE_AD` | | `true` (default) or `false` |
| `AZURE_OPENAI_API_KEY` | | Required if `USE_AZURE_AD=false` |
| `SNOW_MCP_COMMAND` | | ServiceNow MCP start command |
| `SNOW_INSTANCE` | ✓ | e.g. `https://devXXXXXX.service-now.com` |
| `SNOW_USER` | ✓ | ServiceNow username |
| `SNOW_PASSWORD` | ✓ | ServiceNow password |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | ✓ | GitHub PAT (repo + workflow scopes) |
| `GITHUB_ORG` | ✓ | GitHub org/user owning the Terraform repo |
| `GITHUB_TERRAFORM_REPO` | | Terraform modules repo (default: `terraform-modules-demo`) |
| `AZURE_MCP_SERVER_COMMAND` | ✓ | Full path to `npx @azure/mcp@latest server start` |
| `AZURE_SUBSCRIPTION_ID` | ✓ | Azure subscription for resource checks |
| `AZURE_SERVICE_BUS_NAMESPACE` | | ASB namespace for async mode (omit for sync) |
| `AZURE_SERVICE_BUS_QUEUE` | | ASB queue name (default: `provision-requests`) |
| `AZURE_STORAGE_ACCOUNT_NAME` | | Blob storage for job state (async mode) |
| `AZURE_STORAGE_CONTAINER` | | Blob container (default: `provision-jobs`) |
| `AZURE_AI_FOUNDRY_PROJECT` | | Foundry project connection string for telemetry |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | | App Insights for OpenTelemetry traces |

## Demo Terraform Repo

The agent reads from [`natesanshreyas/terraform-modules-demo`](https://github.com/natesanshreyas/terraform-modules-demo) which contains:

```
modules/
  resource-group/     main.tf, variables.tf
  storage-account/    main.tf, variables.tf, outputs.tf
  openai/             main.tf, variables.tf
examples/
  storage-account-example/  main.tf  ← agent reads for few-shot context
  openai-example/           main.tf
```

## File Structure

```
snow-terraform-agent/
├── src/
│   ├── __init__.py
│   ├── main.py                 FastAPI app, dual-mode API (async/sync)
│   ├── provisioning_agent.py   LLM agent loop with eval-retry logic
│   ├── multi_mcp_client.py     MultiMCPClient — manages 3 MCP servers
│   ├── terraform_evaluator.py  3 LLM judges (security/compliance/quality)
│   ├── asb_consumer.py         Azure Service Bus worker process
│   ├── asb_sender.py           Enqueue messages to ASB
│   ├── blob_store.py           Azure Blob state store for job tracking
│   ├── telemetry.py            Azure AI Foundry + OpenTelemetry integration
│   ├── openai_client.py        Azure OpenAI wrapper with retry
│   └── ui.html                 Browser UI
├── requirements.txt
├── .env.example
└── README.md
```
