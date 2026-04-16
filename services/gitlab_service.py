"""GitLab REST API service.

Reuses the authentication / request pattern from the reference script
(``gitlab_audit.py``): a ``requests.Session`` with the ``PRIVATE-TOKEN`` header,
a retry adapter, and pagination helpers.

This module has a strict split:
  * READ-ONLY helpers (resolvers, ``get_issue``, ``build_action_plan``) may run
    automatically to assemble a payload.
  * MUTATING helpers (``create_issue``, ``update_issue``, ``add_comment``,
    ``set_time_estimate``, issue links, work items) and ``execute_plan`` are
    only ever called from the approval-gated /api/execute endpoint.
"""

import logging
from urllib.parse import quote

import requests

from core.config import (
    GITLAB_BASE_URL,
    GITLAB_API_PREFIX,
    GITLAB_TOKEN,
    GITLAB_VERIFY_SSL,
    HTTP_TIMEOUT,
    MAX_SEARCH_RESULTS,
)
from agents.schema import (
    GitLabAction,
    ACTION_CREATE_ISSUE,
    ACTION_UPDATE_ISSUE,
    ACTION_ADD_COMMENT,
    ACTION_LINK_ISSUES,
    ACTION_CREATE_CHILD_ITEM,
)

logger = logging.getLogger(__name__)


