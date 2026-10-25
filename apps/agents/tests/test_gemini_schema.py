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
