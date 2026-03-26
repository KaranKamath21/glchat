"""GitLab REST API service.

Reuses the authentication / request pattern from the reference script
(``gitlab_audit.py``): a ``requests.Session`` with the ``PRIVATE-TOKEN`` header,
a retry adapter, and pagination helpers.

This module has a strict split:
  * READ-ONLY helpers (resolvers, ``get_issue``, ``build_action_plan``) may run
    automatically to assemble a payload.
  * MUTATING helpers (``create_issue``, ``update_issue``, ``add_comment``,
    ``set_time_estimate``) and ``execute_plan`` are only ever called from the
    approval-gated /api/execute endpoint.
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


def _summarize(action: GitLabAction, target: dict) -> str:
    proj = (target.get("project") or {}).get("path") or action.project or "?"
    if action.action == ACTION_CREATE_ISSUE:
        return f'Create issue "{action.title or "(untitled)"}" in {proj}'
    if action.action == ACTION_UPDATE_ISSUE:
        return f"Update issue #{action.issue_iid} in {proj}"
    if action.action == ACTION_ADD_COMMENT:
        return f"Add comment to issue #{action.issue_iid} in {proj}"
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

    else:
        raise ValueError(f"Unsupported action: {action}")

    return results


def _run_issue_extras(project_id, iid, extras, results):
    """Apply time estimate and comments as follow-up calls on an issue."""
    estimate = extras.get("time_estimate")
    if estimate:
        gl.set_time_estimate(project_id, iid, estimate)
        results["steps"].append({"step": "time_estimate", "ok": True, "duration": estimate})
    for body in extras.get("comments") or []:
        note = gl.add_comment(project_id, iid, body)
        results["steps"].append({"step": "comment", "ok": True, "id": note.get("id")})