class GitLabAPIError(Exception):
    """Raised when the GitLab API returns an error response."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"GitLab API error {status}: {message}")


# ─────────────────────────────────────────────
# GITLAB CLIENT
# ─────────────────────────────────────────────
class GitLabClient:
    def __init__(self, base_url: str, token: str, api_prefix: str = "/api/v4", verify_ssl: bool = True):
        self.base = base_url.rstrip("/") + api_prefix
        self.web_base = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}

        self.session = requests.Session()
        self.session.headers.update(self.headers)
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.adapters.Retry(
                total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504]
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ── low-level request handling ──────────────────────────────────────
    def _error_message(self, resp: requests.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return resp.text[:300] or resp.reason
        if isinstance(data, dict):
            msg = data.get("message") or data.get("error")
            if msg:
                return msg if isinstance(msg, str) else str(msg)
        return str(data)[:300]

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None):
        url = f"{self.base}{path}"
        resp = self.session.request(
            method, url, params=params, json=json_body,
            timeout=HTTP_TIMEOUT, verify=self.verify_ssl,
        )
        if not resp.ok:
            raise GitLabAPIError(resp.status_code, self._error_message(resp))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path: str, params: dict = None):
        return self._request("GET", path, params=params)

    def get_all(self, path: str, params: dict = None) -> list:
        params = {**(params or {}), "per_page": MAX_SEARCH_RESULTS, "page": 1}
        return self.get(path, params) or []

    def post(self, path: str, json_body: dict = None):
        return self._request("POST", path, json_body=json_body)

    def put(self, path: str, json_body: dict = None):
        return self._request("PUT", path, json_body=json_body)

    def version(self) -> dict:
        return self.get("/version")

    # ── READ-ONLY resolvers ─────────────────────────────────────────────
    def find_project(self, name_or_path: str) -> dict | None:
        """Resolve a project by path (preferred) or fuzzy name search."""
        if not name_or_path:
            return None
        # Direct path lookup first, e.g. "group/subgroup/project".
        if "/" in name_or_path:
            try:
                return self.get(f"/projects/{quote(name_or_path, safe='')}")
            except GitLabAPIError as e:
                if e.status not in (404,):
                    raise
        results = self.get_all("/projects", {"search": name_or_path, "membership": True})
        if not results:
            return None
        lowered = name_or_path.lower()
        for p in results:
            if p.get("path_with_namespace", "").lower() == lowered or p.get("name", "").lower() == lowered:
                return p
        return results[0]

    def find_group(self, name_or_path: str) -> dict | None:
        """Resolve a group by path/id (preferred) or fuzzy name search."""
        if not name_or_path:
            return None
        if "/" in name_or_path or str(name_or_path).isdigit():
            try:
                return self.get(f"/groups/{quote(str(name_or_path), safe='')}")
            except GitLabAPIError as e:
                if e.status != 404:
                    raise
        results = self.get_all("/groups", {"search": name_or_path})
        lowered = name_or_path.lower()
        for g in results:
            if g.get("full_path", "").lower() == lowered or g.get("name", "").lower() == lowered:
                return g
        return results[0] if results else None

    def find_user(self, username_or_name: str) -> dict | None:
        if not username_or_name:
            return None
        exact = self.get("/users", {"username": username_or_name})
        if exact:
            return exact[0]
        results = self.get("/users", {"search": username_or_name})
        return results[0] if results else None

    def find_label(self, project_id, name: str) -> dict | None:
        if not name:
            return None
        results = self.get_all(f"/projects/{project_id}/labels", {"search": name})
        lowered = name.lower()
        for lb in results:
            if lb.get("name", "").lower() == lowered:
                return lb
        return results[0] if results else None

    def find_milestone(self, project_id, title: str) -> dict | None:
        if not title:
            return None
        results = self.get_all(f"/projects/{project_id}/milestones", {"search": title})
        lowered = title.lower()
        for m in results:
            if m.get("title", "").lower() == lowered:
                return m
        return results[0] if results else None

    def get_issue(self, project_id, iid: int) -> dict:
        return self.get(f"/projects/{project_id}/issues/{iid}")

    # ── MUTATING operations (approval-gated) ─────────────────────────────
    def create_issue(self, project_id, payload: dict) -> dict:
        return self.post(f"/projects/{project_id}/issues", clean(payload))

    def update_issue(self, project_id, iid: int, payload: dict) -> dict:
        return self.put(f"/projects/{project_id}/issues/{iid}", clean(payload))

    def set_time_estimate(self, project_id, iid: int, duration: str) -> dict:
        return self.post(f"/projects/{project_id}/issues/{iid}/time_estimate", {"duration": duration})

    def add_comment(self, project_id, iid: int, body: str) -> dict:
        return self.post(f"/projects/{project_id}/issues/{iid}/notes", {"body": body})

    def create_issue_link(self, project_id, iid: int, payload: dict) -> dict:
        return self.post(f"/projects/{project_id}/issues/{iid}/links", clean(payload))

    def create_work_item(self, project_id, body: dict) -> dict:
        # Experimental Work Items API; note the '/-/' segment.
        return self.post(f"/projects/{project_id}/-/work_items", body)


def clean(payload: dict) -> dict:
    """Drop keys whose value is None so we never send unintended nulls."""
    return {k: v for k, v in (payload or {}).items() if v is not None}


# Module-level singleton, mirroring the cicero convention.
gl = GitLabClient(
    base_url=GITLAB_BASE_URL,
    token=GITLAB_TOKEN,
    api_prefix=GITLAB_API_PREFIX,
    verify_ssl=GITLAB_VERIFY_SSL,
)


# ─────────────────────────────────────────────
# PAYLOAD BUILDING (read-only) & EXECUTION (mutating)
# ─────────────────────────────────────────────
def _compose_description(description, acceptance_criteria) -> str | None:
    """Fold an acceptance-criteria checklist into the issue description."""
    parts = []
    if description:
        parts.append(description.strip())
    if acceptance_criteria:
        lines = "\n".join(f"- [ ] {c}" for c in acceptance_criteria)
        parts.append(f"## Acceptance Criteria\n\n{lines}")
    return "\n\n".join(parts) if parts else None


def _resolve_assignees(project, names, notes, warnings) -> list:
    ids = []
    for name in names or []:
        user = gl.find_user(name)
        if user:
            ids.append(user["id"])
            notes.append(f"Assignee '{name}' → @{user['username']} (id {user['id']})")
        else:
            warnings.append(f"User '{name}' not found — skipped")
    return ids


def _resolve_label_ids(project_id, names, notes, warnings) -> list:
    ids = []
    for name in names or []:
        label = gl.find_label(project_id, name)
        if label:
            ids.append(label["id"])
            notes.append(f"Label '{name}' → id {label['id']}")
        else:
            warnings.append(f"Label '{name}' not found in project — skipped")
    return ids


def _resolve_label_ids(project_id, names, notes, warnings) -> list:
    ids = []
    for name in names or []:
        label = gl.find_label(project_id, name)
        if label:
            ids.append(label["id"])
            notes.append(f"Label '{name}' → id {label['id']}")
        else:
            warnings.append(f"Label '{name}' not found in project — skipped")
    return ids


def build_action_plan(action: GitLabAction) -> dict:
    """Assemble a complete, editable action plan from a parsed GitLabAction.

    READ-ONLY. Runs reference lookups (project/user/label/milestone) and returns
    a plan dict for front-end review. Never mutates GitLab.
    """
    notes: list = []
    warnings: list = []

    target = {"project": None, "issue_iid": action.issue_iid}
    project = None
    project_id = None

    if action.project:
        try:
            project = gl.find_project(action.project)
        except GitLabAPIError as e:
            warnings.append(f"Project lookup failed: {e.message}")
        if project:
            project_id = project["id"]
            target["project"] = {
                "id": project["id"],
                "path": project.get("path_with_namespace"),
                "name": project.get("name"),
                "web_url": project.get("web_url"),
            }
            notes.append(f"Project '{action.project}' → {project.get('path_with_namespace')} (id {project['id']})")
        else:
            warnings.append(f"Project '{action.project}' not found — set the project id manually")
    else:
        warnings.append("No project specified")

    payload: dict = {}
    extras: dict = {}
    method, path = "", ""

    def resolve_milestone():
        if action.milestone and project_id:
            try:
                m = gl.find_milestone(project_id, action.milestone)
            except GitLabAPIError as e:
                warnings.append(f"Milestone lookup failed: {e.message}")
                return None
            if m:
                notes.append(f"Milestone '{action.milestone}' → id {m['id']}")
                return m["id"]
            warnings.append(f"Milestone '{action.milestone}' not found — skipped")
        return None

    # ── ISSUE CREATE ─────────────────────────────────────────────────────
    if action.action == ACTION_CREATE_ISSUE:
        payload["title"] = action.title
        payload["description"] = _compose_description(action.description, action.acceptance_criteria)
        if project_id and action.assignees:
            payload["assignee_ids"] = _resolve_assignees(project, action.assignees, notes, warnings)
        if action.labels:
            payload["labels"] = list(action.labels)  # names accepted directly by the API
        mid = resolve_milestone()
        if mid:
            payload["milestone_id"] = mid
        if action.due_date:
            payload["due_date"] = action.due_date
        if action.confidential is not None:
            payload["confidential"] = action.confidential
        extras["time_estimate"] = action.time_estimate
        extras["comments"] = action.comments or []
        method, path = "POST", f"/projects/{project_id}/issues"

    # ── ISSUE UPDATE ─────────────────────────────────────────────────────
    elif action.action == ACTION_UPDATE_ISSUE:
        if action.title:
            payload["title"] = action.title
        composed = _compose_description(action.description, action.acceptance_criteria)
        if composed is not None:
            payload["description"] = composed
        if project_id and action.assignees:
            payload["assignee_ids"] = _resolve_assignees(project, action.assignees, notes, warnings)
        if action.labels:
            payload["labels"] = list(action.labels)
        if action.add_labels:
            payload["add_labels"] = list(action.add_labels)
        if action.remove_labels:
            payload["remove_labels"] = list(action.remove_labels)
        mid = resolve_milestone()
        if mid:
            payload["milestone_id"] = mid
        if action.due_date:
            payload["due_date"] = action.due_date
        if action.confidential is not None:
            payload["confidential"] = action.confidential
        if action.state_event:
            payload["state_event"] = action.state_event
        extras["time_estimate"] = action.time_estimate
        extras["comments"] = action.comments or []
        if action.issue_iid is None:
            warnings.append("No issue IID given — required to update")
        method, path = "PUT", f"/projects/{project_id}/issues/{action.issue_iid}"

    # ── ADD COMMENT ──────────────────────────────────────────────────────
    elif action.action == ACTION_ADD_COMMENT:
        extras["comments"] = action.comments or []
        if not extras["comments"]:
            warnings.append("No comment text found")
        if action.issue_iid is None:
            warnings.append("No issue IID given — required to comment")
        method, path = "POST", f"/projects/{project_id}/issues/{action.issue_iid}/notes"

    # ── LINK ISSUES ──────────────────────────────────────────────────────
    elif action.action == ACTION_LINK_ISSUES:
        target_project_id = project_id
        if action.target_project:
            try:
                tproj = gl.find_project(action.target_project)
            except GitLabAPIError as e:
                tproj = None
                warnings.append(f"Target project lookup failed: {e.message}")
            if tproj:
                target_project_id = tproj["id"]
                notes.append(f"Target project '{action.target_project}' → id {tproj['id']}")
            else:
                warnings.append(f"Target project '{action.target_project}' not found")
        payload["target_project_id"] = target_project_id
        payload["target_issue_iid"] = action.target_issue_iid
        payload["link_type"] = action.link_type or "relates_to"
        if action.issue_iid is None:
            warnings.append("No source issue IID — required to link")
        if action.target_issue_iid is None:
            warnings.append("No target issue IID — required to link")
        method, path = "POST", f"/projects/{project_id}/issues/{action.issue_iid}/links"

    # ── CREATE CHILD ITEM (work item under a parent issue) ────────────────
    elif action.action == ACTION_CREATE_CHILD_ITEM:
        payload["title"] = action.title
        payload["work_item_type"] = action.work_item_type or "task"
        payload["description"] = _compose_description(action.description, action.acceptance_criteria)
        if project_id and action.assignees:
            payload["assignee_ids"] = _resolve_assignees(project, action.assignees, notes, warnings)
        if project_id and action.labels:
            payload["label_ids"] = _resolve_label_ids(project_id, action.labels, notes, warnings)
        # Resolve the parent issue's global work-item id (hierarchy uses id, not iid).
        if project_id and action.parent_issue_iid is not None:
            try:
                parent = gl.get_issue(project_id, action.parent_issue_iid)
                payload["parent_id"] = parent.get("id")
                notes.append(f"Parent issue #{action.parent_issue_iid} → work item id {parent.get('id')}")
            except GitLabAPIError as e:
                warnings.append(f"Parent issue lookup failed: {e.message}")
        else:
            warnings.append("No parent issue IID — required to create a child item")
        if not action.title:
            warnings.append("No title for the child item")
        method, path = "POST", f"/projects/{project_id}/-/work_items"

    else:
        raise ValueError(f"Unsupported action: {action.action}")

    return {
        "action": action.action,
        "summary": _summarize(action, target),
        "target": target,
        "payload": payload,
        "extras": extras,
        "endpoint": {"method": method, "path": path},
        "resolution_notes": notes,
        "warnings": warnings,
    }


def _normalize_issue(issue: dict) -> dict:
    """Flatten a raw GitLab issue into the fields the UI/agent care about."""
    return {
        "iid": issue.get("iid"),
        "id": issue.get("id"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "description": issue.get("description"),
        "web_url": issue.get("web_url"),
        "labels": issue.get("labels") or [],
        "assignees": [a.get("username") for a in (issue.get("assignees") or [])],
        "author": (issue.get("author") or {}).get("username"),
        "milestone": (issue.get("milestone") or {}).get("title"),
        "due_date": issue.get("due_date"),
        "confidential": issue.get("confidential"),
        "time_estimate": (issue.get("time_stats") or {}).get("human_time_estimate"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
    }


def _format_issue_summary(i: dict, project_path: str = None) -> str:
    """A concise text summary of an issue (also stored in chat memory)."""
    where = f" in {project_path}" if project_path else ""
    lines = [f"**#{i['iid']} {i.get('title') or '(untitled)'}** — {i.get('state', '')}{where}"]
    meta = []
    if i.get("assignees"):
        meta.append("Assignees: " + ", ".join("@" + a for a in i["assignees"]))
    if i.get("labels"):
        meta.append("Labels: " + ", ".join(i["labels"]))
    if i.get("milestone"):
        meta.append("Milestone: " + i["milestone"])
    if i.get("due_date"):
        meta.append("Due: " + i["due_date"])
    if i.get("time_estimate"):
        meta.append("Estimate: " + i["time_estimate"])
    if meta:
        lines.append(" · ".join(meta))
    return "\n".join(lines)


def build_issue_view(action: GitLabAction) -> dict:
    """READ-ONLY: fetch a single issue for viewing. Returns {issue, summary, warnings}.

    Executes immediately (no approval gate) since it only reads.
    """
    if not action.project:
        return {"issue": None, "summary": "Which project is the issue in?", "warnings": ["No project specified"]}
    if action.issue_iid is None:
        return {"issue": None, "summary": "What's the issue number (IID)?", "warnings": ["No issue IID"]}
    try:
        project = gl.find_project(action.project)
    except GitLabAPIError as e:
        return {"issue": None, "summary": f"Project lookup failed: {e.message}", "warnings": [e.message]}
    if not project:
        return {"issue": None, "summary": f"Project '{action.project}' not found.",
                "warnings": [f"Project '{action.project}' not found"]}
    try:
        raw = gl.get_issue(project["id"], action.issue_iid)
    except GitLabAPIError as e:
        return {"issue": None, "summary": f"Couldn't fetch issue #{action.issue_iid}: {e.message}",
                "warnings": [e.message]}
    issue = _normalize_issue(raw)
    issue["project_path"] = project.get("path_with_namespace")
    issue["project_id"] = project["id"]
    return {"issue": issue, "summary": _format_issue_summary(issue, project.get("path_with_namespace")), "warnings": []}


def _normalize_issue_brief(issue: dict) -> dict:
    """Compact issue row for lists (no description)."""
    return {
        "iid": issue.get("iid"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "web_url": issue.get("web_url"),
        "labels": issue.get("labels") or [],
        "assignees": [a.get("username") for a in (issue.get("assignees") or [])],
        "milestone": (issue.get("milestone") or {}).get("title"),
        "due_date": issue.get("due_date"),
        "project_path": (issue.get("references") or {}).get("full", "").split("#")[0] or None,
    }


def _list_filter_params(action: GitLabAction) -> dict:
    """Map a GitLabAction's filter fields to the list/stats query params."""
    params = {}
    if action.state_filter:
        params["state"] = action.state_filter
    if action.search:
        params["search"] = action.search
    if action.labels:
        params["labels"] = ",".join(action.labels)
    if action.milestone:
        params["milestone"] = action.milestone
    if action.author:
        params["author_username"] = action.author
    if action.assignees:
        params["assignee_username"] = list(action.assignees)
    return params


