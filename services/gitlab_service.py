"""GitLab REST API service.

Reuses the authentication / request pattern from the reference script
(``gitlab_audit.py``): a ``requests.Session`` with the ``PRIVATE-TOKEN`` header,
a retry adapter, and pagination helpers.

Resolvers (``find_project``, ``find_user``, ``find_label``, ``find_milestone``)
are read-only and safe to call while assembling a payload. The create/update/
comment helpers mutate and must only run once the user has approved.
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
