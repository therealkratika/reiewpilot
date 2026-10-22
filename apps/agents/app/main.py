
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import ReviewDatabase
from .auth import (
    AuthenticatedUser,
    authorize_repository,
    complete_github_login,
    enforce_anonymous_rate_limit,
    get_current_user,
    start_github_login,
)
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

if configured_web_origin:
    allowed_origins = [configured_web_origin]
elif os.getenv("APP_ENV", "development").lower() == "production":
    allowed_origins = []
else:
    allowed_origins = ["http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)


# Health check
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "reviewpilot-agents",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/auth/login")
def github_login(request: Request):
    enforce_anonymous_rate_limit(request, "oauth-login")
    return start_github_login()


@app.get("/v1/auth/callback")
def github_oauth_callback(request: Request, code: str | None = None, state: str | None = None):
    enforce_anonymous_rate_limit(request, "oauth-callback")
    if request.query_params.get("error"):
        raise HTTPException(status_code=401, detail="GitHub authorization was not completed.")
    return complete_github_login(request, code, state)


@app.get("/v1/auth/me")
def auth_me(user: AuthenticatedUser = Depends(get_current_user)):
    return {"authenticated": True, "user": user.public_profile()}


@app.post("/v1/auth/logout")
def auth_logout(
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    revoked = ReviewDatabase().revoke_user_session(
        user.session_hash,
        datetime.now(timezone.utc),
    )
    if not revoked:
        raise HTTPException(status_code=401, detail="Session is invalid or expired.")
    secure = os.getenv("SESSION_COOKIE_SECURE", "true").strip().lower() not in {
        "0", "false", "no",
    }
    same_site = os.getenv("SESSION_COOKIE_SAMESITE", "none" if secure else "lax").lower()
    response.delete_cookie(
        "reviewpilot_session",
        path="/",
        secure=secure,
        httponly=True,
        samesite=same_site,
    )
    response.delete_cookie(
        "reviewpilot_csrf",
        path="/",
        secure=secure,
        httponly=False,
        samesite=same_site,
    )
    logger.info("User logged out user_id=%s", user.id)
    return {"authenticated": False}


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
def get_webhook_job(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    job = ReviewDatabase().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Webhook job was not found.")
    authorize_repository(user, job["owner"], job["repo"])
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
def analyze_review(
    payload: ReviewInput,
    user: AuthenticatedUser = Depends(get_current_user),
):
    repository_parts = payload.repository.split("/")
    if len(repository_parts) != 2:
        raise HTTPException(status_code=400, detail="repository must use the owner/repository format.")
    authorize_repository(user, *repository_parts)
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
def create_fix_proposal(
    payload: FixInput,
    user: AuthenticatedUser = Depends(get_current_user),
):
    repository_parts = payload.repository.split("/")
    if len(repository_parts) != 2:
        raise HTTPException(status_code=400, detail="repository must use the owner/repository format.")
    authorize_repository(user, *repository_parts)
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
def review_and_fix(
    payload: WorkflowInput,
    user: AuthenticatedUser = Depends(get_current_user),
):
    repository_parts = payload.repository.split("/")
    if len(repository_parts) != 2:
        raise HTTPException(status_code=400, detail="repository must use the owner/repository format.")
    authorize_repository(user, *repository_parts)
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
def review_fix_validate(
    payload: ReviewFixValidateInput,
    user: AuthenticatedUser = Depends(get_current_user),
):
    repository_parts = payload.repository.split("/")
    if len(repository_parts) != 2:
        raise HTTPException(status_code=400, detail="repository must use the owner/repository format.")
    authorize_repository(user, *repository_parts)
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
def validate_code(
    payload: ValidationInput,
    _user: AuthenticatedUser = Depends(get_current_user),
):
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
    user: AuthenticatedUser = Depends(get_current_user),
):
    if pull_number < 1:
        raise HTTPException(
            status_code=400,
            detail="pull_number must be positive.",
        )

    try:
        client = authorize_repository(user, owner, repo)
        return JSONResponse(
            content=pull_request_snapshot(
                owner,
                repo,
                pull_number,
                client=client,
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

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception("GitHub PR lookup failed")

        raise HTTPException(
            status_code=502,
            detail="GitHub PR lookup failed. Check server logs.",
        ) from exc


# Publish GitHub review
@app.post("/v1/github/reviews/publish")
def publish_github_review(
    payload: GitHubPublishInput,
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        client = authorize_repository(user, payload.owner, payload.repo, write=True)
        database = ReviewDatabase()
        database.record_audit_event(
            user_id=user.id,
            action="review.publish",
            owner=payload.owner,
            repo=payload.repo,
            pull_number=payload.pull_number,
            resource_id=payload.expected_head_sha,
            outcome="requested",
            created_at=datetime.now(timezone.utc),
        )
        result = publish_review(payload, client=client)
        database.update_review_publication(
            payload.owner,
            payload.repo,
            payload.pull_number,
            payload.expected_head_sha,
            result,
            user_id=user.id,
        )
        logger.info(
            "Review publication requested user_id=%s repository=%s/%s pull=%s status=%s",
            user.id,
            payload.owner,
            payload.repo,
            payload.pull_number,
            result.get("status"),
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

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception("GitHub review publish failed")

        raise HTTPException(
            status_code=502,
            detail="GitHub review publish failed. Check server logs.",
        ) from exc


# Publish GitHub fix
@app.post("/v1/github/fixes/publish")
def publish_github_fix(
    payload: GitHubFixPublishInput,
    user: AuthenticatedUser = Depends(get_current_user),
):
    database = ReviewDatabase()
    approval = database.get_fix_approval(payload.approval_id, user.id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Fix approval was not found.")
    if approval["consumed_at"] is not None:
        raise HTTPException(status_code=409, detail="Fix approval has already been used.")
    expires_at = datetime.fromisoformat(approval["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="Fix approval has expired.")
    if approval["head_sha"] != approval["proposal_head_sha"]:
        raise HTTPException(status_code=409, detail="Fix approval is stale.")

    try:
        client = authorize_repository(
            user,
            approval["owner"],
            approval["repo"],
            write=True,
        )
        current_pr = client.get_pull_request(
            approval["owner"],
            approval["repo"],
            approval["pull_number"],
        )
        current_sha = (current_pr.get("head") or {}).get("sha", "")
        if current_pr.get("state") != "open" or current_sha != approval["head_sha"]:
            raise HTTPException(status_code=409, detail="The PR changed after this fix was approved.")

        def verify_and_consume_approval(service_sha: str) -> None:
            if service_sha != approval["head_sha"]:
                raise HTTPException(status_code=409, detail="The PR changed after this fix was approved.")
            latest = client.get_pull_request(
                approval["owner"],
                approval["repo"],
                approval["pull_number"],
            )
            latest_sha = (latest.get("head") or {}).get("sha", "")
            if latest.get("state") != "open" or latest_sha != approval["head_sha"]:
                raise HTTPException(status_code=409, detail="The PR changed after this fix was approved.")
            if not database.consume_fix_approval(
                payload.approval_id,
                user.id,
                datetime.now(timezone.utc),
            ):
                raise HTTPException(status_code=409, detail="Fix approval is no longer valid.")

        if not payload.dry_run and approval["head_sha"] != current_sha:
            raise HTTPException(status_code=409, detail="The PR changed after this fix was approved.")
        database.record_audit_event(
            user_id=user.id,
            action="fix.publish",
            owner=approval["owner"],
            repo=approval["repo"],
            pull_number=approval["pull_number"],
            resource_id=str(payload.approval_id),
            outcome="requested" if not payload.dry_run else "dry_run",
            created_at=datetime.now(timezone.utc),
        )
        result = publish_fix(
            owner=approval["owner"],
            repo=approval["repo"],
            pull_number=approval["pull_number"],
            expected_head_sha=approval["head_sha"],
            file_path=approval["file_path"],
            updated_code=approval["updated_code"],
            commit_message=f"Apply ReviewPilot fix: {approval['file_path']}",
            dry_run=payload.dry_run,
            client=client,
            before_publish=verify_and_consume_approval if not payload.dry_run else None,
        )
        database.update_fix_publication(
            approval["owner"],
            approval["repo"],
            approval["pull_number"],
            approval["head_sha"],
            approval["file_path"].removeprefix("a/").removeprefix("b/"),
            result,
        )
        logger.info(
            "Fix publication requested user_id=%s approval_id=%s repository=%s/%s pull=%s status=%s",
            user.id,
            payload.approval_id,
            approval["owner"],
            approval["repo"],
            approval["pull_number"],
            result.get("status"),
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

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception("GitHub fix publish failed")

        raise HTTPException(
            status_code=502,
            detail="GitHub fix publish failed. Check server logs.",
        ) from exc


@app.post("/v1/github/fixes/{fix_proposal_id}/approve")
def approve_fix_proposal(
    fix_proposal_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
):
    database = ReviewDatabase()
    proposal = database.get_fix_proposal(fix_proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Fix proposal was not found.")
    try:
        client = authorize_repository(
            user,
            proposal["owner"],
            proposal["repo"],
            write=True,
        )
        pull_request = client.get_pull_request(
            proposal["owner"],
            proposal["repo"],
            proposal["pull_number"],
        )
    except GitHubAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code if exc.status_code in (401, 403, 404, 429) else 502,
            detail="Could not verify repository write access or pull request state.",
        ) from exc
    head_sha = (pull_request.get("head") or {}).get("sha", "")
    if (
        pull_request.get("state") != "open"
        or not head_sha
        or proposal["head_sha"] != head_sha
    ):
        raise HTTPException(status_code=409, detail="The PR head changed; generate a new fix proposal.")
    now = datetime.now(timezone.utc)
    database.record_audit_event(
        user_id=user.id,
        action="fix.approve",
        owner=proposal["owner"],
        repo=proposal["repo"],
        pull_number=proposal["pull_number"],
        resource_id=str(fix_proposal_id),
        outcome="approved",
        created_at=now,
    )
    approval_id = database.create_fix_approval(
        fix_proposal_id=fix_proposal_id,
        user_id=user.id,
        repository_id=proposal["repository_id"],
        pull_number=proposal["pull_number"],
        head_sha=head_sha,
        approved_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    logger.info(
        "Fix proposal approved user_id=%s proposal_id=%s repository=%s/%s pull=%s",
        user.id,
        fix_proposal_id,
        proposal["owner"],
        proposal["repo"],
        proposal["pull_number"],
    )
    return {
        "approval_id": approval_id,
        "fix_proposal_id": fix_proposal_id,
        "repository": f"{proposal['owner']}/{proposal['repo']}",
        "pull_number": proposal["pull_number"],
        "head_sha": head_sha,
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
    }


@app.get("/v1/reviews/history")
def get_review_history(user: AuthenticatedUser = Depends(get_current_user)):
    return {"reviews": ReviewDatabase().list_user_review_history(user.id)}


# GitHub pull request review pipeline
@app.post(
    "/v1/github/pulls/{owner}/{repo}/{pull_number}/review-pipeline"
)
def github_review_pipeline(
    owner: str,
    repo: str,
    pull_number: int,
    payload: GitHubPipelineInput,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if pull_number < 1:
        raise HTTPException(
            status_code=400,
            detail="pull_number must be positive.",
        )

    try:
        client = authorize_repository(user, owner, repo)
        result = run_github_review_pipeline(
            owner,
            repo,
            pull_number,
            payload,
            client=client,
        )
        database = ReviewDatabase()
        review_id = database.save_review_result(
            {
                "owner": owner,
                "repo": repo,
                "pull_number": pull_number,
                "head_sha": (result.get("pull_request") or {}).get("head_sha", ""),
                "action": "manual",
            },
            result,
            user_id=user.id,
        )
        if review_id is not None and isinstance(result.get("fix"), dict):
            proposal_id = database.get_fix_proposal_id(review_id)
            if proposal_id is not None:
                result["fix"]["proposal_id"] = proposal_id
        logger.info(
            "Review pipeline completed user_id=%s repository=%s/%s pull=%s",
            user.id,
            owner,
            repo,
            pull_number,
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

    except HTTPException:
        raise

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
