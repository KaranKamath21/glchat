"""Flattened configuration constants.

Loads the non-secret ``core/config.json`` and merges in the env-switched
secret layer (``core/secrets.py``), exposing everything as UPPER_CASE module
constants. Mirrors the cicero-chatbot config convention.
"""

import json
from pathlib import Path

from core.secrets import get_config_secret
from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path("core/config.json")
with open(_CONFIG_PATH) as f:
    _config = json.load(f)

ENV = _config["env"]

# ── Server ────────────────────────────────────────────────────────────────
HOST = _config["server"]["host"]
PORT = _config["server"]["port"]

# ── LLM (non-secret) ────────────────────────────────────────────────────────
LLM_PROVIDER = _config["llm"].get("provider", "groq")
GROQ_CHAT_MODEL = _config["llm"]["groq"]["chat_model"]
GROQ_TEMPERATURE = _config["llm"]["groq"].get("temperature", 0)

# ── GitLab (non-secret) ─────────────────────────────────────────────────────
GITLAB_BASE_URL = _config["gitlab"]["base_url"]
GITLAB_API_PREFIX = _config["gitlab"].get("api_prefix", "/api/v4")
GITLAB_VERIFY_SSL = _config["gitlab"].get("verify_ssl", True)
GITLAB_DEFAULT_PROJECT = _config["gitlab"].get("default_project", "")

# ── AWS / Bedrock (non-secret) ──────────────────────────────────────────────
BEDROCK_AWS_REGION = _config["aws"]["bedrock_region"]
BEDROCK_MODEL_ID = _config["aws"]["model_id"]

# ── Limits ──────────────────────────────────────────────────────────────────
HTTP_TIMEOUT = _config["limits"]["http_timeout"]
LLM_TIMEOUT = _config["limits"]["llm_timeout"]
MAX_SEARCH_RESULTS = _config["limits"]["max_search_results"]

# ── Secrets ───────────────────────────────────────────────────────────────
_secret_config = get_config_secret()

GITLAB_TOKEN = _secret_config["gitlab"]["token"]
GROQ_API_KEY = _secret_config["llm"]["groq_api_key"]
AWS_ACCESS_KEY_ID = _secret_config["aws"]["access_key_id"]
AWS_SECRET_ACCESS_KEY = _secret_config["aws"]["secret_access_key"]

# Postgres connection string for LangGraph conversation memory (checkpointer).
POSTGRES_URI = _secret_config.get("database", {}).get("postgres_uri", "")
