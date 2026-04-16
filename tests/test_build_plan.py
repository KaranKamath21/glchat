"""Offline tests for the read-only payload builder.

Stubs the GitLab resolvers so we can validate payload assembly (the heart of
the approval gate) without any network or LLM calls. Run from the project root:

    ./venv/bin/pytest -q
"""

import pytest

import services.gitlab_service as svc
from services.gitlab_service import build_action_plan, build_issue_view, build_issue_list, build_issue_statistics
from agents.schema import GitLabAction


@pytest.fixture(autouse=True)
def stub_resolvers(monkeypatch):
    monkeypatch.setattr(svc.gl, "find_project", lambda name: {
        "id": 42, "path_with_namespace": "group/mobile-app",
        "name": "Mobile App", "web_url": "https://gl.example/group/mobile-app",
    })
    monkeypatch.setattr(svc.gl, "find_user", lambda name: {"id": 7, "username": name})
    monkeypatch.setattr(svc.gl, "find_label", lambda pid, name: {"id": 100, "name": name})
    monkeypatch.setattr(svc.gl, "find_milestone", lambda pid, title: {"id": 5, "title": title})


def test_create_issue_full():
    action = GitLabAction(
        action="create_issue", project="Mobile App", title="Fix broken login",
        description="Login fails on submit.",
        acceptance_criteria=["User can log in", "Failed attempts are logged"],
        assignees=["jdoe"], labels=["bug", "frontend"], milestone="v1.0",
        due_date="2026-07-24",
        time_estimate="3h30m", comments=["Reported by support"],
    )
    plan = build_action_plan(action)

    assert plan["endpoint"] == {"method": "POST", "path": "/projects/42/issues"}
    p = plan["payload"]
    assert p["title"] == "Fix broken login"
    assert "## Acceptance Criteria" in p["description"]
    assert "- [ ] User can log in" in p["description"]
    assert p["assignee_ids"] == [7]
    assert p["labels"] == ["bug", "frontend"]
    assert p["milestone_id"] == 5
    assert p["due_date"] == "2026-07-24"
    assert plan["extras"]["time_estimate"] == "3h30m"
    assert plan["extras"]["comments"] == ["Reported by support"]
    assert plan["target"]["project"]["id"] == 42


def test_update_issue_add_labels_and_state():
    action = GitLabAction(
        action="update_issue", project="Mobile App", issue_iid=12,
        assignees=["asmith"], add_labels=["needs-review"],
        state_event="close",
        comments=["please prioritize"],
    )
    plan = build_action_plan(action)
    assert plan["endpoint"] == {"method": "PUT", "path": "/projects/42/issues/12"}
    p = plan["payload"]
    assert p["assignee_ids"] == [7]
    assert p["add_labels"] == ["needs-review"]
    assert p["state_event"] == "close"
    assert plan["extras"]["comments"] == ["please prioritize"]


def test_add_comment():
    action = GitLabAction(
        action="add_comment", project="Mobile App", issue_iid=7,
        comments=["Verified on staging."],
    )
    plan = build_action_plan(action)
    assert plan["endpoint"] == {"method": "POST", "path": "/projects/42/issues/7/notes"}
    assert plan["extras"]["comments"] == ["Verified on staging."]
    assert plan["payload"] == {}


def test_fork_only_fields_are_not_supported():
    """story_points, priority and recurring tasks exist only on the fork, not stock GitLab."""
    a = GitLabAction(action="create_issue", title="X")
    assert not hasattr(a, "story_points")
    assert not hasattr(a, "priority")
    assert not hasattr(a, "recurring_task_id")
    # Extra kwargs are ignored by pydantic and must never reach the payload.
    plan = build_action_plan(GitLabAction(
        action="create_issue", project="Mobile App", title="X",
        story_points=5, priority=3, cron="0 9 * * 1"))
    for key in ("story_points", "weight", "priority", "cron"):
        assert key not in plan["payload"]


def test_recurring_task_action_is_rejected():
    with pytest.raises(ValueError):
        build_action_plan(GitLabAction(
            action="create_recurring_task", project="Mobile App", title="X"))


def test_link_issues():
    action = GitLabAction(
        action="link_issues", project="Mobile App", issue_iid=10,
        target_issue_iid=20, link_type="blocks",
    )
    plan = build_action_plan(action)
    assert plan["endpoint"] == {"method": "POST", "path": "/projects/42/issues/10/links"}
    p = plan["payload"]
    assert p["target_issue_iid"] == 20
    assert p["link_type"] == "blocks"
    assert p["target_project_id"] == 42  # defaults to source project


