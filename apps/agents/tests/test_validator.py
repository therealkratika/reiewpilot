import pytest

from app.validator import run_docker_validation


def test_validation_rejects_duplicate_normalized_paths(monkeypatch):
    monkeypatch.setattr("app.validator.shutil.which", lambda _: "/usr/bin/docker")

    with pytest.raises(ValueError, match="Duplicate normalized file path"):
        run_docker_validation(
            "src/index.js",
            {
                "src/index.js": "console.log('safe');",
                "src//index.js": "console.log('overwritten');",
            },
            run_tests=False,
        )


def test_validation_rejects_current_directory_as_file():
    with pytest.raises(ValueError, match="Unsafe relative path"):
        run_docker_validation(".", {".": "not a file"}, run_tests=False)
