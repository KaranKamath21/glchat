# 🦊 GitLab AI Agent

A **conversational, memory-backed** agent that turns natural-language chat into GitLab
**Issue** actions — create, edit, comment, link and break down — behind a web UI
that previews the **exact API payload**, lets you edit every field, and **sends nothing to
GitLab until you approve it**.

It holds a normal contextual conversation (answers "what can you do?", asks clarifying
questions) and **remembers the whole conversation** via LangGraph's checkpointer, persisted in
**PostgreSQL**. When you ask for an action, it proposes an approval-gated payload inline.

Built with **FastAPI**, **LangGraph** + **LangChain** (Groq primary + AWS Bedrock fallback),
and a small static chat front-end. Designed as a sibling of the `cicero-chatbot` project.
It uses only stock GitLab REST endpoints, so it works against gitlab.com or any self-managed
instance.

---

## 🌟 What it does

- **Conversational & contextual** — chat naturally; it answers questions about its
  capabilities and remembers earlier turns. History is persisted with LangGraph's
  **PostgreSQL checkpointer** (one `thread_id` per conversation), so context survives restarts.
- **Create an Issue** — title, description with an **Acceptance Criteria** checklist,
  assignee(s), labels, due date, milestone, time-tracking **estimate**, and one or more
  initial **comments**.
- **Edit an existing Issue** — relabel, reassign, change due date / estimate, edit
  description, set milestone, close/reopen.
- **Comment** — add notes to an existing issue.
- **View an issue** — show an existing issue's current details (read-only; runs immediately,
  no approval needed).
- **List / search issues** — find issues by project, group, or globally ("my issues"), filtered
  by state, labels, assignee, author, milestone, or free text; plus **statistics** (open/closed/
  all counts). Read-only.
- **Link issues** — relate two issues (`relates_to` / `blocks` / `is_blocked_by`).
- **Child items** — create a sub-item (e.g. a `task`) under a parent issue via the Work Items
  API (`hierarchy.parent_id`). *Note: GitLab marks this endpoint experimental.*
- **Reference resolution** — turns human inputs into the IDs the API needs (project name →
  id, username → user id, label name → id, milestone name → id). These lookups are
  **read-only** and run automatically.

## 🔒 The approval gate (non-negotiable)

The agent **never** calls a mutating GitLab endpoint on its own.

| Endpoint | Mutates? | What it does |
|---|---|---|
| `POST /api/chat` | ❌ read-only | One conversation turn (with memory). Returns a chat reply and, when you asked for an action, an editable **proposal** (built via read-only lookups) |
| `GET /api/history` | ❌ read-only | The persisted conversation for a `thread_id` (page reloads) |
| `GET /api/issue` | ❌ read-only | Fetches an existing issue for the edit flow |
| `POST /api/execute` | ✅ **only** mutating path | Performs the reviewed proposal (create/update/comment/estimate/link/child item) |

The LLM can only **propose** (by calling the `GitLabAction` tool, which just runs read-only
resolvers). `/api/execute` is triggered **solely** by the **"Approve & Send to GitLab"** button
in the UI, operating on the payload *as you edited it*.

---

## 🏗️ Architecture

```
gitlab-ai-agent/
├── app.py                      # FastAPI entrypoint: serves UI + JSON API (the approval gate)
├── core/
│   ├── config.py               # flattens config.json + secrets into constants
│   ├── config.sample.json      # copy → config.json
│   ├── secrets.py              # env-switched secrets (local: env vars, prod: AWS Secrets Manager)
│   └── settings.py             # tunable limits singleton
├── agents/
│   ├── llm.py                  # Groq (primary) + Bedrock (fallback) model handles (lazy)
│   ├── schema.py               # Pydantic GitLabAction (bound to the LLM as a tool)
│   ├── prompt.py               # chatbot system prompt (capabilities + when to call the tool)
│   ├── memory.py               # LangGraph PostgreSQL checkpointer (conversation memory)
│   └── graph.py                # LangGraph agent: chat_turn(), get_history()
├── services/
│   └── gitlab_service.py       # GitLab client + read-only resolvers + payload builder + mutations
├── templates/index.html        # chat UI
├── static/{app.js,style.css}   # chat logic + inline proposal cards + styling
├── requirements.txt
├── .env.example
└── README.md
```

---

## 🚀 Quick start

### 1. Prerequisites
- Python 3.10+
- **PostgreSQL** (for conversation memory) — any reachable database; the app creates its own
  checkpoint tables on first run
