# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI + LangGraph chatbot that turns natural-language chat into **approval-gated** GitLab Issue
actions. See [README.md](README.md) for the user-facing feature list and example prompts.

**Stock GitLab only.** This deliberately targets endpoints and fields that exist in official
GitLab CE/EE. The following are fork-only and **must not be added**:

- `story_points` — a custom integer field on some forks' issue payload. Official GitLab has
  `weight` (Premium), not story points. Do not add a `weight` fallback or a
  `story_points_field` config knob either.
- `priority` (integer 1–4 on the issue payload) — fork-only. Official GitLab has *label*
  priority, not an issue field.
- `/projects/:id/recurring_tasks` (create/update) — fork-only; no such REST resource upstream.
  That also rules out `cron`, `cron_timezone`, `active`, `ends_on`, `duration_days` and
  `source_issue_iid` fields.

`tests/test_build_plan.py::test_fork_only_fields_are_not_supported` guards this: it asserts the
fields are absent from `GitLabAction` and that passing them as stray kwargs never reaches a payload.

Two things that *are* upstream but come with caveats — keep, but do not assume they work
everywhere: `POST /projects/:id/-/work_items` (`create_child_item`) is marked experimental by
GitLab and is primarily a GraphQL API; the `blocks` / `is_blocked_by` link types need
Premium/Ultimate, while `relates_to` works on all tiers.

## Setup & commands

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp core/config.sample.json core/config.json   # required — config.json is gitignored, not present in a fresh clone
cp .env.example .env                          # GITLAB_TOKEN, GROQ_API_KEY, POSTGRES_URI

