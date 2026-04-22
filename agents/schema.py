"""Structured representation of a GitLab action parsed from natural language.

The LLM populates a single flat ``GitLabAction`` (via
``with_structured_output``). Human-friendly names (project name, usernames,
label names, milestone name) are captured here as-is; the service layer
resolves them into the IDs the API needs when assembling the payload.
"""

from typing import List, Optional, Union
from pydantic import BaseModel, Field, field_validator

# Action identifiers understood by the agent / executor.
ACTION_CREATE_ISSUE = "create_issue"
ACTION_UPDATE_ISSUE = "update_issue"
ACTION_ADD_COMMENT = "add_comment"
ACTION_LINK_ISSUES = "link_issues"
ACTION_CREATE_CHILD_ITEM = "create_child_item"
ACTION_GET_ISSUE = "get_issue"
ACTION_LIST_ISSUES = "list_issues"
ACTION_ISSUE_STATISTICS = "issue_statistics"

ACTIONS = [
    ACTION_CREATE_ISSUE,
    ACTION_UPDATE_ISSUE,
    ACTION_ADD_COMMENT,
    ACTION_LINK_ISSUES,
    ACTION_CREATE_CHILD_ITEM,
    ACTION_GET_ISSUE,
    ACTION_LIST_ISSUES,
    ACTION_ISSUE_STATISTICS,
]

# Read-only actions execute immediately (no approval gate).
READ_ACTIONS = {ACTION_GET_ISSUE, ACTION_LIST_ISSUES, ACTION_ISSUE_STATISTICS}


# Groq validates tool arguments strictly against the JSON schema, and LLMs
# sometimes emit numeric/boolean fields as strings (e.g. "12" for issue_iid),
# which Groq then rejects with a 400. We accept int|str|bool in the schema and
# coerce in a "before" validator so the stored value is always the right type.
IntLike = Optional[Union[int, str]]
BoolLike = Optional[Union[bool, str]]


def _to_int_or_none(v):
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return v


def _to_bool_or_none(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y"):
            return True
        if s in ("false", "0", "no", "n"):
            return False
    return None


class IssueLookup(BaseModel):
    """Fetch an existing issue's CURRENT title, description, labels, and fields (read-only).

    Call this before enhancing or editing an existing issue, so you can improve its real
    current content (e.g. rewrite the description, expand acceptance criteria) instead of
    guessing. Do NOT call it for brand-new issues. After you receive the content, call
    GitLabAction (update_issue) with the improved fields.
    """

    project: str = Field(description="Project name or path the issue belongs to.")
    issue_iid: Union[int, str] = Field(description="The issue's internal ID (the #number).")

    @field_validator("issue_iid", mode="before")
    @classmethod
    def _coerce_iid(cls, v):
        return _to_int_or_none(v)


class GitLabAction(BaseModel):
    """Propose a GitLab issue or comment action for the user to approve.

    Call this tool ONLY when the user is asking to CREATE or EDIT an issue, ADD a
    comment, LINK two issues, CREATE a child item (sub-task) under a parent issue,
    or VIEW/SHOW an existing issue (get_issue).
    Extract the fields from the request
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
            "'get_issue' (view/show an existing issue's details), 'list_issues' "
            "(search/list issues with filters), 'issue_statistics' (counts of "
            "open/closed/all)."
        )
    )

    # ── Targeting ────────────────────────────────────────────────────────
    project: Optional[str] = Field(
        default=None,
        description="Human-friendly project name or path (e.g. 'Mobile App' or 'group/mobile-app').",
    )
    issue_iid: IntLike = Field(
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
    confidential: BoolLike = Field(
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
    target_issue_iid: IntLike = Field(
        default=None, description="For link_issues: the #IID of the issue to link to. `issue_iid` is the source issue."
    )
    link_type: Optional[str] = Field(
        default=None,
        description="For link_issues: relationship type — 'relates_to' (default), 'blocks', or 'is_blocked_by'.",
    )

    # ── Child items (create_child_item) ───────────────────────────────────
    parent_issue_iid: IntLike = Field(
        default=None,
        description="For create_child_item: the #IID of the parent issue the new child item belongs under.",
    )
    work_item_type: Optional[str] = Field(
        default=None,
        description="For create_child_item: the child's work-item type, e.g. 'task' (default) or 'issue'.",
    )

    # ── Listing / searching (list_issues, issue_statistics) ───────────────
    group: Optional[str] = Field(
        default=None,
        description="For list_issues/issue_statistics: a group name/path to scope the search to (instead of a project).",
    )
    scope: Optional[str] = Field(
        default=None,
        description="For list_issues with no project/group: 'assigned_to_me', 'created_by_me', or 'all'.",
    )
    state_filter: Optional[str] = Field(
        default=None,
        description="For list_issues/issue_statistics: filter by state — 'opened', 'closed', or 'all'.",
    )
    search: Optional[str] = Field(
        default=None, description="For list_issues: free-text to search in issue titles/descriptions."
    )
    author: Optional[str] = Field(
        default=None, description="For list_issues: filter to issues authored by this username."
    )
    max_results: IntLike = Field(
        default=None, description="For list_issues: maximum number of issues to return."
    )

    @field_validator(
        "issue_iid", "target_issue_iid",
        "parent_issue_iid", "max_results", mode="before",
    )
    @classmethod
    def _coerce_int_fields(cls, v):
        return _to_int_or_none(v)

    @field_validator("confidential", mode="before")
    @classmethod
    def _coerce_bool_fields(cls, v):
        return _to_bool_or_none(v)
