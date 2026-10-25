from .diff_parser import validate_finding_locations
from .github_service import pull_request_snapshot
from .reviewer import analyze_diff
from .schemas import FixInput, GitHubPipelineInput
from .fixer import make_unified_diff, propose_fix
from .validator import run_docker_validation
from .workflow import validate_patch_scope


def run_github_review_pipeline(owner: str, repo: str, pull_number: int,
                               options: GitHubPipelineInput, client=None,
                               auto_select_target: bool = False) -> dict:
    snapshot = pull_request_snapshot(owner, repo, pull_number, client=client)
    if snapshot["state"] != "open":
        return {"status": "rejected", "reason": "Only open pull requests can be reviewed.",
                "pull_request": snapshot, "published": False}

    analysis = analyze_diff(snapshot["diff"])
    accepted, warnings = validate_finding_locations(analysis.findings, snapshot["diff"])
    accepted = accepted[:options.max_findings]
    result = {
        "status": "review_completed",
        "pull_request": {k: snapshot[k] for k in ("repository", "pull_number", "title", "state", "draft",
                                                  "html_url", "head_sha", "head_ref", "changed_files")},
        "review": {"summary": analysis.summary, "findings": [f.model_dump() for f in accepted],
                   "rejected_findings": len(analysis.findings) - len(accepted),
                   "validation_warnings": warnings},
        "fix": None,
        "validation": {"status": "not_attempted", "reason": "Fix proposal/validation was not requested.",
                       "applied": False},
        "published": False,
        "next_action": "Inspect the report. Publishing is a separate explicit API action.",
    }

    if not options.propose_fix:
        return result
    target = options.target_file.removeprefix("a/").removeprefix("b/") if options.target_file else None
    if auto_select_target and not target:
        target = next(
            (
                file_meta.get("filename")
                for file_meta in snapshot["changed_files"]
                if any(
                    finding.file_path.removeprefix("a/").removeprefix("b/")
                    == file_meta.get("filename")
                    for finding in accepted
                )
            ),
            None,
        )
    if not target:
        result["fix"] = {"status": "not_attempted", "reason": "Set target_file to a changed file path."}
        return result

    file_meta = next((f for f in snapshot["changed_files"] if f.get("filename") == target), None)
    if file_meta is None:
        result["fix"] = {"status": "rejected", "reason": "target_file is not listed among this PR's changed files."}
        return result
    relevant = [f for f in accepted if f.file_path.removeprefix("a/").removeprefix("b/") == target]
    if not relevant:
        result["fix"] = {"status": "not_attempted", "reason": "No accepted findings target this file."}
        return result

    try:
        if client is None:
            from .github_client import GitHubClient
            client = GitHubClient()
        original_code = client.get_file_content(owner, repo, target, snapshot["head_sha"])
    except Exception as exc:
        result["fix"] = {"status": "unavailable", "reason": f"Could not fetch target file at PR head: {exc}"}
        return result

    proposal = propose_fix(FixInput(repository=f"{owner}/{repo}", pull_request_number=pull_number,
        head_sha=snapshot["head_sha"], file_path=target, original_code=original_code, findings=relevant))
    patch = make_unified_diff(target, original_code, proposal.updated_code)
    valid, message = validate_patch_scope(target, patch)
    result["fix"] = {"status": "proposal_ready" if valid else "rejected_by_structural_validation",
        "file_path": target, "explanation": proposal.explanation, "updated_code": proposal.updated_code, "patch": patch,
        "tests_to_add": proposal.tests_to_add, "assumptions": proposal.assumptions,
        "structural_validation": {"passed": valid, "message": message},
        "applied": False, "requires_human_review": True}
    if not valid:
        result["status"] = "fix_proposal_rejected"
        return result
    if not options.validate_fix:
        result["validation"] = {"status": "not_attempted", "reason": "Set validate_fix=true to run isolated validation.",
                                "applied": False}
        return result

    files = {target: proposal.updated_code}
    files.update(options.test_files)
    try:
        validation = run_docker_validation(target, files, run_tests=True)
        result["validation"] = {**validation, "applied": False}
    except (ValueError, RuntimeError) as exc:
        result["validation"] = {"status": "unavailable", "reason": str(exc), "applied": False}
    result["status"] = ("fix_validated" if result["validation"]["status"] == "passed"
        else "fix_validation_failed" if result["validation"]["status"] == "failed"
        else "fix_validation_unavailable")
    return result
