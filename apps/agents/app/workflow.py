from .diff_parser import validate_finding_locations
from .fixer import make_unified_diff, propose_fix
from .reviewer import analyze_diff
from .schemas import FixInput, ReviewFixValidateInput, WorkflowInput
from .validator import run_docker_validation, validate_relative_path


def validate_patch_scope(file_path: str, patch: str) -> tuple[bool, str]:
    if not patch:
        return False, "The proposed fix does not contain any changes."
    paths = {
        line[4:].strip()[2:]
        for line in patch.splitlines()
        if line.startswith(("--- ", "+++ ")) and line[4:].strip().startswith(("a/", "b/"))
    }
    if paths != {file_path}:
        return False, "The proposal changes files outside the requested target."
    return True, "The proposal is limited to the requested target file."


def _review(payload: WorkflowInput):
    analysis = analyze_diff(payload.diff)
    accepted, warnings = validate_finding_locations(analysis.findings, payload.diff)
    target = validate_relative_path(payload.file_path.removeprefix("a/").removeprefix("b/"))
    target_findings = []
    for finding in accepted:
        finding_path = finding.file_path.removeprefix("a/").removeprefix("b/")
        try:
            matches_target = validate_relative_path(finding_path) == target
        except ValueError:
            matches_target = False
        if matches_target:
            target_findings.append(finding)
    return analysis, accepted, target_findings[: payload.max_findings_to_fix], warnings


def run_review_and_fix(payload: WorkflowInput) -> dict:
    analysis, accepted, target_findings, warnings = _review(payload)
    if not target_findings:
        return {
            "status": "review_completed",
            "review": {
                "summary": analysis.summary,
                "findings": [finding.model_dump() for finding in accepted],
                "rejected_findings": len(analysis.findings) - len(accepted),
                "validation_warnings": warnings,
            },
            "fix": {"status": "not_attempted", "reason": "No accepted findings require a fix."},
        }

    proposal = propose_fix(
        FixInput(
            repository=payload.repository,
            pull_request_number=payload.pull_request_number,
            head_sha=payload.head_sha,
            file_path=payload.file_path,
            original_code=payload.original_code,
            findings=target_findings,
        )
    )
    patch = make_unified_diff(payload.file_path, payload.original_code, proposal.updated_code)
    valid, message = validate_patch_scope(payload.file_path, patch)
    return {
        "status": "proposal_ready" if valid else "fix_proposal_rejected",
        "review": {
            "summary": analysis.summary,
            "findings": [finding.model_dump() for finding in accepted],
            "rejected_findings": len(analysis.findings) - len(accepted),
            "validation_warnings": warnings,
        },
        "fix": {
            "status": "proposal_ready" if valid else "rejected_by_structural_validation",
            "file_path": payload.file_path,
            "explanation": proposal.explanation,
            "updated_code": proposal.updated_code,
            "patch": patch,
            "tests_to_add": proposal.tests_to_add,
            "assumptions": proposal.assumptions,
            "structural_validation": {"passed": valid, "message": message},
            "applied": False,
            "requires_human_review": True,
        },
    }


def run_review_fix_validate(payload: ReviewFixValidateInput) -> dict:
    result = run_review_and_fix(payload)
    if result["status"] != "proposal_ready":
        result["validation"] = {"status": "not_attempted", "applied": False}
        return result

    files = {payload.file_path: result["fix"]["updated_code"]}
    files.update(payload.test_files)
    validation = run_docker_validation(
        payload.file_path,
        files,
        run_tests=payload.run_tests,
    )
    result["validation"] = {**validation, "applied": False}
    result["status"] = (
        "fix_validated" if validation["status"] == "passed" else "fix_validation_failed"
    )
    return result