def _resolve_list_scope(action: GitLabAction, warnings: list):
    """Pick the endpoint scope: project, group, or global. Returns (base_path, where)."""
    if action.project:
        try:
            project = gl.find_project(action.project)
        except GitLabAPIError as e:
            warnings.append(f"Project lookup failed: {e.message}")
            return None, None
        if not project:
            warnings.append(f"Project '{action.project}' not found")
            return None, None
        return f"/projects/{project['id']}", project.get("path_with_namespace")
    if action.group:
        try:
            group = gl.find_group(action.group)
        except GitLabAPIError as e:
            warnings.append(f"Group lookup failed: {e.message}")
            return None, None
        if not group:
            warnings.append(f"Group '{action.group}' not found")
            return None, None
        return f"/groups/{group['id']}", group.get("full_path")
    return "", "your issues"  # global /issues


def build_issue_list(action: GitLabAction) -> dict:
    """READ-ONLY: list/search issues in a project, group, or globally."""
    warnings = []
    base, where = _resolve_list_scope(action, warnings)
    if base is None:
        return {"issues": [], "summary": warnings[0] if warnings else "Nothing to list.", "warnings": warnings}

    params = _list_filter_params(action)
    limit = min(action.max_results or MAX_SEARCH_RESULTS, 100)
    params["per_page"] = limit
    params["page"] = 1
    if base == "":  # global scope requires a scope value
        params["scope"] = action.scope or "assigned_to_me"

    path = f"{base}/issues"
    try:
        raw = gl.get(path, params) or []
    except GitLabAPIError as e:
        return {"issues": [], "summary": f"Couldn't list issues: {e.message}", "warnings": [e.message]}

    issues = [_normalize_issue_brief(i) for i in raw]
    more = " (showing first %d — refine to narrow)" % limit if len(issues) >= limit else ""
    summary = f"Found {len(issues)} issue(s) in {where}{more}."
    return {
        "issues": issues,
        "summary": summary,
        "warnings": warnings,
        "endpoint": {"method": "GET", "path": path},
    }


