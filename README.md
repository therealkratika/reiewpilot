# ReviewPilot

ReviewPilot is a monorepo for an AI-assisted GitHub pull-request review
workflow. The main flow is a React dashboard backed by the FastAPI agent
service.

## Architecture

```text
reviewpilot/
├── apps/
│   ├── web/                     # React + Vite dashboard
│   │   └── src/
│   │       ├── components/
│   │       ├── pages/
│   │       ├── services/
│   │       └── App.jsx
│   ├── agents/                  # Main FastAPI backend
│   │   ├── app/
│   │   ├── tests/
│   │   └── requirements.txt
│   └── api/                     # Milestone 1 Express scaffold (separate)
│       └── src/server.ts
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

`apps/api` is retained as a standalone scaffold and is not included in the
main Docker Compose flow. The dashboard calls `apps/agents` directly.

## Local development

Copy `.env.example` to `.env`, then set `GEMINI_API_KEY` and `GITHUB_TOKEN`.
Use a fine-grained GitHub token restricted to a test repository, and keep
secrets out of source control.

Run the agent service:

```bash
cd apps/agents
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
uvicorn app.main:app --reload --port 8000
```

Run the dashboard in another terminal:

```bash
cd apps/web
npm install
npm run dev
```

Alternatively, start the main flow with Docker Compose from the repository
root:

```bash
docker compose up --build
```

The dashboard is available at <http://localhost:5173>, the agent API at
<http://localhost:8000/docs>, and the separate Express scaffold can be run
from `apps/api` with `npm install && npm run dev`.

## Review pipeline

The agent exposes
`POST /v1/github/pulls/{owner}/{repo}/{pull_number}/review-pipeline`.
It fetches a pull request, reviews its diff, filters findings to added lines,
and can propose and validate a fix for one changed file. It does not
automatically publish comments or apply, commit, or push patches.

Publishing is a separate, explicit
`POST /v1/github/reviews/publish` operation. Inspect the result first and
provide the current `expected_head_sha`; its dry-run default is `true`.

## Human-approved fix publication

When a review is run with `propose_fix: true`, the response includes the
proposed patch, target file, updated file content, and the PR `head_sha` used
to generate it. The dashboard shows the patch and requires an explicit
confirmation before calling `POST /v1/github/fixes/publish`.

Fix publication uses GitHub's `createCommitOnBranch` GraphQL mutation. The
mutation includes `expectedHeadOid`, so GitHub rejects the commit if the
branch changed after review. The mutation contains exactly one file addition
for the validated changed file; it does not publish review comments or apply
local patches automatically. The endpoint defaults to `dry_run: true`.

The backend token needs repository contents write permission for the target
repository (for fine-grained tokens, **Contents: Read and write**). Pull
request read access is also required. Do not expose `GITHUB_TOKEN` to the
frontend. Test first with `dry_run: true`, then use a dedicated test
repository and confirm the commit on the PR branch manually.

## GitHub webhook setup

Set a random `GITHUB_WEBHOOK_SECRET` in the backend environment and configure
the same value in a GitHub repository webhook. Use the backend URL followed by
`/v1/github/webhooks`, select **application/json**, and subscribe to **Pull
requests**. ReviewPilot verifies `X-Hub-Signature-256` against the raw request
body before parsing it.

The webhook handles `opened`, `reopened`, and `synchronize` pull-request
actions. It extracts the repository, PR number, and head SHA, then reuses the
review pipeline with fix-proposal generation enabled and validation disabled.
It never commits, applies patches, or publishes review comments automatically.
Repeated events for the same repository, PR, head SHA, and action are ignored.

There is currently no durable job queue in this project. Webhook processing is
synchronous and uses a bounded, process-local idempotency store. This is
appropriate for local development only; production should put signature
verification and idempotency in front of a durable worker queue before
relying on webhook retries or multiple backend instances.

## Render deployment preparation

The repository includes [render.yaml](./render.yaml), which defines the
FastAPI service with `apps/agents` as its root directory:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`

Create the service from the Blueprint or configure those values manually.
Set `GEMINI_API_KEY`, `GITHUB_TOKEN`, and `GITHUB_WEBHOOK_SECRET` as secret
environment variables in Render. `GITHUB_API_URL` defaults to
`https://api.github.com`; set `WEB_ORIGIN` to the deployed frontend origin
when the dashboard is hosted outside local Vite.

The current Docker validator launches a `node:22-alpine` container through the
host Docker CLI. A native Render Python service should not be assumed to have
Docker-in-Docker or a usable Docker daemon, so fix validation will normally
return an unavailable result there. Do not expose a host Docker socket as a
shortcut. Use a separate trusted validation worker or an isolated external
job runner before enabling production fix validation.

The Render service does not automatically publish reviews, commit fixes, or
apply patches. Those actions remain explicit API operations requiring the
existing approval and SHA checks.
