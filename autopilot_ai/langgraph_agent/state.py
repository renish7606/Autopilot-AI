"""Typed state carried between LangGraph ReAct iterations."""

from __future__ import annotations

import operator
from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    goal: str
    workspace: str
    approved: bool
    iterations: int
    final_answer: str | None
    tool_calls_log: list[dict[str, Any]]
