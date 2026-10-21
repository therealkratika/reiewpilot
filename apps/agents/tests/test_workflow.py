import pytest

from app.workflow import validate_patch_scope


def test_patch_scope_accepts_single_requested_file():
    patch = (
        "--- a/src/target.py\n"
        "+++ b/src/target.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    valid, message = validate_patch_scope("src/target.py", patch)

    assert valid is True
    assert "limited to the requested target" in message


@pytest.mark.parametrize(
    ("target", "patch"),
    [
        ("src/target.py", ""),
        (
            "src/target.py",
            "--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-old\n+new\n",
        ),
        (
            "src/target.py",
            "--- a/src/target.py\n"
            "+++ b/src/target.py\n"
            "--- a/src/extra.py\n"
            "+++ b/src/extra.py\n",
        ),
        (
            "../target.py",
            "--- a/target.py\n+++ b/target.py\n@@ -1 +1 @@\n-old\n+new\n",
        ),
    ],
)
def test_patch_scope_rejects_empty_unsafe_or_unrelated_changes(target, patch):
    valid, _ = validate_patch_scope(target, patch)

    assert valid is False
