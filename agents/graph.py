"""Conversational agent built on LangGraph with PostgreSQL-backed memory.

A minimal ``MessagesState`` graph with a single agent node. The node binds the
``GitLabAction`` schema as a tool: if the model calls it, we run the read-only
resolvers and return an approval-gated proposal; otherwise the model's plain
reply is a normal chat turn. The compiled graph uses the Postgres checkpointer,
so every turn is contextual and history persists across restarts.
"""

import json
import logging
from datetime import date
from functools import lru_cache

from langchain_core.messages import (
    SystemMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
)
from langgraph.graph import StateGraph, START, END, MessagesState

from agents.llm import get_groq_model, get_bedrock_model
from agents.prompt import build_chatbot_prompt
from agents.schema import (
    GitLabAction,
    IssueLookup,
    READ_ACTIONS,
    ACTION_GET_ISSUE,
    ACTION_LIST_ISSUES,
    ACTION_ISSUE_STATISTICS,
)
from agents.memory import get_checkpointer, memory_lock
from services.gitlab_service import (
    gl,
    build_action_plan,
    build_issue_view,
    build_issue_list,
    build_issue_statistics,
    GitLabAPIError,
)

logger = logging.getLogger(__name__)

# Tools bound to the model. GitLabAction is a *proposal* signal (never executed here);
# IssueLookup is a read-only fetch that IS executed so the model can enhance real content.
_TOOLS = [GitLabAction, IssueLookup]
MAX_TOOL_STEPS = 4


def _text_of(message) -> str:
    """Extract plain text from an AIMessage whose content may be str or blocks."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return ""


def _invoke_llm(messages):
    """Invoke the tool-bound model, Groq primary with Bedrock fallback.

    If Bedrock is unavailable (e.g. no AWS creds), re-raise the *primary* Groq
    error, since that's the real cause worth surfacing — not the AWS token error.
    """
    try:
        return get_groq_model().bind_tools(_TOOLS).invoke(messages)
    except Exception as groq_err:
        logger.warning("[Chat] Primary model (Groq) failed: %s. Trying Bedrock.", groq_err)
        try:
            return get_bedrock_model().bind_tools(_TOOLS).invoke(messages)
        except Exception as bedrock_err:
            logger.warning("[Chat] Bedrock fallback also failed: %s", bedrock_err)
            raise groq_err


def _run_issue_lookup(args: dict) -> dict:
    """Execute the read-only IssueLookup tool: fetch an issue's current content."""
    project = args.get("project")
    iid = args.get("issue_iid")
    try:
        proj = gl.find_project(project) if project else None
        if not proj:
            return {"error": f"Project '{project}' not found"}
        issue = gl.get_issue(proj["id"], iid)
    except GitLabAPIError as e:
        return {"error": e.message}
    return {
        "project_id": proj["id"],
        "project_path": proj.get("path_with_namespace"),
        "iid": issue.get("iid"),
        "title": issue.get("title"),
        "description": (issue.get("description") or "")[:6000],
        "labels": issue.get("labels"),
        "state": issue.get("state"),
        "due_date": issue.get("due_date"),
        "assignees": [a.get("username") for a in (issue.get("assignees") or [])],
        "milestone": (issue.get("milestone") or {}).get("title"),
        "web_url": issue.get("web_url"),
    }


def _agent_node(state):
    """One conversation turn.

    Runs a bounded tool loop: the model may call IssueLookup (read-only, executed here)
    to fetch an existing issue's content, then reply or call GitLabAction to propose an
    (approval-gated) action. GitLabAction is never executed — only acknowledged.
    """
    today = date.today().isoformat()
    convo = [SystemMessage(content=build_chatbot_prompt(today))] + list(state["messages"])
    produced = []

    for _ in range(MAX_TOOL_STEPS):
        resp = _invoke_llm(convo)
        convo.append(resp)
        produced.append(resp)

        tool_calls = resp.tool_calls or []
        lookup_calls = [tc for tc in tool_calls if tc["name"] == IssueLookup.__name__]

        if lookup_calls:
            # Execute read-only lookups and feed results back so the model can enhance.
            for tc in tool_calls:
                if tc["name"] == IssueLookup.__name__:
                    content = json.dumps(_run_issue_lookup(tc.get("args") or {}))[:6000]
                else:
                    # A proposal call made prematurely alongside a lookup — acknowledge so
                    # history stays valid; the model should re-propose after the lookup.
                    content = "Fetch the issue first, then propose the update."
                tm = ToolMessage(content=content, tool_call_id=tc["id"])
                convo.append(tm)
                produced.append(tm)
            continue  # loop back to the model with the fetched content

        # No lookups: this is the final turn (chat reply or a GitLabAction call, which the
        # app handles — either showing read-only results or a proposal for approval).
        for tc in tool_calls:
            produced.append(ToolMessage(
                content="Handled by the app and shown to the user.",
                tool_call_id=tc["id"],
            ))
        break

    return {"messages": produced}


