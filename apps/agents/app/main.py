
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import ReviewDatabase
from .diff_parser import validate_finding_locations
from .fixer import make_unified_diff, propose_fix
from .github_client import GitHubAPIError
from .github_service import publish_fix, pull_request_snapshot, publish_review
from .job_queue import (
    QueueUnavailableError,
    enqueue_webhook_review,
    queued_job_status,
)
from .pipeline import run_github_review_pipeline
from .reviewer import analyze_diff
from .schemas import (
    FixInput,
    GitHubFixPublishInput,
    GitHubPipelineInput,
    GitHubPublishInput,
    ReviewFixValidateInput,
    ReviewInput,
    ValidationInput,
    WorkflowInput,
)
from .validator import run_docker_validation
from .workflow import run_review_and_fix, run_review_fix_validate
from .webhook import (
    parse_pull_request_event,
    verify_signature,
)


def _find_env_file() -> Path | None:
    current = Path(__file__).resolve()

    for parent in current.parents:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate

    return None


env_file = _find_env_file()

if env_file:
    load_dotenv(env_file)
else:
    load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reviewpilot")

@asynccontextmanager
async def lifespan(_app):
    ReviewDatabase().initialize()
    yield


app = FastAPI(
    title="ReviewPilot Agent Service",
    version="0.8.0",
    lifespan=lifespan,
)


# CORS configuration
configured_web_origin = os.getenv("WEB_ORIGIN")

allowed_origins = ["http://localhost:5173"]

