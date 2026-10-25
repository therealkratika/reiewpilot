import re
from collections import defaultdict

HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines_by_file(diff: str) -> dict[str, set[int]]:
    result: dict[str, set[int]] = defaultdict(set)
    current_file = None
    new_line = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current_file = path[2:] if path.startswith("b/") else path
            if current_file == "/dev/null":
                current_file = None
            new_line = None
            continue
        if line.startswith("--- "):
            continue
        match = HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if current_file is None or new_line is None or line.startswith("\\"):
            continue
        if line.startswith("+"):
            result[current_file].add(new_line)
            new_line += 1
        elif line.startswith("-"):
            continue
        else:
            new_line += 1
    return dict(result)


def validate_finding_locations(findings: list, diff: str) -> tuple[list, list[str]]:
    changed = added_lines_by_file(diff)
    accepted, warnings = [], []
    for finding in findings:
        path = finding.file_path.removeprefix("a/").removeprefix("b/")
        allowed = changed.get(path, set())
        if finding.line_start > finding.line_end or finding.line_start not in allowed or finding.line_end not in allowed:
            warnings.append(f"Rejected finding '{finding.title}': location is not a valid added-line range.")
            continue
        if finding.line_end - finding.line_start > 20:
            warnings.append(f"Rejected finding '{finding.title}': range exceeds 21 lines.")
            continue
        accepted.append(finding)
    return accepted, warnings
