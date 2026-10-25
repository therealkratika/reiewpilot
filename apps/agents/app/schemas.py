from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class GitHubPublishComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    line: int = Field(ge=1)
    body: str = Field(min_length=3, max_length=5000)


class GitHubPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)
    pull_number: int = Field(gt=0)
    expected_head_sha: str = Field(min_length=7, max_length=64)
    summary: str = Field(min_length=1, max_length=5000)
    event: Literal["COMMENT", "REQUEST_CHANGES", "APPROVE"] = "COMMENT"
    dry_run: bool = True
    comments: list[GitHubPublishComment] = Field(default_factory=list, max_length=30)


class GitHubFixPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)
    pull_number: int = Field(gt=0)
    expected_head_sha: str = Field(min_length=7, max_length=64)
    file_path: str = Field(min_length=1, max_length=500)
    updated_code: str = Field(min_length=1, max_length=60_000)
    commit_message: str = Field(default="Apply ReviewPilot fix proposal", min_length=1, max_length=200)
    dry_run: bool = True
    approved: bool = False


class GitHubPipelineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    propose_fix: bool = False
    validate_fix: bool = False
    target_file: str | None = Field(default=None, min_length=1, max_length=500)
    test_files: dict[str, str] = Field(default_factory=dict, max_length=20)
    max_findings: int = Field(default=10, ge=1, le=30)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(min_length=1, max_length=200)
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=7, max_length=64)
    diff: str = Field(min_length=1, max_length=100_000)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["bug", "security", "performance", "reliability", "maintainability"]
    severity: Literal["critical", "high", "medium", "low"]
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=10, max_length=2000)
    evidence: str = Field(min_length=3, max_length=1000)
    suggested_fix: str = Field(min_length=3, max_length=2000)
    file_path: str = Field(min_length=1, max_length=500)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)


class ReviewAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=2000)
    findings: list[Finding] = Field(max_length=30)


class FixInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(min_length=1, max_length=200)
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=7, max_length=64)
    file_path: str = Field(min_length=1, max_length=500)
    original_code: str = Field(min_length=1, max_length=50_000)
    findings: list[Finding] = Field(min_length=1, max_length=10)


class FixProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(min_length=5, max_length=2000)
    updated_code: str = Field(min_length=1, max_length=60_000)
    tests_to_add: list[str] = Field(max_length=10)
    assumptions: list[str] = Field(max_length=10)


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(min_length=1, max_length=200)
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=7, max_length=64)
    diff: str = Field(min_length=1, max_length=100_000)
    file_path: str = Field(min_length=1, max_length=500)
    original_code: str = Field(min_length=1, max_length=50_000)
    max_findings_to_fix: int = Field(default=3, ge=1, le=10)


class ValidationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["javascript"] = "javascript"
    entry_file: str = Field(min_length=1, max_length=200)
    files: dict[str, str] = Field(min_length=1, max_length=30)
    run_tests: bool = True


class ReviewFixValidateInput(WorkflowInput):
    test_files: dict[str, str] = Field(default_factory=dict, max_length=20)
    run_tests: bool = True
