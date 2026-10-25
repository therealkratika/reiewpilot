import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .diff_parser import validate_finding_locations
from .fixer import make_unified_diff, propose_fix
from .github_client import GitHubAPIError
from .github_service import publish_fix, pull_request_snapshot, publish_review
from .pipeline import run_github_review_pipeline
from .reviewer import analyze_diff
from .schemas import FixInput, GitHubFixPublishInput, GitHubPipelineInput, GitHubPublishInput, ReviewFixValidateInput, ReviewInput, ValidationInput, WorkflowInput
from .validator import run_docker_validation
from .workflow import run_review_and_fix, run_review_fix_validate
from .webhook import (
    WebhookEventStore,
    event_key,
    initiate_review,
    parse_pull_request_event,
    verify_signature,
)

repo_env = Path(__file__).resolve().parents[3] / ".env"
if repo_env.is_file():
    load_dotenv(repo_env)
else:
    load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reviewpilot")
app = FastAPI(title="ReviewPilot Agent Service", version="0.8.0")
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
webhook_events = WebhookEventStore()


@app.get("/health")
def health():
    return {"status": "ok", "service": "reviewpilot-agents", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/github/webhooks")
async def github_webhook(request: Request):
    body = await request.body()
    if not os.getenv("GITHUB_WEBHOOK_SECRET"):
        raise HTTPException(status_code=503, detail="GITHUB_WEBHOOK_SECRET is not configured.")
    if not verify_signature(
        body,
        request.headers.get("x-hub-signature-256"),
        os.getenv("GITHUB_WEBHOOK_SECRET"),
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    if request.headers.get("x-github-event") != "pull_request":
        return JSONResponse({"status": "ignored", "reason": "Unsupported GitHub event."}, status_code=202)
    try:
        event = parse_pull_request_event(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if event["action"] not in {"opened", "reopened", "synchronize"}:
        return JSONResponse({"status": "ignored", "reason": "Pull request action is not review-triggering."}, status_code=202)

    key = event_key(event)
    if not webhook_events.claim(key):
        return JSONResponse({"status": "duplicate", "key": list(key)}, status_code=202)
    try:
        result = initiate_review(event)
    except Exception as exc:
        webhook_events.release(key)
        logger.exception("Webhook review initiation failed")
        raise HTTPException(status_code=502, detail="Webhook review initiation failed.") from exc
    return JSONResponse(
        {
            "status": "accepted",
            "repository": f"{event['owner']}/{event['repo']}",
            "pull_number": event["pull_number"],
            "head_sha": event["head_sha"],
            "action": event["action"],
            "review": result,
            "publishing": False,
        },
        status_code=202,
    )


def provider_error(exc: Exception, component: str):
    if isinstance(exc, RuntimeError) and ("API_KEY is not configured" in str(exc) or "GITHUB_TOKEN is not configured" in str(exc)):
        return HTTPException(status_code=503, detail=str(exc))
    logger.exception("%s failed", component)
    return HTTPException(status_code=502, detail=f"{component} failed. Check server logs.")


@app.post("/v1/reviews/analyze")
def analyze_review(payload: ReviewInput):
    try:
        analysis = analyze_diff(payload.diff)
    except Exception as exc:
        raise provider_error(exc, "Reviewer") from exc
    accepted, warnings = validate_finding_locations(analysis.findings, payload.diff)
    return JSONResponse(content={"repository": payload.repository, "pull_request_number": payload.pull_request_number,
        "head_sha": payload.head_sha, "model": os.getenv("OPENAI_MODEL", "gpt-5.5"),
        "summary": analysis.summary, "findings": [f.model_dump() for f in accepted],
        "rejected_findings": len(analysis.findings)-len(accepted), "validation_warnings": warnings})


@app.post("/v1/fixes/propose")
def create_fix_proposal(payload: FixInput):
    try:
        proposal = propose_fix(payload)
    except Exception as exc:
        raise provider_error(exc, "Fixer") from exc
    patch = make_unified_diff(payload.file_path, payload.original_code, proposal.updated_code)
    return JSONResponse(content={"status": "proposal_ready" if patch else "no_changes_proposed",
        "file_path": payload.file_path, "explanation": proposal.explanation, "patch": patch,
        "tests_to_add": proposal.tests_to_add, "assumptions": proposal.assumptions,
        "applied": False, "validated": False})


@app.post("/v1/workflows/review-and-fix")
def review_and_fix(payload: WorkflowInput):
    try:
        return JSONResponse(content=run_review_and_fix(payload))
    except Exception as exc:
        raise provider_error(exc, "Review-and-fix workflow") from exc


@app.post("/v1/workflows/review-fix-validate")
def review_fix_validate(payload: ReviewFixValidateInput):
    try:
        return JSONResponse(content=run_review_fix_validate(payload))
    except Exception as exc:
        raise provider_error(exc, "Review-fix-validate workflow") from exc


@app.post("/v1/validation/run")
def validate_code(payload: ValidationInput):
    try:
        result = run_docker_validation(payload.entry_file, payload.files, payload.run_tests)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Validation worker failed")
        raise HTTPException(status_code=500, detail="Validation worker failed. Check server logs.") from exc
    return JSONResponse(content=result, status_code=200 if result["status"] == "passed" else 422)


@app.get("/v1/github/pulls/{owner}/{repo}/{pull_number}")
def get_github_pull(owner: str, repo: str, pull_number: int):
    if pull_number < 1:
        raise HTTPException(status_code=400, detail="pull_number must be positive.")
    try:
        return JSONResponse(content=pull_request_snapshot(owner, repo, pull_number))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GitHubAPIError as exc:
        status = exc.status_code if exc.status_code in (400, 401, 403, 404, 429) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("GitHub PR lookup failed")
        raise HTTPException(status_code=502, detail="GitHub PR lookup failed. Check server logs.") from exc


@app.post("/v1/github/reviews/publish")
def publish_github_review(payload: GitHubPublishInput):
    try:
        result = publish_review(payload)
        code = 409 if result["status"] == "stale_head_sha" else 422 if result["status"] == "rejected_invalid_locations" else 200
        return JSONResponse(content=result, status_code=code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GitHubAPIError as exc:
        status = exc.status_code if exc.status_code in (400, 401, 403, 404, 409, 422, 429) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("GitHub review publish failed")
        raise HTTPException(status_code=502, detail="GitHub review publish failed. Check server logs.") from exc


@app.post("/v1/github/fixes/publish")
def publish_github_fix(payload: GitHubFixPublishInput):
    try:
        result = publish_fix(payload)
        code = {"stale_head_sha": 409, "closed_pr": 409, "invalid_file_path": 400, "approval_required": 400}.get(result["status"], 200)
        return JSONResponse(content=result, status_code=code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GitHubAPIError as exc:
        status = exc.status_code if exc.status_code in (400, 401, 403, 404, 409, 422, 429) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("GitHub fix publish failed")
        raise HTTPException(status_code=502, detail="GitHub fix publish failed. Check server logs.") from exc


@app.post("/v1/github/pulls/{owner}/{repo}/{pull_number}/review-pipeline")
def github_review_pipeline(owner: str, repo: str, pull_number: int, payload: GitHubPipelineInput):
    if pull_number < 1:
        raise HTTPException(status_code=400, detail="pull_number must be positive.")
    try:
        result = run_github_review_pipeline(owner, repo, pull_number, payload)
        return JSONResponse(content=result)
    except GitHubAPIError as exc:
        status = exc.status_code if exc.status_code in (400, 401, 403, 404, 429) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise provider_error(exc, "GitHub review pipeline") from exc
    except Exception as exc:
        logger.exception("GitHub review pipeline failed")
        raise HTTPException(status_code=502, detail="GitHub review pipeline failed. Check server logs.") from exc