def build_issue_statistics(action: GitLabAction) -> dict:
    """READ-ONLY: counts of opened/closed/all issues for a project, group, or globally."""
    warnings = []
    base, where = _resolve_list_scope(action, warnings)
    if base is None:
        return {"statistics": None, "summary": warnings[0] if warnings else "Nothing to count.", "warnings": warnings}

    params = _list_filter_params(action)
    path = f"{base}/issues_statistics"
    try:
        data = gl.get(path, params) or {}
    except GitLabAPIError as e:
        return {"statistics": None, "summary": f"Couldn't get statistics: {e.message}", "warnings": [e.message]}

    counts = (data.get("statistics") or {}).get("counts") or {}
    summary = (f"Issues in {where} — open: {counts.get('opened', '?')}, "
               f"closed: {counts.get('closed', '?')}, all: {counts.get('all', '?')}.")
    return {"statistics": counts, "summary": summary, "warnings": warnings}


def _summarize(action: GitLabAction, target: dict) -> str:
    proj = (target.get("project") or {}).get("path") or action.project or "?"
    if action.action == ACTION_CREATE_ISSUE:
        return f'Create issue "{action.title or "(untitled)"}" in {proj}'
    if action.action == ACTION_UPDATE_ISSUE:
        return f"Update issue #{action.issue_iid} in {proj}"
    if action.action == ACTION_ADD_COMMENT:
        return f"Add comment to issue #{action.issue_iid} in {proj}"
    if action.action == ACTION_LINK_ISSUES:
        rel = (action.link_type or "relates_to").replace("_", " ")
        return f"Link issue #{action.issue_iid} — {rel} — #{action.target_issue_iid} in {proj}"
    if action.action == ACTION_CREATE_CHILD_ITEM:
        kind = action.work_item_type or "task"
        return f'Create child {kind} "{action.title or "(untitled)"}" under issue #{action.parent_issue_iid} in {proj}'
    return action.action


