# ReviewPilot

ReviewPilot is a monorepo for AI-assisted GitHub pull-request reviews. The
primary application is a React dashboard backed by a FastAPI service, Gemini,
GitHub, Redis/RQ, and SQLAlchemy persistence.

## Architecture

```text
apps/web/       React 19 + Vite dashboard
apps/agents/    FastAPI application, RQ worker, SQLAlchemy models/migrations,
                and pytest suite
apps/api/       Separate Express scaffold; not used by the main application
docker-compose.yml
render.yaml     API, worker, managed PostgreSQL, and Redis-compatible service
```

The web dashboard calls the FastAPI service. The API fetches GitHub pull
request data and asks Gemini to analyze the diff. Webhooks are verified and
enqueued as uniquely identified RQ jobs in Redis; a separate worker reviews
the pull request. SQLAlchemy stores repository and PR metadata, job/review
history, findings, fix proposals, and publication state. Alembic migrations
run when the FastAPI application starts.

Redis holds queued jobs, retry state, and short-lived worker leases. Review
and job records are stored in the database; they are not automatically
expired. The database does not store GitHub tokens or full pull request
diffs. See [the development audit](./docs/DEVELOPMENT_AUDIT.md) for known
limitations and highest-priority risks.

## Local setup

### Recommended: Docker Compose

1. Copy `.env.example` to `.env`.
2. Set `GEMINI_API_KEY`, `GITHUB_TOKEN`, and `GITHUB_WEBHOOK_SECRET` if you
   plan to use the corresponding features. Keep real credentials out of Git.
3. From the repository root, run:

   ```bash
   docker compose up --build
   ```

Compose starts PostgreSQL, Redis with append-only persistence, the FastAPI
service, its independent RQ worker, and the Vite development server. Open
<http://localhost:5173> for the dashboard and <http://localhost:8000/docs>
for the API. Compose persists database and Redis data in named volumes.
Its PostgreSQL password is for local development only; never reuse it in a
deployed environment.

Stop the services with `docker compose down`. This preserves named volumes.

### Run the backend and worker directly

Python 3.12 is the tested deployment runtime. Start Redis and PostgreSQL
locally (or start only those Compose services with
`docker compose up -d redis database`), then from `apps/agents`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
```

Use the same `REDIS_URL` and `DATABASE_URL` for the API and worker. For a
local Redis started by Compose, set `REDIS_URL=redis://localhost:6379/0`.
For a fully native development setup, the database defaults to
`sqlite:///data/reviewpilot.sqlite3`. With Redis and the database available,
run the following in separate terminals from `apps/agents`:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
python -m app.worker
```

The worker must run separately from the API for webhook jobs to be processed.
If Redis is unavailable or not configured, webhook requests fail with a
retryable `503`; jobs are not accepted into an in-memory fallback queue.

### Run the dashboard directly

In another terminal:

```bash
cd apps/web
npm ci
npm run dev
```

Set `VITE_API_URL` in `apps/web/.env` to the FastAPI origin if it is not
`http://localhost:8000`. `VITE_AGENTS_URL` remains a supported fallback.

The separate Express service under `apps/api` is a placeholder scaffold,
not part of the production review flow:

```bash
cd apps/api
npm ci
npm run dev
```

## Environment variables

The root `.env.example` is a safe template; it contains no credentials.
Compose passes the root `.env` to the backend and overrides the database and
Redis URLs to use its services. Set production secrets in the hosting
provider's secret/environment settings, not in source control or frontend
variables.