python app.py            # uvicorn on config.server.host:port (default 0.0.0.0:8000), reload=True
pytest -q                # tests are fully offline: no DB, network, or API keys needed
pytest tests/test_build_plan.py::test_create_issue_full -q   # single test
```

**Always run from the repo root.** [core/config.py](core/config.py) and [core/secrets.py](core/secrets.py)
open `Path("core/config.json")` relative to the CWD, so any other working directory raises
`FileNotFoundError` at import — including under pytest (`pytest.ini` sets `pythonpath = .`).

There is no linter or formatter configured.

## The approval gate (the core invariant)

The LLM can never mutate GitLab. Preserve this split when changing anything:

- **Read-only, runs automatically**: `POST /api/chat`, `GET /api/history`, `GET /api/issue`, every
  resolver in [services/gitlab_service.py](services/gitlab_service.py) (`find_project`, `find_user`,
  `find_label`, `find_milestone`, `get_issue`), and the `build_*` functions.
- **Mutating, only ever reached from `POST /api/execute`**: `execute_plan` and the `GitLabClient`
  create/update/comment/estimate/link/work-item methods. `/api/execute` is triggered solely by the
  UI's "Approve & Send" button, on the payload **as the user edited it** — never on the plan the
  server produced.

Two escape hatches from the gate, both deliberate and read-only:

1. `READ_ACTIONS` in [agents/schema.py](agents/schema.py) (`get_issue`, `list_issues`,
   `issue_statistics`) execute immediately in `chat_turn` — no proposal card.
2. The `IssueLookup` tool *is* executed inside the graph (see below).

## Architecture

**Two tools with opposite semantics** ([agents/graph.py](agents/graph.py)): both `GitLabAction` and
`IssueLookup` are bound to the model, but `GitLabAction` is a *signal only* — the graph never runs it,
it just emits a placeholder `ToolMessage` so message history stays valid, and `chat_turn` turns the
tool args into a proposal (or a read-only result). `IssueLookup` is genuinely executed in
`_agent_node`'s bounded loop (`MAX_TOOL_STEPS = 4`) so the model can fetch an existing issue's real
content before proposing an edit. Every tool call must get a matching `ToolMessage` or the next turn's
history is invalid.

**Request flow**: `POST /api/chat` → `chat_turn` → LangGraph single-node graph → model emits
`GitLabAction` → `build_action_plan` runs read-only resolvers and returns
`{action, summary, target, payload, extras, endpoint, resolution_notes, warnings}` → UI renders an
editable form → `POST /api/execute` → `execute_plan`.

`payload` is the literal GitLab request body. `extras` holds follow-up calls that need separate
endpoints (`time_estimate`, `comments`) — `_run_issue_extras` applies them after the main create/update.
`target` carries resolved IDs (project id, issue iid) and is what `execute_plan` routes on.

**Memory** ([agents/memory.py](agents/memory.py)): LangGraph `PostgresSaver`, one `thread_id` per
conversation, opened lazily via `lru_cache` so the app boots without a DB. A single psycopg connection
is shared, so **all checkpointer access must hold `memory_lock()`** (FastAPI runs sync routes in a
threadpool). The compiled graph is also `lru_cache`d — `graph._graph.cache_clear()` is required after
swapping the checkpointer in tests.

**LLM** ([agents/llm.py](agents/llm.py)): Groq primary, Bedrock fallback. On double failure
`_invoke_llm` re-raises the **Groq** error, not the AWS one — the Bedrock error is usually just
"no credentials" and hides the real cause.

**Config layering**: non-secret `core/config.json` (copied from `config.sample.json`) flattened into
UPPER_CASE constants by `core/config.py`; secrets from `core/secrets.py`, which switches on
`config.env` — `"local"` reads `.env` via python-dotenv, `"production"` reads AWS Secrets Manager
(TTL-cached). `core/settings.py` exposes the `limits`/`app` sections as a `settings` singleton.
Never read `os.getenv` outside `core/secrets.py`.

## Conventions that bite

- **`gl` is a module-level singleton** constructed at import time in `gitlab_service.py`. Tests
  `monkeypatch.setattr(svc.gl, "find_project", ...)` on the instance rather than injecting a client.
- **Groq validates tool args strictly** against the JSON schema and LLMs emit `"2"` for ints, which
  Groq rejects with a 400. `GitLabAction` therefore declares numeric/bool fields as `int|str` /
  `bool|str` (`IntLike`, `BoolLike`) and coerces in `mode="before"` validators. Any new numeric field
  must be added to the `_coerce_int_fields` validator list, and the prompt's "MUST be JSON numbers"
  rule updated.
- **Resolvers degrade, they don't raise.** A missing project/user/label appends to `warnings` and
  leaves the field blank for the human to fill in. Keep that behavior — the plan is always shown.
- **The proposal form is generic over payload keys.** `buildForm` in [static/app.js](static/app.js)
  iterates the payload dict; `fieldType(key)` and `LABELS` map key names to input widgets. A new payload
  key renders automatically as a string input — add it to the key lists / `LABELS` for correct typing.

## Adding a new action

Touch all five, in order:

1. [agents/schema.py](agents/schema.py) — `ACTION_*` constant, add to `ACTIONS` (and `READ_ACTIONS` if
   read-only), any new fields on `GitLabAction` + validator lists.
2. [agents/prompt.py](agents/prompt.py) — capability bullet and a field-extraction rule; the model
   only uses what the prompt describes.
3. `build_action_plan` in [services/gitlab_service.py](services/gitlab_service.py) — a branch setting
   `payload`, `extras`, and `method, path`; plus a case in `_summarize`.
4. `execute_plan` (mutating actions) or `_run_read_action` in [agents/graph.py](agents/graph.py)
   (read-only actions).
5. [static/app.js](static/app.js) — key lists / `LABELS`, and `buildForm` if the action needs
   target-level id fields.

Add coverage in [tests/test_build_plan.py](tests/test_build_plan.py) (payload assembly with stubbed
resolvers) and, if the graph path changes, [tests/test_chat_turn.py](tests/test_chat_turn.py) (real
LangGraph flow with a `FakeLLM` returning preset `AIMessage`s and an `InMemorySaver`).