if configured_web_origin and configured_web_origin not in allowed_origins:
    allowed_origins.append(configured_web_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health check
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "reviewpilot-agents",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# GitHub webhook endpoint
@app.post("/v1/github/webhooks")
async def github_webhook(request: Request):
    max_body_bytes = 1_000_000
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_body_bytes:
                raise HTTPException(status_code=413, detail="Webhook body is too large.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from exc

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_body_bytes:
            raise HTTPException(status_code=413, detail="Webhook body is too large.")
        body.extend(chunk)
    raw_body = bytes(body)

    secret = os.getenv("GITHUB_WEBHOOK_SECRET")

    if not secret:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_WEBHOOK_SECRET is not configured.",
        )

    # Verify GitHub signature
    if not verify_signature(
        raw_body,
        request.headers.get("x-hub-signature-256"),
        secret,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature.",
        )

    # Accept only pull request events
    if request.headers.get("x-github-event") != "pull_request":
        return JSONResponse(
            {
                "status": "ignored",
                "reason": "Unsupported GitHub event.",
            },
            status_code=202,
        )

    # Parse webhook payload
    try:
        event = parse_pull_request_event(raw_body)

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    # Filter relevant actions
    if event["action"] not in {
        "opened",
        "reopened",
        "synchronize",
    }:
        return JSONResponse(
            {
                "status": "ignored",
                "reason": "Pull request action is not review-triggering.",
            },
            status_code=202,
        )

    try:
        job_id, enqueued = enqueue_webhook_review(event)
    except QueueUnavailableError as exc:
        logger.exception("Could not enqueue GitHub webhook review")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not persist or enqueue GitHub webhook review")
        raise HTTPException(
            status_code=503,
            detail="Webhook job service is unavailable. GitHub may retry this delivery.",
        ) from exc

    return JSONResponse(
        {
            "status": "accepted" if enqueued else "duplicate",
            "message": "Pull request review queued." if enqueued else "This pull request head is already queued or processed.",
            "job_id": job_id,
            "repository": f"{event['owner']}/{event['repo']}",
            "pull_number": event["pull_number"],
            "head_sha": event["head_sha"],
            "action": event["action"],
            "publishing": True,
        },
        status_code=202,
    )


@app.get("/v1/github/jobs/{job_id}")
def get_webhook_job(job_id: str):
    job = ReviewDatabase().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Webhook job was not found.")
    try:
        queue_status = queued_job_status(job_id)
    except QueueUnavailableError:
        queue_status = None
    if queue_status:
        job["queue_status"] = queue_status
        status_map = {
            "queued": "queued",
            "started": "started",
            "scheduled": "retrying",
            "deferred": "queued",
            "finished": "completed",
            "failed": "failed",
            "stopped": "failed",
            "canceled": "failed",
        }
        if queue_status in status_map:
            job["status"] = status_map[queue_status]
    return job


# Provider error handling
def provider_error(exc: Exception, component: str):
    if isinstance(exc, RuntimeError) and (
        "API_KEY is not configured" in str(exc)
        or "GITHUB_TOKEN is not configured" in str(exc)
    ):
        return HTTPException(
            status_code=503,
            detail=str(exc),
        )

    try:
        from google.genai.errors import APIError

        if isinstance(exc, APIError):
            status = (
                exc.code
                if exc.code in (400, 401, 403, 404, 429)
                else 502
            )

            return HTTPException(
                status_code=status,
                detail=f"{component} error ({exc.code}): {exc.message}",
            )

    except ImportError:
        pass

    logger.exception("%s failed", component)

    return HTTPException(
        status_code=502,
        detail=f"{component} failed. Check server logs.",
    )


# Analyze code review
@app.post("/v1/reviews/analyze")
def analyze_review(payload: ReviewInput):
    try:
        analysis = analyze_diff(payload.diff)

    except Exception as exc:
        raise provider_error(exc, "Reviewer") from exc

    accepted, warnings = validate_finding_locations(
        analysis.findings,
        payload.diff,
    )

    return JSONResponse(
        content={
            "repository": payload.repository,
            "pull_request_number": payload.pull_request_number,
            "head_sha": payload.head_sha,
            "model": os.getenv(
                "GEMINI_MODEL",
                "gemini-2.5-flash",
            ),
            "summary": analysis.summary,
            "findings": [
                finding.model_dump()
                for finding in accepted
            ],
            "rejected_findings": (
                len(analysis.findings) - len(accepted)
            ),
            "validation_warnings": warnings,
        }
    )


# Generate fix proposal
@app.post("/v1/fixes/propose")
def create_fix_proposal(payload: FixInput):
    try:
        proposal = propose_fix(payload)

    except Exception as exc:
        raise provider_error(exc, "Fixer") from exc

    patch = make_unified_diff(
        payload.file_path,
        payload.original_code,
        proposal.updated_code,
    )

    return JSONResponse(
        content={
            "status": (
                "proposal_ready"
                if patch
                else "no_changes_proposed"
            ),
            "file_path": payload.file_path,
            "explanation": proposal.explanation,
            "patch": patch,
            "tests_to_add": proposal.tests_to_add,
            "assumptions": proposal.assumptions,
            "applied": False,
            "validated": False,
        }
    )


# Review and fix workflow
@app.post("/v1/workflows/review-and-fix")
def review_and_fix(payload: WorkflowInput):
    try:
        return JSONResponse(
            content=run_review_and_fix(payload)
        )

    except Exception as exc:
        raise provider_error(
            exc,
            "Review-and-fix workflow",
        ) from exc


# Review, fix and validate workflow
@app.post("/v1/workflows/review-fix-validate")
def review_fix_validate(payload: ReviewFixValidateInput):
    try:
        return JSONResponse(
            content=run_review_fix_validate(payload)
        )

    except Exception as exc:
        raise provider_error(
            exc,
            "Review-fix-validate workflow",
        ) from exc


# Docker code validation
@app.post("/v1/validation/run")
def validate_code(payload: ValidationInput):
    try:
        result = run_docker_validation(
            payload.entry_file,
            payload.files,
            payload.run_tests,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logger.exception("Validation worker failed")

        raise HTTPException(
            status_code=500,
            detail="Validation worker failed. Check server logs.",
        ) from exc

    return JSONResponse(
        content=result,
        status_code=(
            200 if result["status"] == "passed" else 422
        ),
    )


# Get GitHub pull request details
@app.get("/v1/github/pulls/{owner}/{repo}/{pull_number}")
def get_github_pull(
    owner: str,
    repo: str,
    pull_number: int,
):
    if pull_number < 1:
        raise HTTPException(
            status_code=400,
            detail="pull_number must be positive.",
        )

    try:
        return JSONResponse(
            content=pull_request_snapshot(
                owner,
                repo,
                pull_number,
            )
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except GitHubAPIError as exc:
        status = (
            exc.status_code
            if exc.status_code in (400, 401, 403, 404, 429)
            else 502
        )

        raise HTTPException(
            status_code=status,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logger.exception("GitHub PR lookup failed")

        raise HTTPException(
            status_code=502,
            detail="GitHub PR lookup failed. Check server logs.",
        ) from exc


# Publish GitHub review
@app.post("/v1/github/reviews/publish")
def publish_github_review(payload: GitHubPublishInput):
    try:
        result = publish_review(payload)
        ReviewDatabase().update_review_publication(
            payload.owner,
            payload.repo,
            payload.pull_number,
            payload.expected_head_sha,
            result,
        )

        code = (
            409
            if result["status"] == "stale_head_sha"
            or result["status"] == "closed_pr"
            else 422
            if result["status"] == "rejected_invalid_locations"
            else 200
        )

        return JSONResponse(
            content=result,
            status_code=code,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except GitHubAPIError as exc:
        status = (
            exc.status_code
            if exc.status_code in (
                400, 401, 403, 404, 409, 422, 429
            )
            else 502
        )

        raise HTTPException(
            status_code=status,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logger.exception("GitHub review publish failed")

        raise HTTPException(
            status_code=502,
            detail="GitHub review publish failed. Check server logs.",
        ) from exc


# Publish GitHub fix
@app.post("/v1/github/fixes/publish")
def publish_github_fix(payload: GitHubFixPublishInput):
    try:
        result = publish_fix(payload)
        ReviewDatabase().update_fix_publication(
            payload.owner,
            payload.repo,
            payload.pull_number,
            payload.expected_head_sha,
            payload.file_path.removeprefix("a/").removeprefix("b/"),
            result,
        )

        code = {
            "stale_head_sha": 409,
            "closed_pr": 409,
            "invalid_file_path": 400,
            "approval_required": 400,
        }.get(result["status"], 200)

        return JSONResponse(
            content=result,
            status_code=code,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except GitHubAPIError as exc:
        status = (
            exc.status_code
            if exc.status_code in (
                400, 401, 403, 404, 409, 422, 429
            )
            else 502
        )

        raise HTTPException(
            status_code=status,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logger.exception("GitHub fix publish failed")

        raise HTTPException(
            status_code=502,
            detail="GitHub fix publish failed. Check server logs.",
        ) from exc


# GitHub pull request review pipeline
@app.post(
    "/v1/github/pulls/{owner}/{repo}/{pull_number}/review-pipeline"
)
def github_review_pipeline(
    owner: str,
    repo: str,
    pull_number: int,
    payload: GitHubPipelineInput,
):
    if pull_number < 1:
        raise HTTPException(
            status_code=400,
            detail="pull_number must be positive.",
        )

    try:
        result = run_github_review_pipeline(
            owner,
            repo,
            pull_number,
            payload,
        )
        ReviewDatabase().save_review_result(
            {
                "owner": owner,
                "repo": repo,
                "pull_number": pull_number,
                "head_sha": (result.get("pull_request") or {}).get("head_sha", ""),
                "action": "manual",
            },
            result,
        )

        return JSONResponse(content=result)

    except GitHubAPIError as exc:
        status = (
            exc.status_code
            if exc.status_code in (400, 401, 403, 404, 429)
            else 502
        )

        raise HTTPException(
            status_code=status,
            detail=str(exc),
        ) from exc

    except RuntimeError as exc:
        raise provider_error(
            exc,
            "GitHub review pipeline",
        ) from exc

    except Exception as exc:
        logger.exception("GitHub review pipeline failed")

        raise HTTPException(
            status_code=502,
            detail="GitHub review pipeline failed. Check server logs.",
        ) from exc


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
    )
