"""Typed contracts used at the boundary between planning and execution."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=300)
    tool_name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any]
    expected_result: str = Field(min_length=1, max_length=300)
    risk_level: Literal["low", "medium", "high"] = "low"
    requires_approval: bool = False


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "error", "denied"]
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    summary: str
    evidence: dict[str, Any] = Field(default_factory=dict)
