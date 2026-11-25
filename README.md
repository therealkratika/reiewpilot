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
├── render.yaml
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
<http://localhost:8000/docs>. Compose starts PostgreSQL, Redis, the API, and a
separate RQ worker. PostgreSQL and Redis data use named Docker volumes. The
separate Express scaffold can be run from `apps/api` with
`npm install && npm run dev`.

The backend uses Alembic migrations and SQLAlchemy persistence for observed
repositories, pull requests, webhook job status, review history, findings,
fix proposals, and publication status. It stores only the metadata and
proposal data needed by these features, not GitHub tokens or full PR diffs.
For local development without Compose, `DATABASE_URL` defaults to
`sqlite:///data/reviewpilot.sqlite3`; configure a reachable Redis instance in
`REDIS_URL` to accept webhook jobs. Missing or unavailable Redis causes the
webhook endpoint to return a retryable `503` rather than accepting a volatile
background task.

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
actions. The endpoint verifies the signature, persists a uniquely identified
job, and enqueues it in Redis-backed RQ; a separate worker performs the review.
Jobs use a finite retry policy and can be inspected at
`GET /v1/github/jobs/{job_id}`. RQ retains successful and failed job records
for 30 and 90 days respectively; application job and review history is stored
in the database. Database job identity and review history currently have no
automatic expiry, so the repository/PR/head-SHA deduplication remains
permanent; define a database retention policy before operating at high volume.

The worker checks the PR head SHA and its token-fenced Redis lease immediately
before publishing. It publishes only a comment review when valid findings
exist; it never commits or applies a fix. A GitHub review marker prevents
duplicate comments if a worker retries after an uncertain API response.
Repeated deliveries for the same repository, PR, and head SHA are deduplicated.
The RQ worker must be deployed separately from the API, and Redis must be
configured with persistence appropriate for the deployment.

## Render deployment preparation

The repository includes [render.yaml](./render.yaml), which defines the
FastAPI service and RQ worker with `apps/agents` as their root directory, plus
managed PostgreSQL and Redis-compatible Key Value services:

- Build: `pip install --only-binary=:all: -r requirements-lock.txt`
- API: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Worker: `python -m app.worker`
- Health check: `/health`

Create the service from the Blueprint or configure those values manually.
Set `GEMINI_API_KEY`, `GITHUB_TOKEN`, and `GITHUB_WEBHOOK_SECRET` as secret
environment variables in Render. `GITHUB_API_URL` defaults to
`https://api.github.com`; set `WEB_ORIGIN` to the deployed frontend origin
when the dashboard is hosted outside local Vite.

The API and worker share the managed PostgreSQL database and Redis service.
Alembic migrations run when the API starts. Back up the database and use
backward-compatible migrations when deploying schema changes.

The current Docker validator launches a `node:22-alpine` container through the
host Docker CLI. A native Render Python service should not be assumed to have
Docker-in-Docker or a usable Docker daemon, so fix validation will normally
return an unavailable result there. Do not expose a host Docker socket as a
shortcut. Use a separate trusted validation worker or an isolated external
job runner before enabling production fix validation.

Webhook-triggered reviews may publish comment-only reviews when findings are
present. Fix commits are never created by the webhook worker; they remain an
explicit API action requiring approval and current-head checks.
