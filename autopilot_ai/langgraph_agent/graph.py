"""LangGraph ReAct StateGraph definition."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from .state import AgentState

MAX_ITERATIONS = 10

SYSTEM_PROMPT = """You are AutoPilot AI, a local-first Windows computer-use agent.
Use the available tools to gather fresh observations and complete the user's goal.
Use only paths and facts returned by tools or explicitly present in the request.
Call at most one tool per assistant turn; observe its result before selecting the next tool.
For requests to list or show directory entries, call list_directory. Use search_files only when the user gives a filename, filename fragment, or file extension.
For local files, folders, Downloads, or workspace requests, never use web search. Use the approved local filesystem tools and use the exact returned file path for any follow-up read.
Never claim an operation succeeded unless its tool result says status=success and its independent verification passed.
If a tool reports that approval is required, stop and explain what needs approval; do not claim it was performed.
Do not repeat a tool call with the same arguments after an operation has been attempted.
Never use run_command for general-purpose shell execution; it is restricted to safe inspection commands.
Do not perform destructive actions. Stay within approved paths and follow tool validation.
When finished, call finish with a concise summary grounded in tool observations."""


def build_graph(
    workspace: str,
    approved: bool,
    model_name: str = "qwen2.5:0.5b",
    approved_action: dict[str, Any] | None = None,
    goal: str = "",
):
    """Build and compile an Ollama-backed ReAct graph with bounded iterations."""
    from langchain_ollama import ChatOllama

    from .tools_lc import make_tools

    tools = make_tools(workspace, approved, approved_action, goal)
    tool_node = ToolNode(tools, handle_tool_errors=True)
    llm = ChatOllama(model=model_name, temperature=0).bind_tools(tools)

    def call_model(state: AgentState) -> dict[str, Any]:
        response = llm.invoke([SystemMessage(content=SYSTEM_PROMPT), *state["messages"]])
        calls = getattr(response, "tool_calls", []) or []
        if len(calls) > 1:
            # Keep tool execution sequential so observations are available before
            # the next operation and side effects cannot race each other.
            selected = next((call for call in calls if call.get("name") != "finish"), calls[0])
            response = response.model_copy(update={"tool_calls": [selected]})
        updates: dict[str, Any] = {
            "messages": [response],
            "iterations": state["iterations"] + 1,
        }
        calls = getattr(response, "tool_calls", []) or []
        if len(calls) == 1 and calls[0].get("name") == "finish":
            answer = calls[0].get("args", {}).get("answer")
            if isinstance(answer, str) and answer.strip():
                updates["final_answer"] = answer.strip()
        return updates

    def route(state: AgentState) -> str:
        if state.get("final_answer"):
            return END
        if state["iterations"] >= MAX_ITERATIONS:
            return END
        last = state["messages"][-1] if state["messages"] else None
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    def route_after_tool(state: AgentState) -> str:
        """Pause immediately when an operation is waiting for user approval."""
        last = state["messages"][-1] if state["messages"] else None
        if isinstance(last, ToolMessage) and isinstance(last.content, str):
            try:
                if json.loads(last.content).get("requires_approval") is True:
                    return END
            except (json.JSONDecodeError, AttributeError):
                pass
        return "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", tool_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", route_after_tool, {"agent": "agent", END: END})
    return graph.compile()