@lru_cache(maxsize=1)
def _graph():
    builder = StateGraph(MessagesState)
    builder.add_node("agent", _agent_node)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    return builder.compile(checkpointer=get_checkpointer())


def _default_reply(action: GitLabAction) -> str:
    verb = (action.action or "action").replace("_", " ")
    return (f"I've prepared this {verb}. Review the fields below and click "
            f"“Approve & Send to GitLab” to apply it, or edit anything first.")


def chat_turn(thread_id: str, text: str) -> dict:
    """Run one conversation turn. Returns {'reply': str, 'proposal': dict|None}."""
    config = {"configurable": {"thread_id": thread_id}}
    with memory_lock():
        result = _graph().invoke({"messages": [HumanMessage(content=text)]}, config)

    ai = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage)), None)
    if ai is None:
        return {"reply": "…", "proposal": None}

    reply = _text_of(ai)
    result = {"reply": reply or "…", "proposal": None, "issue": None, "issues": None, "statistics": None}

    action_call = next(
        (tc for tc in (ai.tool_calls or []) if tc["name"] == GitLabAction.__name__), None
    )
    if not action_call:
        return result

    try:
        action = GitLabAction(**(action_call.get("args") or {}))
    except Exception as e:
        logger.error("[Chat] Invalid action args: %s", e)
        result["reply"] = reply or "I couldn't understand that request."
        return result

    if action.action in READ_ACTIONS:
        # Read-only: run immediately, no approval gate.
        read = _run_read_action(action)
        result.update(read)
        result["reply"] = (reply + "\n\n" if reply else "") + read.get("summary", "")
    else:
        try:
            result["proposal"] = build_action_plan(action)
            result["reply"] = reply or _default_reply(action)
        except GitLabAPIError as e:
            result["reply"] = (reply + "\n\n" if reply else "") + f"I couldn't resolve references: {e.message}"
        except Exception as e:
            logger.error("[Chat] Failed to build action from tool call: %s", e)
            result["reply"] = (reply + "\n\n" if reply else "") + f"I couldn't build that action: {e}"

    return result


def _run_read_action(action: GitLabAction) -> dict:
    """Run a read-only action and return the UI keys (issue / issues / statistics) + summary."""
    if action.action == ACTION_GET_ISSUE:
        view = build_issue_view(action)
        return {"issue": view.get("issue"), "summary": view["summary"]}
    if action.action == ACTION_LIST_ISSUES:
        res = build_issue_list(action)
        return {"issues": res.get("issues"), "summary": res["summary"]}
    if action.action == ACTION_ISSUE_STATISTICS:
        res = build_issue_statistics(action)
        return {"statistics": res.get("statistics"), "summary": res["summary"]}
    return {"summary": ""}


def get_history(thread_id: str) -> list:
    """Return the persisted conversation as [{'role','text'}] for the UI."""
    config = {"configurable": {"thread_id": thread_id}}
    with memory_lock():
        snapshot = _graph().get_state(config)

    values = getattr(snapshot, "values", None) or {}
    out = []
    for m in values.get("messages", []):
        if isinstance(m, HumanMessage):
            role = "user"
        elif isinstance(m, AIMessage):
            role = "assistant"
        else:
            continue  # skip System / Tool messages
        text = _text_of(m)
        if not text and isinstance(m, AIMessage) and m.tool_calls:
            text = "(prepared a GitLab action for approval)"
        if text:
            out.append({"role": role, "text": text})
    return out
