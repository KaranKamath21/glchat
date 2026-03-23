"""Typed wrapper over the tunable ``limits``/``app`` config sections.

Exposed as a ``settings`` singleton, mirroring the cicero-chatbot convention:
``from core.settings import settings``.
"""

import json
from pathlib import Path


class AgentSettings:
    def __init__(self, config: dict):
        limits = config["limits"]
        app = config["app"]

        self.http_timeout = limits["http_timeout"]
        self.llm_timeout = limits["llm_timeout"]
        self.max_search_results = limits["max_search_results"]

        self.app_title = app["title"]
        self.app_version = app["version"]


CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH, "r") as f:
    config_data = json.load(f)

settings = AgentSettings(config_data)
