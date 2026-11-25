# ReviewPilot Development Audit

**Updated:** 2026-11-25
**Scope:** Current application architecture, AI/GitHub workflows, reliability,
storage, frontend, CI, dependencies, containers, deployment files, and user
documentation.

## Architecture and highest-priority risks

The primary flow is a React/Vite dashboard calling a FastAPI service. FastAPI
uses Gemini for review and fix proposals, and GitHub REST/GraphQL APIs for
pull-request reads and explicitly requested publication. Signed GitHub
webhooks are queued in Redis-backed RQ; a separate worker processes reviews
with token-fenced Redis leases. SQLAlchemy/Alembic persists repository and PR
metadata, job status, review history, findings, fix proposals, and publication
status. Docker Compose provides local PostgreSQL, Redis, API, worker, and web
services. Render configures the API and worker with managed database and
Redis-compatible services. `apps/api` remains a separate Express scaffold,
not part of the primary flow.

**Highest priority:** The API has no caller authentication or
repository-scoped authorization. The GitHub webhook is signature-verified,
but other routes can expose PR data, consume Gemini/GitHub quotas, publish
reviews, or create commits using the server credential. The request's
`approved` boolean is not server-verifiable human authorization. Do not
expose write-capable routes to untrusted callers or the public internet until
server-side authentication, authorization, repository restrictions, and
rate/cost controls are implemented.

## Audit checklist