def test_create_child_item_resolves_parent_and_labels(monkeypatch):
    monkeypatch.setattr(svc.gl, "get_issue", lambda pid, iid: {"id": 9001, "iid": iid, "title": "parent"})
    action = GitLabAction(
        action="create_child_item", project="Mobile App", parent_issue_iid=5,
        title="Write unit tests", work_item_type="task",
        assignees=["jdoe"], labels=["testing"],
        acceptance_criteria=["Coverage > 80%"],
    )
    plan = build_action_plan(action)
    assert plan["endpoint"] == {"method": "POST", "path": "/projects/42/-/work_items"}
    p = plan["payload"]
    assert p["title"] == "Write unit tests"
    assert p["work_item_type"] == "task"
    assert p["parent_id"] == 9001          # parent iid → global work item id
    assert p["assignee_ids"] == [7]
    assert p["label_ids"] == [100]         # work items use label IDs
    assert "## Acceptance Criteria" in p["description"]


def test_work_item_body_nesting():
    from services.gitlab_service import _build_work_item_body
    body = _build_work_item_body({
        "title": "Child", "work_item_type": "task",
        "description": "do it", "assignee_ids": [7], "label_ids": [100], "parent_id": 9001,
    })
    assert body["work_item_type_name"] == "task"
    assert body["features"]["hierarchy"]["parent_id"] == 9001
    assert body["features"]["assignees"]["assignee_ids"] == [7]
    assert body["features"]["labels"]["label_ids"] == [100]
    assert body["features"]["description"]["description"] == "do it"


def test_missing_project_warns():
    action = GitLabAction(action="create_issue", title="No project")
    plan = build_action_plan(action)
    assert any("project" in w.lower() for w in plan["warnings"])


def test_build_issue_view(monkeypatch):
    monkeypatch.setattr(svc.gl, "get_issue", lambda pid, iid: {
        "iid": iid, "id": 5000, "title": "Broken login", "state": "opened",
        "description": "It breaks on submit.", "web_url": "https://gl/x/12",
        "labels": ["bug", "frontend"],
        "assignees": [{"username": "jdoe"}], "author": {"username": "asmith"},
        "milestone": {"title": "v1.0"}, "due_date": "2026-08-01",
        "time_stats": {"human_time_estimate": "3h"},
    })
    action = GitLabAction(action="get_issue", project="Mobile App", issue_iid=12)
    view = build_issue_view(action)
    i = view["issue"]
    assert i["iid"] == 12
    assert i["title"] == "Broken login"
    assert i["assignees"] == ["jdoe"]
    assert i["labels"] == ["bug", "frontend"]
    assert i["milestone"] == "v1.0"
    assert i["time_estimate"] == "3h"
    assert i["project_path"] == "group/mobile-app"
    assert "#12 Broken login" in view["summary"]
    assert view["warnings"] == []


def test_build_issue_view_missing_iid():
    view = build_issue_view(GitLabAction(action="get_issue", project="Mobile App"))
    assert view["issue"] is None
    assert view["warnings"]


def _capture_get(monkeypatch, return_value):
    calls = {}

    def fake_get(path, params=None):
        calls["path"] = path
        calls["params"] = params or {}
        return return_value

    monkeypatch.setattr(svc.gl, "get", fake_get)
    return calls


def test_list_issues_project_with_filters(monkeypatch):
    calls = _capture_get(monkeypatch, [
        {"iid": 1, "title": "A", "state": "opened", "web_url": "u1",
         "labels": ["bug"], "assignees": [{"username": "jdoe"}],
         "milestone": {"title": "v1"},
         "references": {"full": "group/mobile-app#1"}},
        {"iid": 2, "title": "B", "state": "opened", "web_url": "u2", "labels": [], "assignees": []},
    ])
    action = GitLabAction(action="list_issues", project="Mobile App",
                          state_filter="opened", labels=["bug"], assignees=["jdoe"], search="crash")
    res = build_issue_list(action)
    assert calls["path"] == "/projects/42/issues"
    assert calls["params"]["state"] == "opened"
    assert calls["params"]["labels"] == "bug"
    assert calls["params"]["assignee_username"] == ["jdoe"]
    assert calls["params"]["search"] == "crash"
    assert len(res["issues"]) == 2
    assert res["issues"][0]["iid"] == 1
    assert res["issues"][0]["assignees"] == ["jdoe"]


def test_list_issues_global_defaults_scope(monkeypatch):
    calls = _capture_get(monkeypatch, [])
    res = build_issue_list(GitLabAction(action="list_issues"))
    assert calls["path"] == "/issues"
    assert calls["params"]["scope"] == "assigned_to_me"   # default for global
    assert res["issues"] == []


def test_list_issues_group_scope(monkeypatch):
    monkeypatch.setattr(svc.gl, "find_group", lambda n: {"id": 88, "full_path": "group", "name": "Group"})
    calls = _capture_get(monkeypatch, [])
    build_issue_list(GitLabAction(action="list_issues", group="Group"))
    assert calls["path"] == "/groups/88/issues"


def test_issue_statistics(monkeypatch):
    calls = _capture_get(monkeypatch, {"statistics": {"counts": {"all": 8, "closed": 5, "opened": 3}}})
    res = build_issue_statistics(GitLabAction(action="issue_statistics", project="Mobile App", state_filter="all"))
    assert calls["path"] == "/projects/42/issues_statistics"
    assert res["statistics"] == {"all": 8, "closed": 5, "opened": 3}
    assert "open: 3" in res["summary"]
