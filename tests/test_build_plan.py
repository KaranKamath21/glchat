"""Offline tests for the read-only payload builder.

Stubs the GitLab resolvers so we can validate payload assembly (the heart of
the approval gate) without any network or LLM calls. Run from the project root:

    ./venv/bin/pytest -q
"""

import pytest

import services.gitlab_service as svc
from services.gitlab_service import build_action_plan
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


def test_missing_project_warns():
    action = GitLabAction(action="create_issue", title="No project")
    plan = build_action_plan(action)
    assert any("project" in w.lower() for w in plan["warnings"])
