"""Structured representation of a GitLab action parsed from natural language.

The LLM populates a single flat ``GitLabAction`` (via
``with_structured_output``). Human-friendly names (project name, usernames,
label names, milestone name) are captured here as-is; the service layer
resolves them into the IDs the API needs when assembling the payload.
"""

from typing import List, Optional
from pydantic import BaseModel, Field

# Action identifiers understood by the agent / executor.
ACTION_CREATE_ISSUE = "create_issue"
ACTION_UPDATE_ISSUE = "update_issue"
ACTION_ADD_COMMENT = "add_comment"
ACTION_LINK_ISSUES = "link_issues"
ACTION_CREATE_CHILD_ITEM = "create_child_item"
ACTION_GET_ISSUE = "get_issue"

ACTIONS = [
    ACTION_CREATE_ISSUE,
    ACTION_UPDATE_ISSUE,
    ACTION_ADD_COMMENT,
    ACTION_LINK_ISSUES,
    ACTION_CREATE_CHILD_ITEM,
    ACTION_GET_ISSUE,
]

# Read-only actions execute immediately (no approval gate).
READ_ACTIONS = {ACTION_GET_ISSUE}


class GitLabAction(BaseModel):
    """Propose a GitLab issue or comment action for the user to approve.

    Call this tool ONLY when the user is asking to CREATE or EDIT an issue, ADD a
    comment, LINK two issues, CREATE a child item (sub-task) under a parent issue,
    or VIEW/SHOW an existing issue (get_issue). Extract the fields from the request
    and the conversation so far. This tool does NOT execute anything — it merely
    prepares a payload that the user reviews and approves in the UI. For greetings,
    questions about capabilities, or any non-actionable message, do NOT call this
    tool; reply in plain text instead.
    """

    action: str = Field(
        description=(
            "The type of action. One of: 'create_issue', 'update_issue', "
            "'add_comment', 'link_issues' (relate/block two issues), "
            "'create_child_item' (create a sub-item under a parent issue), "
            "'get_issue' (view/show an existing issue's details)."
        )
    )

    # ── Targeting ────────────────────────────────────────────────────────
    project: Optional[str] = Field(
        default=None,
        description="Human-friendly project name or path (e.g. 'Mobile App' or 'group/mobile-app').",
    )
    issue_iid: Optional[int] = Field(
        default=None,
        description="The issue's internal ID (IID) shown as #123. Required for update_issue and add_comment.",
    )

    # ── Issue content ─────────────────────────────────────────────────────
    title: Optional[str] = Field(default=None, description="Issue title.")
    description: Optional[str] = Field(
        default=None,
        description="Issue body / description. Do NOT put acceptance criteria here; use the acceptance_criteria field.",
    )
    acceptance_criteria: Optional[List[str]] = Field(
        default=None,
        description="List of acceptance-criteria bullet points, if the user mentions any.",
    )
    assignees: Optional[List[str]] = Field(
        default=None,
        description="Usernames or full names of people to assign (e.g. ['jdoe', 'Jane Smith']).",
    )
    labels: Optional[List[str]] = Field(
        default=None, description="Label names to apply (e.g. ['bug', 'frontend'])."
    )
    add_labels: Optional[List[str]] = Field(
        default=None,
        description="For update_issue only: label names to ADD without removing existing labels.",
    )
    remove_labels: Optional[List[str]] = Field(
        default=None,
        description="For update_issue only: label names to REMOVE.",
    )
    due_date: Optional[str] = Field(
        default=None, description="Due date in strict YYYY-MM-DD format."
    )
    milestone: Optional[str] = Field(default=None, description="Milestone name/title.")
    time_estimate: Optional[str] = Field(
        default=None,
        description="Time-tracking estimate in GitLab duration format, e.g. '3h30m', '2d', '45m'.",
    )
    confidential: Optional[bool] = Field(
        default=None, description="Whether the issue should be confidential."
    )
    state_event: Optional[str] = Field(
        default=None,
        description="For update_issue: 'close' to close or 'reopen' to reopen the issue.",
    )
    comments: Optional[List[str]] = Field(
        default=None,
        description="One or more comment/note bodies to post (initial comments on create, or the note for add_comment).",
    )

    # ── Linked items (link_issues) ────────────────────────────────────────
    target_project: Optional[str] = Field(
        default=None,
        description="For link_issues: the target issue's project (name/path). Defaults to the source project if omitted.",
    )
    target_issue_iid: Optional[int] = Field(
        default=None, description="For link_issues: the #IID of the issue to link to. `issue_iid` is the source issue."
    )
    link_type: Optional[str] = Field(
        default=None,
        description="For link_issues: relationship type — 'relates_to' (default), 'blocks', or 'is_blocked_by'.",
    )

    # ── Child items (create_child_item) ───────────────────────────────────
    parent_issue_iid: Optional[int] = Field(
        default=None,
        description="For create_child_item: the #IID of the parent issue the new child item belongs under.",
    )
    work_item_type: Optional[str] = Field(
        default=None,
        description="For create_child_item: the child's work-item type, e.g. 'task' (default) or 'issue'.",
    )