| Variable | Used by | Purpose and default |
|---|---|---|
| `GEMINI_API_KEY` | FastAPI | Gemini API credential. `GOOGLE_API_KEY` is accepted as a compatibility fallback. |
| `GEMINI_MODEL` | FastAPI | Optional shared model override. Without it, review uses `gemini-2.5-flash` and fix proposals use `gemini-3.8-flash`. |
| `GITHUB_TOKEN` | FastAPI/worker | GitHub API credential. Use a fine-grained token or GitHub App installation token with only required repository permissions: Pull requests read for lookup and write for review publication; Contents read when file-content access is needed and write to publish a fix commit. |
| `GITHUB_API_URL` | FastAPI/worker | GitHub API origin; defaults to `https://api.github.com`. |
| `GITHUB_WEBHOOK_SECRET` | FastAPI | Shared webhook HMAC secret. Required to accept GitHub webhooks. |
| `REDIS_URL` | FastAPI/worker | Redis connection for durable RQ jobs, retries, and worker leases. Required for webhook enqueueing and processing. |
| `DATABASE_URL` | FastAPI/worker | SQLAlchemy URL. Defaults to local SQLite at `data/reviewpilot.sqlite3`; use shared PostgreSQL for multi-process/production deployment. Legacy `postgres://` and `postgresql://` URLs are normalized to psycopg. |
| `DATABASE_PATH` | FastAPI/worker | SQLite file path used only when `DATABASE_URL` is unset; defaults to `data/reviewpilot.sqlite3`. |
| `WEB_ORIGIN` | FastAPI | Exact browser origin allowed by CORS, for example the deployed dashboard origin. Local Vite origin `http://localhost:5173` is always allowed. |
| `PORT` | FastAPI | Listening port for direct module startup; defaults to `8000`. Hosting platforms may provide it automatically. |
| `VITE_API_URL` | Vite dashboard | FastAPI base URL embedded at frontend build time; defaults to `http://localhost:8000`. |
| `VITE_AGENTS_URL` | Vite dashboard | Legacy fallback for `VITE_API_URL`; Compose uses this name. |
| `API_PORT` | Express scaffold only | Express scaffold port; defaults to `4000`. It does not configure FastAPI or Compose. |

Never put `GITHUB_TOKEN`, `GEMINI_API_KEY`, or `GITHUB_WEBHOOK_SECRET` in
`VITE_*` variables: frontend variables are bundled into browser-visible code.

## GitHub webhook configuration

1. Set a high-entropy `GITHUB_WEBHOOK_SECRET` for the backend.
2. In the GitHub repository webhook settings, use
   `https://<backend-host>/v1/github/webhooks` as the payload URL, choose
   **application/json**, and subscribe to **Pull requests**.
3. Store the same secret in GitHub's webhook configuration; never put the
   value in this README or commit it.

The API validates `X-Hub-Signature-256` against the raw request body and
accepts `opened`, `reopened`, and `synchronize` actions. It queues a review
job and responds with a job ID. Jobs can be inspected at
`GET /v1/github/jobs/{job_id}`. RQ retries failed jobs up to three times with
scheduled delays of 15, 60, and 180 seconds; the worker runs with its
scheduler enabled. RQ retains successful jobs for 30 days and failed jobs
for 90 days. Database deduplication by repository, PR number, and head SHA
has no automatic expiry, so repeat deliveries for a completed head remain
deduplicated permanently.

The worker checks the PR head and its token-fenced Redis lease before
publishing. When valid findings exist, webhook processing may publish a
comment-only review. It does not create fix commits. Review publication
markers prevent duplicate comments after retries where GitHub accepted a
review but the worker did not receive confirmation.

## API endpoints

The interactive OpenAPI reference is available at `/docs`.

