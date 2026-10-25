import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

MAX_TOTAL_BYTES = 200_000
MAX_FILES = 30
TIMEOUT_SECONDS = 25


def validate_relative_path(raw_path: str) -> str:
    path = PurePosixPath(raw_path)
    if not raw_path or raw_path == "." or path.is_absolute() or ".." in path.parts or "\\" in raw_path:
        raise ValueError(f"Unsafe relative path: {raw_path!r}")
    if raw_path == ".git" or raw_path.startswith(".git/"):
        raise ValueError("Git metadata cannot be included.")
    return str(path)


def run_docker_validation(entry_file: str, files: dict[str, str], run_tests: bool = True) -> dict:
    entry = validate_relative_path(entry_file)
    if len(files) > MAX_FILES:
        raise ValueError(f"Too many files; limit is {MAX_FILES}.")
    normalized, total = {}, 0
    for name, content in files.items():
        safe = validate_relative_path(name)
        if not isinstance(content, str):
            raise ValueError(f"File content must be text: {safe}")
        if safe in normalized:
            raise ValueError(f"Duplicate normalized file path: {safe}")
        total += len(content.encode())
        normalized[safe] = content
    if entry not in normalized:
        raise ValueError("entry_file must be present in files.")
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"Workspace exceeds {MAX_TOTAL_BYTES} bytes.")
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker CLI was not found. Start Docker Desktop.")
    with tempfile.TemporaryDirectory(prefix="reviewpilot-validation-") as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        for name, content in normalized.items():
            dest = workspace / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        base = [docker, "run", "--rm", "--network", "none", "--memory", "256m", "--cpus", "1",
                "--pids-limit", "64", "--read-only", "--security-opt", "no-new-privileges",
                "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
                "-v", f"{workspace}:/workspace:ro", "-w", "/workspace", "node:22-alpine"]
        syntax = _run(base + ["node", "--check", entry])
        tests = None
        if run_tests and syntax["status"] == "passed":
            names = [n for n in normalized if n.endswith((".test.js", ".spec.js", ".test.cjs", ".spec.cjs", ".test.mjs", ".spec.mjs"))]
            tests = _run(base + ["node", "--test"]) if names else {
                "status": "not_run", "reason": "No recognized Node test files supplied.",
                "exit_code": None, "stdout": "", "stderr": ""}
        passed = syntax["status"] == "passed" and (tests is None or tests["status"] in ("passed", "not_run"))
        return {"status": "passed" if passed else "failed", "language": "javascript",
                "entry_file": entry, "syntax_check": syntax, "tests": tests, "applied": False,
                "workspace_destroyed_after_run": True,
                "note": "Passing checks do not prove correctness; tests may be incomplete."}


def _run(command: list[str]) -> dict:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        return {"status": "passed" if proc.returncode == 0 else "failed",
                "exit_code": proc.returncode, "stdout": proc.stdout[:8000], "stderr": proc.stderr[:8000]}
    except subprocess.TimeoutExpired as exc:
        def bounded(value):
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")[:8000]
            return str(value or "")[:8000]
        return {"status": "timed_out", "exit_code": None, "stdout": bounded(exc.stdout), "stderr": bounded(exc.stderr)}
