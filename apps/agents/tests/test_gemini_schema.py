import pytest

from app.reviewer import analyze_diff
from app.schemas import ReviewAnalysis, gemini_response_schema


def _assert_no_additional_properties(value):
    if isinstance(value, dict):
        assert "additionalProperties" not in value
        assert "additional_properties" not in value
        for nested_value in value.values():
            _assert_no_additional_properties(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            _assert_no_additional_properties(nested_value)


def test_gemini_response_schema_removes_nested_unsupported_fields():
    schema = gemini_response_schema(ReviewAnalysis)

    _assert_no_additional_properties(schema)
    assert schema["properties"]["findings"]["items"]["$ref"] == "#/$defs/Finding"
    assert "$defs" in schema


def test_reviewer_sends_converted_schema_to_gemini(monkeypatch):
    captured = {}

    class FakeResponse:
        parsed = ReviewAnalysis(summary="No issues.", findings=[])
        text = None

    class FakeModels:
        def generate_content(self, **kwargs):
            captured["config"] = kwargs["config"]
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.reviewer.genai.Client", lambda api_key: FakeClient())

    result = analyze_diff("diff --git a/a.js b/a.js\n+++ b/a.js\n+const value = 1;\n")

    assert result == ReviewAnalysis(summary="No issues.", findings=[])
    schema = captured["config"].response_schema
    _assert_no_additional_properties(schema)
    assert schema["properties"]["findings"]["items"]["$ref"] == "#/$defs/Finding"
    assert "properties" in schema["$defs"]["Finding"]


def test_reviewer_retries_temporary_gemini_failures(monkeypatch):
    import app.gemini as gemini

    class FakeAPIError(Exception):
        def __init__(self, code):
            super().__init__(f"status {code}")
            self.code = code

    class FakeResponse:
        parsed = ReviewAnalysis(summary="Recovered.", findings=[])
        text = None

    attempts = []
    delays = []

    class FakeModels:
        def generate_content(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) < 3:
                raise FakeAPIError(503)
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(gemini, "APIError", FakeAPIError)
    monkeypatch.setattr(gemini.time, "sleep", delays.append)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.reviewer.genai.Client", lambda api_key: FakeClient())

    result = analyze_diff("diff --git a/a.js b/a.js\n+++ b/a.js\n+const value = 1;\n")

    assert result.summary == "Recovered."
    assert len(attempts) == 3
    assert delays == [0.5, 1.0]


def test_reviewer_does_not_retry_non_retryable_gemini_failure(monkeypatch):
    import app.gemini as gemini

    class FakeAPIError(Exception):
        def __init__(self, code):
            super().__init__(f"status {code}")
            self.code = code

    class FakeModels:
        def generate_content(self, **kwargs):
            raise FakeAPIError(400)

    class FakeClient:
        models = FakeModels()

    attempts = []
    monkeypatch.setattr(gemini, "APIError", FakeAPIError)
    monkeypatch.setattr(gemini.time, "sleep", lambda delay: attempts.append(delay))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.reviewer.genai.Client", lambda api_key: FakeClient())

    with pytest.raises(FakeAPIError):
        analyze_diff("diff --git a/a.js b/a.js\n+++ b/a.js\n+const value = 1;\n")

    assert attempts == []


@pytest.mark.parametrize("status_code", [408, 429])
def test_reviewer_retries_transient_gemini_rate_limits(monkeypatch, status_code):
    import app.gemini as gemini

    class FakeAPIError(Exception):
        def __init__(self, code):
            super().__init__(f"status {code}")
            self.code = code

    class FakeResponse:
        parsed = ReviewAnalysis(summary="Recovered.", findings=[])
        text = None

    attempts = []

    class FakeModels:
        def generate_content(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise FakeAPIError(status_code)
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(gemini, "APIError", FakeAPIError)
    monkeypatch.setattr(gemini.time, "sleep", lambda _: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.reviewer.genai.Client", lambda api_key: FakeClient())

    result = analyze_diff("diff --git a/a.js b/a.js\n+++ b/a.js\n+const value = 1;\n")

    assert result.summary == "Recovered."
    assert len(attempts) == 2


def test_reviewer_rejects_empty_model_response(monkeypatch):
    class FakeResponse:
        parsed = None
        text = ""

    class FakeModels:
        def generate_content(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.reviewer.genai.Client", lambda api_key: FakeClient())

    with pytest.raises(RuntimeError, match="did not return a structured review"):
        analyze_diff("diff --git a/a.js b/a.js\n+++ b/a.js\n+const value = 1;\n")


def test_reviewer_rejects_malformed_structured_response(monkeypatch):
    class FakeResponse:
        parsed = None
        text = "{ malformed"

    class FakeModels:
        def generate_content(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.reviewer.genai.Client", lambda api_key: FakeClient())

    with pytest.raises(ValueError):
        analyze_diff("diff --git a/a.js b/a.js\n+++ b/a.js\n+const value = 1;\n")
