"""GitLab AI Agent — FastAPI entrypoint.

Serves the review UI and a small JSON API. The approval gate is enforced here:

  * POST /api/parse   — READ-ONLY. Runs the LLM parse + reference lookups and
                        returns a fully editable action plan. Never mutates.
  * POST /api/execute — MUTATING. The ONLY code path that creates/updates issues,
                        posts comments, sets estimates, or links items.
                        Triggered solely by the front-end "Approve & Send" button.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.config import (
    HOST,
    PORT,
    GITLAB_BASE_URL,
    GITLAB_DEFAULT_PROJECT,
)
from core.settings import settings
from agents.graph import chat_turn, get_history
from services.gitlab_service import (
    gl,
    execute_plan,
    GitLabAPIError,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

app = FastAPI(title=settings.app_title, version=settings.app_version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ── Request models ──────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    thread_id: str
    text: str


class ExecuteRequest(BaseModel):
    action: str
    target: Dict[str, Any] = {}
    payload: Dict[str, Any] = {}
    extras: Dict[str, Any] = {}
    endpoint: Optional[Dict[str, Any]] = None


# ── Routes ────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def api_config():
    """Non-secret config surfaced to the UI."""
    return {
        "gitlab_base_url": GITLAB_BASE_URL,
        "default_project": GITLAB_DEFAULT_PROJECT,
    }


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    """READ-ONLY: one conversation turn with memory.

    Returns a chat reply and, when the user requested an action, an editable
    approval-gated proposal. Never mutates GitLab.
    """
    text = (req.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "Empty message"})
    if not (req.thread_id or "").strip():
        return JSONResponse(status_code=400, content={"error": "Missing thread_id"})
    try:
        result = chat_turn(req.thread_id, text)
    except RuntimeError as e:  # e.g. POSTGRES_URI not set
        logger.error("[Chat] Memory unavailable: %s", e)
        return JSONResponse(status_code=503, content={"error": str(e)})
    except Exception as e:
        logger.error("[Chat] Turn failed: %s", e)
        return JSONResponse(status_code=502, content={"error": f"Chat failed: {e}"})
    return result


@app.get("/api/history")
def api_history(thread_id: str):
    """READ-ONLY: the persisted conversation for a thread (for page reloads)."""
    try:
        messages = get_history(thread_id)
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})
    except Exception as e:
        logger.error("[History] Failed: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"messages": messages}


@app.get("/api/issue")
def api_issue(project: str, iid: int):
    """READ-ONLY: fetch an existing issue's current fields for the edit flow."""
    try:
        proj = gl.find_project(project)
        if not proj:
            return JSONResponse(status_code=404, content={"error": f"Project '{project}' not found"})
        issue = gl.get_issue(proj["id"], iid)
    except GitLabAPIError as e:
        return JSONResponse(status_code=e.status, content={"error": e.message})
    return {"project": proj, "issue": issue}


@app.post("/api/execute")
def api_execute(req: ExecuteRequest):
    """MUTATING (approval-gated): perform the reviewed plan against GitLab."""
    plan = req.model_dump()
    logger.info("[Execute] Approved action=%s target=%s", plan.get("action"), plan.get("target"))
    try:
        result = execute_plan(plan)
    except GitLabAPIError as e:
        logger.warning("[Execute] GitLab error %s: %s", e.status, e.message)
        return JSONResponse(status_code=e.status, content={"error": e.message})
    except Exception as e:
        logger.error("[Execute] Unexpected failure: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"result": result}


if __name__ == "__main__":
    uvicorn.run("app:app", host=HOST, port=PORT, reload=True)