| Feature | Current implementation | Status: Complete / Partial / Missing / Needs verification | Problems | Recommended changes | Relevant file paths |
|---|---|---|---|---|---|
| Pull-request retrieval and analysis | FastAPI fetches PR metadata, diff, and paginated changed files; Gemini returns structured findings which are validated against added lines. | Partial | No caller auth or model-cost limits. Finding-range checks do not prove every line in a multiline comment range is valid. | Add authorization, quotas, and tests for edge-case diffs and full comment-range rules. | `apps/agents/app/pipeline.py`, `reviewer.py`, `diff_parser.py`, `github_client.py`, `main.py` |
| Fix proposal and patch scope | Gemini proposes a complete file with assumptions/tests; workflow generates a unified diff and checks that only the requested file is targeted. | Partial | Structural scope validation is not semantic validation; proposed fixes are not guaranteed safe or correct. | Preserve explicit review, tests, and approval; show original and updated content together and report validation limits. | `apps/agents/app/fixer.py`, `workflow.py`, `schemas.py`, `apps/web/src/pages/Dashboard.jsx` |
| GitHub review publication | Explicit route validates current SHA and added-line comment locations, scans paginated reviews for an idempotency marker, and supports dry-run. | Partial | Route accepts caller-provided review content without auth. Marker lookup is not atomic across concurrent publishers. | Authenticate/authorize publishers, bind content to persisted review results, and make publication idempotency concurrency-safe. | `apps/agents/app/github_service.py`, `github_client.py`, `main.py` |
| GitHub fix publication | Fix route checks open PR, current SHA, changed-file membership, and human-confirmation boolean; GraphQL commit uses expected head SHA and changes one file. | Partial | Client can set `approved: true`; there is no user identity or repository authorization. Server token may have write access. | Block public use pending authenticated approval and scoped credentials; audit every write operation. | `apps/agents/app/github_service.py`, `github_client.py`, `schemas.py`, `main.py`, `apps/web/src/pages/Dashboard.jsx` |
| API security | Webhook HMAC and CORS are configured; payload schemas impose field limits. | Missing | Non-webhook routes have no caller authentication, repository allowlist, or rate limiting. CORS is not an access-control boundary. | Treat as a production release blocker; implement identity, repository permissions, abuse controls, and audit logging. | `apps/agents/app/main.py`, `schemas.py`, `apps/web/src/services/api.js` |
| Webhook signature and event handling | Raw-body HMAC verification, PR action filtering, bounded request body, and durable queue enqueueing are implemented. | Complete | Delivery is deduplicated by repository/PR/head SHA, regardless of action; this is intentional for the same reviewed head but means a completed head is not revisited. | Keep behavior documented and introduce a database retention/reenqueue policy if long-term event history grows or same-head reruns are needed. | `apps/agents/app/webhook.py`, `main.py`, `job_queue.py`, `apps/agents/tests/test_webhook.py` |
| Durable jobs and worker leases | RQ stores jobs in Redis; retries use scheduled intervals; independent worker uses token-fenced lease claim/refresh/release and checks ownership before publishing. | Partial | Actual Redis/process-kill recovery and provider outage behavior have not been integration-tested in deployment. Redis durability depends on the selected service. | Verify Redis persistence/recovery guarantees and monitor failed jobs, retries, worker health, and queue depth. | `apps/agents/app/job_queue.py`, `tasks.py`, `worker.py`, `webhook.py`, `render.yaml` |
| Database persistence | Alembic migrations and SQLAlchemy models persist repositories, PRs, jobs, reviews, findings, fixes, and publication status; SQLite is local fallback and PostgreSQL is shared deployment storage. | Partial | Application job/dedup records have no expiry; PostgreSQL deployment migration was not verified against a live server in this audit. | Establish retention and backup policies; test migrations against the deployed PostgreSQL major version before rollout. | `apps/agents/app/database.py`, `models.py`, `apps/agents/migrations/`, `docker-compose.yml`, `render.yaml` |
| Dashboard | React UI runs reviews and displays findings, fix proposals, validation, and publication results; API errors are surfaced in the UI. | Partial | History/settings navigation and service-connected indicator are placeholders; there is no authenticated user session. | Implement or remove placeholder navigation and connection status; integrate authentication before public deployment. | `apps/web/src/pages/Dashboard.jsx`, `ReviewForm.jsx`, `services/api.js` |
| Frontend API failures | API client reports backend JSON details, text errors, malformed JSON, and network failures; Node tests cover these cases. | Complete | Tests exercise API service behavior, not browser-level UI rendering or user interaction. | Add component/browser tests when the UI workflows expand. | `apps/web/src/services/api.js`, `apps/web/tests/api.test.js`, `apps/web/package.json` |
| Docker-based validation | Input paths and size/count are bounded; Node checks run in a network-disabled, constrained Docker container. | Partial | JavaScript-only and requires Docker daemon. Native Render API has no configured Docker daemon or separate validator. | Use a separately isolated validation worker for hosted validation; do not expose the host Docker socket. | `apps/agents/app/validator.py`, `main.py`, `apps/agents/Dockerfile`, `render.yaml` |
| Express API scaffold | Standalone Express placeholder has health and schema-validated request routes. | Partial | Not used by the dashboard or deployment; returns a placeholder queue response. | Keep clearly marked as a scaffold or remove only after its intended lifecycle is decided. | `apps/api/src/server.ts`, `apps/api/package.json` |
| Environment and secrets | `.env.example` contains empty secret placeholders; `.gitignore` excludes `.env`; Render expects provider credentials in environment configuration. | Partial | `WEB_ORIGIN` must be set to the deployed dashboard; frontend variables are public; Compose database credentials are development-only. | Keep secrets server-side, set exact production origin, use least-privilege GitHub credentials, and rotate any accidentally exposed credentials. | `.env.example`, `apps/web/.env.example`, `.gitignore`, `docker-compose.yml`, `render.yaml` |
| Dependency reproducibility | Python ranges are resolved in a pinned Python lockfile; Docker, Render, and CI use it. Web and scaffold use npm lockfiles; Compose uses `npm ci`; CI runs npm audit. | Partial | No automated Python dependency audit/update review; Node runtime is pinned to major 22 rather than exact patch. | Add routine Python dependency review and use compatible runtime updates; regenerate locks through package managers. | `apps/agents/requirements.txt`, `requirements-lock.txt`, `apps/web/package-lock.json`, `apps/api/package-lock.json`, `docker-compose.yml`, `.github/workflows/backend_test.yml` |
| CI and test coverage | GitHub Actions runs backend tests, frontend API tests/build, npm high-severity audits, and Express typecheck on pushes/PRs to `main`. | Partial | CI does not test live Redis/PostgreSQL, build the backend image, lint, or run browser-level tests. | Add integration/deployment checks where services are available and adopt lint/browser suites if project needs warrant them. | `.github/workflows/backend_test.yml`, `apps/agents/tests/`, `apps/web/tests/`, `apps/api/package.json` |
| Render deployment | Blueprint provisions FastAPI, separate RQ worker, managed PostgreSQL, and Redis-compatible Key Value; deployment origins are operator-configured. | Partial | Frontend is not provisioned. Health endpoint is liveness-only; Docker validation is unavailable on the native Python service. | Deploy frontend separately; set `VITE_API_URL` and `WEB_ORIGIN`; verify service persistence, readiness, backups, and monitoring. | `render.yaml`, `README.md`, `apps/agents/app/main.py` |
| Documentation accuracy | README documents architecture, setup, environment, queue, endpoints, tests, Docker, and Render deployment. | Complete | Documentation cannot substitute for authentication, actual production configuration, or live-service verification. | Keep the guide and this audit synchronized when routes, services, variables, or production behavior change. | `README.md`, `docs/DEVELOPMENT_AUDIT.md` |

## Verification

- Backend: `cd apps/agents && python3 -m pytest -q` — **78 passed**.
- Frontend: `cd apps/web && npm test` — **4 passed**.
- Frontend: `cd apps/web && npm run build` — **passed**.
- Express scaffold: `cd apps/api && npm run typecheck` — **passed** with Zod 4.6.7.
- `npm audit --audit-level=high` in both Node projects — **0 vulnerabilities**.
- `docker compose build agents worker` — **passed** using the pinned Python lockfile.
- Docker Compose, Render, and GitHub Actions YAML parsing plus `git diff --check` — **passed**.
- Render/Redis/PostgreSQL services: **not live-deployment tested**.
- Backend test output includes deprecation warnings from the installed
  Google GenAI typing integration and Starlette/httpx test-client integration.

## Recommended remediation order

1. Implement server-side authentication, repository-scoped authorization,
   rate limits, and auditable approval before public exposure of GitHub write
   operations.
2. Verify production Redis durability and PostgreSQL migration/backup/
   retention procedures; exercise worker restart and queue recovery against
   deployed service versions.
3. Configure and deploy the frontend separately, setting its public API URL
   and the API's exact allowed origin.
4. Add readiness/monitoring and, if required, an isolated hosted validator.
5. Improve UI navigation/status and add browser-level tests as those features
   become real product workflows.
