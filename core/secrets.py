"""Environment-switched secret loading.

Mirrors the cicero-chatbot secrets convention: for ``env == "local"`` secrets
are read directly from environment variables (``.env`` via python-dotenv); for
``env == "production"`` they are pulled from AWS Secrets Manager and cached.
Never hardcode tokens here.
"""

import os
import json
import logging
from pathlib import Path

import boto3
from cachetools import TTLCache
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env file for local development fallback
load_dotenv()

_CONFIG_PATH = Path("core/config.json")

with open(_CONFIG_PATH) as f:
    _config = json.load(f)

ENV = _config.get("env", "local")
SECRET_NAME = _config["aws"]["secret_name"]
SECRET_REGION = _config["aws"]["secret_region"]

CACHE_MAX_SIZE = _config["aws"]["cache"]["max_size"]
CACHE_TTL = _config["aws"]["cache"]["ttl_seconds"]

_cache = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=CACHE_TTL)


def get_local_secrets():
    """Build the secrets dictionary from local environment variables."""
    return {
        "gitlab": {
            "token": os.getenv("GITLAB_TOKEN", ""),
        },
        "llm": {
            "groq_api_key": os.getenv("GROQ_API_KEY", ""),
        },
        "aws": {
            "access_key_id": os.getenv("AWS_ACCESS_KEY_ID", ""),
            "secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        },
        "database": {
            "postgres_uri": os.getenv("POSTGRES_URI", ""),
        },
    }


def get_config_secret():
    """Fetch secrets from AWS Secrets Manager (production) or env vars (local)."""

    # 1. Local: read straight from environment variables (no AWS call)
    if ENV == "local":
        logger.info("[Secrets] Environment is 'local'. Loading secrets from environment variables.")
        return get_local_secrets()

    # 2. Production: load exclusively from AWS Secrets Manager (cached)
    if ENV == "production":
        if SECRET_NAME in _cache:
            return _cache[SECRET_NAME]

        try:
            client = boto3.client("secretsmanager", region_name=SECRET_REGION)
            response = client.get_secret_value(SecretId=SECRET_NAME)

            if "SecretString" not in response:
                raise ValueError("SecretString not found in AWS Secrets Manager response")

            secret_dict = json.loads(response["SecretString"])
            if "SECRET_JSON" in secret_dict:
                secret_dict = json.loads(secret_dict["SECRET_JSON"])

            _cache[SECRET_NAME] = secret_dict
            return secret_dict

        except Exception as e:
            logger.error(f"[Secrets] Failed to fetch secret '{SECRET_NAME}' from AWS Secrets Manager: {e}")
            raise e

    # 3. Unknown environment
    logger.error(f"[Secrets] Unknown environment '{ENV}'. Must be 'local' or 'production'.")
    raise ValueError(f"Unknown environment: {ENV}")
