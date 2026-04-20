"""Offline integration tests for the conversational graph.

Swaps the Postgres checkpointer for an in-memory one and stubs the LLM + GitLab
resolvers, so we exercise the real LangGraph flow (chat vs. tool-call proposal)
and memory persistence without a database, network, or API keys.
"""

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import agents.graph as graph
import agents.memory as memory
import services.gitlab_service as svc


class FakeLLM:
    """Returns preset AIMessages in order; .bind_tools is a no-op."""
    def __init__(self, responses):
        self.responses = responses
        self.i = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        r = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return r


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    saver = InMemorySaver()
    monkeypatch.setattr(memory, "get_checkpointer", lambda: saver)
    monkeypatch.setattr(graph, "get_checkpointer", lambda: saver)  # graph imported the name
    graph._graph.cache_clear()
    monkeypatch.setattr(svc.gl, "find_project", lambda n: {
        "id": 42, "path_with_namespace": "g/mobile", "name": "Mobile App", "web_url": "http://x"})
    monkeypatch.setattr(svc.gl, "find_user", lambda n: {"id": 7, "username": n})
    monkeypatch.setattr(svc.gl, "find_label", lambda p, n: {"id": 100, "name": n})
    monkeypatch.setattr(svc.gl, "find_milestone", lambda p, t: {"id": 5, "title": t})
    monkeypatch.setattr(svc.gl, "get_issue", lambda pid, iid: {
        "iid": iid, "title": "login broken", "description": "it breaks",
        "labels": ["bug"], "state": "opened", "assignees": [], "web_url": "http://x/12"})
    yield
    graph._graph.cache_clear()


def test_conversational_reply(monkeypatch):
    monkeypatch.setattr(graph, "get_groq_model",
                        lambda: FakeLLM([AIMessage(content="I can create and edit issues.")]))
    out = graph.chat_turn("t-chat", "what can you do?")
    assert out["proposal"] is None
    assert "create" in out["reply"].lower()


def test_action_request_yields_proposal(monkeypatch):
    ai = AIMessage(content="", tool_calls=[{
        "name": "GitLabAction", "id": "call_1",
        "args": {"action": "create_issue", "project": "Mobile App",
                 "title": "Fix login", "assignees": ["jdoe"],
                 "labels": ["bug"]},
    }])
    monkeypatch.setattr(graph, "get_groq_model", lambda: FakeLLM([ai]))
    out = graph.chat_turn("t-prop", "create an issue in Mobile App titled Fix login ...")
    p = out["proposal"]
    assert p is not None
    assert p["endpoint"] == {"method": "POST", "path": "/projects/42/issues"}
    assert p["payload"]["title"] == "Fix login"
    assert p["payload"]["assignee_ids"] == [7]
    assert p["payload"]["labels"] == ["bug"]
    assert out["reply"]  # a friendly "review & approve" line


def test_enhance_existing_issue_fetches_then_proposes(monkeypatch):
    lookup = AIMessage(content="", tool_calls=[{
        "name": "IssueLookup", "id": "l1",
        "args": {"project": "Mobile App", "issue_iid": 12}}])
    propose = AIMessage(content="Here's an improved version.", tool_calls=[{
        "name": "GitLabAction", "id": "g1",
        "args": {"action": "update_issue", "project": "Mobile App", "issue_iid": 12,
                 "title": "Fix login redirect loop",
                 "description": "Users are bounced back to the login page.",
                 "acceptance_criteria": ["User logs in successfully", "No redirect loop"]}}])
    fake = FakeLLM([lookup, propose])  # one instance so responses advance across calls
    monkeypatch.setattr(graph, "get_groq_model", lambda: fake)
    out = graph.chat_turn("t-enh", "enhance issue #12 in Mobile App")
    p = out["proposal"]
    assert p is not None
    assert p["endpoint"] == {"method": "PUT", "path": "/projects/42/issues/12"}
    assert p["payload"]["title"] == "Fix login redirect loop"
    assert "## Acceptance Criteria" in p["payload"]["description"]
    assert "No redirect loop" in p["payload"]["description"]


def test_get_issue_is_read_only_no_proposal(monkeypatch):
    ai = AIMessage(content="Here it is:", tool_calls=[{
        "name": "GitLabAction", "id": "gi1",
        "args": {"action": "get_issue", "project": "Mobile App", "issue_iid": 12}}])
    monkeypatch.setattr(graph, "get_groq_model", lambda: FakeLLM([ai]))
    out = graph.chat_turn("t-view", "show me issue #12 in Mobile App")
    assert out["proposal"] is None          # read-only: nothing to approve
    assert out["issue"] is not None
    assert out["issue"]["iid"] == 12
    assert out["issue"]["title"] == "login broken"


def test_list_issues_is_read_only(monkeypatch):
    monkeypatch.setattr(svc.gl, "get", lambda path, params=None: [
        {"iid": 1, "title": "A", "state": "opened", "web_url": "u1", "labels": [], "assignees": []},
        {"iid": 2, "title": "B", "state": "closed", "web_url": "u2", "labels": [], "assignees": []},
    ])
    ai = AIMessage(content="", tool_calls=[{
        "name": "GitLabAction", "id": "l1",
        "args": {"action": "list_issues", "project": "Mobile App", "state_filter": "opened"}}])
    monkeypatch.setattr(graph, "get_groq_model", lambda: FakeLLM([ai]))
    out = graph.chat_turn("t-list", "list open issues in Mobile App")
    assert out["proposal"] is None
    assert out["issue"] is None
    assert out["issues"] and len(out["issues"]) == 2
    assert "Found 2 issue" in out["reply"]


def test_memory_persists_across_turns(monkeypatch):
    monkeypatch.setattr(graph, "get_groq_model", lambda: FakeLLM([
        AIMessage(content="hi there"),
        AIMessage(content="earlier you said hello"),
    ]))
    graph.chat_turn("t-mem", "hello")
    graph.chat_turn("t-mem", "what did I say first?")
    hist = graph.get_history("t-mem")
    assert [m["role"] for m in hist] == ["user", "assistant", "user", "assistant"]
    assert hist[0]["text"] == "hello"
    assert hist[2]["text"] == "what did I say first?"