| Method and path | Behavior |
|---|---|
| `GET /health` | Basic API liveness check; does not verify Redis or GitHub connectivity. |
| `POST /v1/github/webhooks` | Verify and enqueue supported GitHub pull-request events. |
| `GET /v1/github/jobs/{job_id}` | Read persisted and RQ queue status. |
| `GET /v1/github/pulls/{owner}/{repo}/{pull_number}` | Fetch pull-request metadata, diff, and changed files. |
| `POST /v1/github/pulls/{owner}/{repo}/{pull_number}/review-pipeline` | Analyze a pull request; optionally propose and validate a fix. |
| `POST /v1/reviews/analyze` | Analyze a supplied diff and validate finding locations. |
| `POST /v1/fixes/propose` | Generate a fix proposal from supplied source and findings. |
| `POST /v1/workflows/review-and-fix` | Analyze a diff and propose a structurally scoped fix. |
| `POST /v1/workflows/review-fix-validate` | Run the review/fix workflow and optional Docker-based JavaScript validation. |
| `POST /v1/validation/run` | Validate supplied JavaScript files in a constrained Docker container. |
| `POST /v1/github/reviews/publish` | Publish an explicitly supplied review; dry-run defaults to `true`. |
| `POST /v1/github/fixes/publish` | Publish a one-file fix commit; dry-run defaults to `true`, and non-dry-run requires `approved: true`. |

**Production security warning:** Except for GitHub webhook signature
verification, API routes currently have no caller authentication or
repository-scoped authorization. CORS is not authentication. The `approved`
field is a client-provided boolean, not proof of a user's identity or
approval. Do not expose write-capable publication routes to untrusted users
or the public internet until server-side authentication and authorization
are implemented. Use an appropriately restricted GitHub credential.

The Docker validator requires a working Docker daemon and supports JavaScript
syntax plus recognized Node test files. The native Render API service does
not provide a Docker daemon; validation there requires a separate isolated
worker/service.

## Testing and dependency pinning

Run the backend tests from `apps/agents`:

```bash
python -m pytest -q
```

Run frontend API tests and a production build from `apps/web`:

```bash
npm ci
npm test
npm run build
```

The Express scaffold can be typechecked separately from `apps/api` with
`npm ci && npm run typecheck`; it is not used by the main dashboard flow.
There is currently no configured lint command.

`apps/agents/requirements.txt` declares supported version ranges;
`requirements-lock.txt` pins the Python 3.12 deployment/test environment.
CI, the backend Docker image, and Render install from the lockfile. The web
and Express projects use their committed `package-lock.json` files with
`npm ci`. GitHub Actions runs backend tests, frontend tests/build, and the
Express scaffold typecheck plus high-severity npm dependency audits for
pushes and pull requests targeting `main`.

## Docker and Render deployment

The backend [Dockerfile](./apps/agents/Dockerfile) builds the API/worker
image using the pinned Python lockfile and includes Alembic migrations.
Docker Compose additionally starts a Node 22 development container for the
dashboard and local PostgreSQL/Redis services. There is no production
frontend container image; deploy the static Vite build separately if needed,
setting `VITE_API_URL` at build time.

The [Render Blueprint](./render.yaml) provisions the FastAPI web service, a
separate RQ worker, managed PostgreSQL, and a Redis-compatible Key Value
service. It does not provision the frontend. To deploy:

1. Create the Render services from the Blueprint.
2. Add `GEMINI_API_KEY`, `GITHUB_TOKEN`, and `GITHUB_WEBHOOK_SECRET` as
   private environment values for both API and worker.
3. Set `WEB_ORIGIN` to the exact deployed dashboard origin; the Blueprint
   intentionally prompts for this rather than allowing localhost by default.
4. If deploying the dashboard, build `apps/web` using `npm ci && npm run
   build` and set `VITE_API_URL` to the public API origin.
5. Configure the GitHub webhook URL and secret, then verify `/health` and
   inspect a webhook job through its status endpoint.

The API and worker must share the same PostgreSQL and Redis URLs. Alembic
migrations run on API startup. Back up PostgreSQL and use backward-compatible
migrations. Confirm the selected Redis service's persistence and recovery
guarantees meet queue durability needs. The Render API health check is
liveness-only; it does not signal queue/database readiness.

Before production, address the missing API authentication/authorization
described above. Keep provider credentials in Render's environment settings,
restrict their repository and operation permissions, and do not reuse the
local Compose database password.