def execute_plan(plan: dict) -> dict:
    """Perform the (possibly user-edited) plan against GitLab.

    MUTATING. Called only from the approval-gated /api/execute endpoint.
    Returns a result dict with the primary object, any follow-up results,
    and a resolved web_url where applicable.
    """
    action = plan["action"]
    target = plan.get("target") or {}
    payload = clean(plan.get("payload") or {})
    extras = plan.get("extras") or {}

    project_id = (target.get("project") or {}).get("id")
    issue_iid = target.get("issue_iid")

    if project_id is None:
        raise GitLabAPIError(400, "No project id — cannot execute")

    results = {"action": action, "steps": [], "web_url": None}

    if action == ACTION_CREATE_ISSUE:
        issue = gl.create_issue(project_id, payload)
        iid = issue["iid"]
        results["web_url"] = issue.get("web_url")
        results["steps"].append({"step": "create_issue", "ok": True, "iid": iid})
        _run_issue_extras(project_id, iid, extras, results)

    elif action == ACTION_UPDATE_ISSUE:
        issue = gl.update_issue(project_id, issue_iid, payload)
        results["web_url"] = issue.get("web_url")
        results["steps"].append({"step": "update_issue", "ok": True, "iid": issue_iid})
        _run_issue_extras(project_id, issue_iid, extras, results)

    elif action == ACTION_ADD_COMMENT:
        for body in extras.get("comments") or []:
            note = gl.add_comment(project_id, issue_iid, body)
            results["steps"].append({"step": "comment", "ok": True, "id": note.get("id")})
        issue = gl.get_issue(project_id, issue_iid)
        results["web_url"] = issue.get("web_url")

    elif action == ACTION_LINK_ISSUES:
        link = gl.create_issue_link(project_id, issue_iid, payload)
        results["steps"].append({"step": "link_issues", "ok": True, "link_type": payload.get("link_type")})
        # The links endpoint returns the (source) issue link; surface its web_url if present.
        results["web_url"] = (link.get("source_issue") or {}).get("web_url") if isinstance(link, dict) else None

    elif action == ACTION_CREATE_CHILD_ITEM:
        body = _build_work_item_body(payload)
        item = gl.create_work_item(project_id, body)
        results["steps"].append({"step": "create_child_item", "ok": True, "id": item.get("id")})
        results["web_url"] = item.get("web_url")
        results["work_item"] = item

    else:
        raise ValueError(f"Unsupported action: {action}")

    return results


def _build_work_item_body(payload: dict) -> dict:
    """Assemble the nested Work Items create body from the flat, edited payload."""
    body = {
        "title": payload.get("title"),
        "work_item_type_name": payload.get("work_item_type") or "task",
    }
    features = {}
    if payload.get("description"):
        features["description"] = {"description": payload["description"]}
    if payload.get("assignee_ids"):
        features["assignees"] = {"assignee_ids": payload["assignee_ids"]}
    if payload.get("label_ids"):
        features["labels"] = {"label_ids": payload["label_ids"]}
    if payload.get("parent_id") is not None:
        features["hierarchy"] = {"parent_id": payload["parent_id"]}
    if features:
        body["features"] = features
    return body


def _run_issue_extras(project_id, iid, extras, results):
    """Apply time estimate and comments as follow-up calls on an issue."""
    estimate = extras.get("time_estimate")
    if estimate:
        gl.set_time_estimate(project_id, iid, estimate)
        results["steps"].append({"step": "time_estimate", "ok": True, "duration": estimate})
    for body in extras.get("comments") or []:
        note = gl.add_comment(project_id, iid, body)
        results["steps"].append({"step": "comment", "ok": True, "id": note.get("id")})