- A GitLab **Personal Access Token** with the `api` scope
- A **Groq API key** (primary LLM). AWS Bedrock creds are optional (fallback only).

### 2. Install
```bash
cd /var/www/html/gitlab-ai-agent
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure (non-secret)
```bash
cp core/config.sample.json core/config.json
```
Edit `core/config.json`:
- `gitlab.base_url` — your instance, e.g. `https://gitlab.example.com`
- `gitlab.default_project` — optional convenience default
- `llm.groq.chat_model` — a Groq model that supports tool use (default `llama-3.3-70b-versatile`)
- `aws.model_id` / `aws.bedrock_region` — Bedrock fallback model (optional)

### 4. Secrets (never committed)
```bash
cp .env.example .env
```
Set in `.env`:
```
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx
POSTGRES_URI=postgresql://user:password@host:5432/gitlab_ai_agent   # conversation memory
AWS_ACCESS_KEY_ID=...      # optional (Bedrock fallback)
AWS_SECRET_ACCESS_KEY=...  # optional
```
> The database named in `POSTGRES_URI` must exist; the app creates the LangGraph checkpoint
> tables inside it automatically on first use.
> In production (`config.json` → `"env": "production"`) secrets are read from AWS Secrets
> Manager (`aws.secret_name`) instead of `.env`.

### 5. Run
```bash
python app.py
```
Open **http://localhost:8000**.

---

## 🖥️ Using it

1. **Chat.** Ask "what can you do?", or just describe what you need. The agent remembers the
   conversation, so you can refine across turns ("actually make the due date next Monday",
   "also assign asmith").
2. When your message is an actionable request, the reply includes an inline **proposal card**:
   the proposed action, exact endpoint, resolution notes/warnings, and an **editable form** for
   every field (plus an **Advanced ▸ raw JSON** override).
3. Click **✔ Approve & Send to GitLab** (or **Cancel**). Nothing is sent until you approve.
4. The result appears in the chat — a link to the created/updated issue, or the GitLab error.
5. **New chat** starts a fresh conversation (`thread_id`); reloading the page resumes the
   current one from Postgres.

### Example prompts
- *Create an issue in Mobile App titled "Fix broken login" assigned to jdoe with labels bug,
  frontend, due next Friday. Acceptance criteria: user can log in; failed attempts are
  logged.*
- *Update issue #12 in Mobile App: reassign to asmith, add label needs-review, close it,
  and add a comment "please prioritize".*
- *Add a comment to issue #7 in Mobile App: "Verified on staging."*
- *Show me issue #12 in Mobile App.* (read-only — displays it immediately)
- *List open issues assigned to jdoe in Mobile App labelled bug.* · *How many open issues are in Mobile App?*
- *In Mobile App, make issue #12 block issue #20.*
- *Add a child task "Write unit tests" under issue #12 in Mobile App, assign jdoe.*

---

## 📡 GitLab endpoints used

| Purpose | Method & path |
|---|---|
| View a single issue | `GET /projects/:id/issues/:issue_iid` (read-only) |
| List / search issues | `GET /projects/:id/issues` · `GET /groups/:id/issues` · `GET /issues` (read-only) |
| Issue statistics | `GET .../issues_statistics` (read-only) |
| Create issue | `POST /projects/:id/issues` |
| Update issue | `PUT /projects/:id/issues/:issue_iid` |
| Comment | `POST /projects/:id/issues/:issue_iid/notes` |
| Time estimate | `POST /projects/:id/issues/:issue_iid/time_estimate` (`duration`) |
| Link issues | `POST /projects/:id/issues/:issue_iid/links` (`target_project_id`, `target_issue_iid`, `link_type`) |
| Child item | `POST /projects/:id/-/work_items` (`work_item_type_name`, `features.hierarchy.parent_id`) — experimental |
| Resolve project / user / label / milestone | `GET /projects`, `/users`, `/projects/:id/labels`, `/projects/:id/milestones` |

Two caveats on tier and stability: the `blocks` / `is_blocked_by` link types require
Premium or Ultimate (`relates_to` works on every tier), and the Work Items endpoint used for
child items is marked experimental by GitLab and may change.

---

## 🧯 Error handling

- **Bad/expired token** → GitLab `401` surfaced in the UI; resolvers degrade with warnings.
- **Unknown project/user/label/milestone** → a warning in the plan; the ID field is left for
  you to fill in manually before approving.
- **GitLab validation errors** (e.g. missing title) → the exact GitLab message is shown after
  you approve, and the review form stays open so you can fix and retry.
