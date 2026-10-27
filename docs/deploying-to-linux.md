# Deploying to a Linux container

Context for whoever — human or agent — moves this app from the Windows
workstation it was built on to a Linux container.

The app itself is portable Python and a static frontend; almost nothing here is
about the screening logic. What needs attention is the **chat harness**, which
shells out to a CLI, and a small number of deployment properties that are easy
to get wrong in ways that only show under load or after a restart.

Read [`../CLAUDE.md`](../CLAUDE.md) first for what the app is and how its parts
fit together. This document assumes that and covers only what changes.

---

## 1. Read this before changing anything

Three things will bite you, in rough order of how expensive they are to discover
late.

### 1.1 Run exactly one worker process

`backend/pipeline.py` keeps running-job progress in a module-level dict,
`JOBS`. Evaluation and audit runs are started with `asyncio.create_task` and
their progress is read back over `GET /api/jobs/{id}/progress`.

With more than one uvicorn worker, the job runs in one process and the poll
lands in another, which has never heard of that job id. The UI shows a job that
never progresses and then 404s. Nothing errors server-side, which is what makes
it hard to diagnose.

```
# correct
uvicorn backend.server:app --host 0.0.0.0 --port 8000

# breaks progress polling, silently
uvicorn backend.server:app --workers 4
gunicorn -k uvicorn.workers.UvicornWorker -w 4 backend.server:app
```

The same applies to horizontal scaling: **one replica**. Two replicas behind a
load balancer have the same split-brain problem, plus they would share a SQLite
file over whatever volume backs it, and per-chat-session workspaces created by
one replica would be missing on the other.

If the app ever needs to scale out, the fix is to move `JOBS` into the database
(a `job` table with progress rows) and the chat workspaces onto shared storage.
Until then, one process is a hard constraint, not a preference. Consider
enforcing it in the deployment manifest rather than trusting the command line.

### 1.2 The container is the sandbox

The harness adapters run their CLI with permission checks disabled —
`--dangerously-bypass-approvals-and-sandbox` for codex,
`--permission-mode bypassPermissions --dangerously-skip-permissions` for claude.
That is deliberate: the agent is given a scratch workspace and expected to run
the analysis tools in it without prompting a user who isn't there.

It means **the container boundary is the only thing containing that agent**. On
the Windows workstation it has been running with a developer's full user
rights, which is fine for a workstation and not fine for a deployment. In the
container:

- Run as a non-root user.
- Give the app write access to its data volume and nothing else.
- Do not mount the Docker socket, cloud credentials, or the host filesystem.
- Treat outbound network access as something to scope deliberately: the agent
  can reach whatever the container can.

`backend/chat/workspace.py` already strips the `config` table from the SQLite
copy it places in each workspace, because that table can hold a provider bearer
token. Keep that in mind if you add anything else to a workspace.

### 1.3 Chat is a synchronous HTTP request

A chat turn runs the model — and in Independent mode, a CLI subprocess — inside
the request. The adapter's own timeout is 300 s (`timeout` in
`backend/chat/adapters/*.py`).

Any ingress in front of the app must allow at least that long, or the browser
gets a gateway timeout while the turn is still running and the answer is lost.
Typical defaults are 30–60 s.

- nginx: `proxy_read_timeout 360s;`
- Azure App Gateway / most cloud LBs: raise the backend request timeout.
- If you cannot raise it, the alternative is to make chat asynchronous (start a
  turn, poll for the answer) the way evaluation already is. That is a real
  change, not a config tweak.

---

## 2. What is Windows-specific in the code

All of it already works on Linux. Listed so you know why it looks the way it
does and do not "simplify" it back into a bug.

| Where | What it does | On Linux |
|---|---|---|
| `chat/adapters/*.py` — `executable()` | Resolves the CLI with `shutil.which` and spawns the **resolved path** rather than the bare name | Harmless and still correct. It exists because an npm-installed `codex` on Windows is a `codex.CMD` shim that `CreateProcess` cannot launch by bare name. Keep it: it also protects against PATH oddities in a container. |
| `chat/adapters/*.py` — `encoding="utf-8"` on `subprocess.run` | Pins stdin/stdout encoding | Keep. Linux is usually UTF-8 already, but only because the locale says so; a container with `LANG` unset can default to ASCII and reintroduce exactly the failure this fixed (the brief contains em dashes). Also set `LANG=C.UTF-8` in the image. |
| `extraction.py` — `_fitz()` lazy import | Imports PyMuPDF on first use rather than at module load | Keep. The chat tools import the ranking code, which imports this module, and they run under whatever `python` the harness finds. |
| `chat/workspace.py` — brief names `sys.executable` | Tells the agent which interpreter to run the tools with | Keep, and note it resolves to the container's Python, which is correct. |
| `tools/build_corpus.py` | Generates the corpus PDFs with PyMuPDF | Works unchanged. |
| **Position description PDF** | Was produced from Markdown with **Word COM automation** | **Does not work on Linux.** See §6. |

---

## 3. Installing the harness CLI

The runtime image is `python:3.12-slim` and currently installs **neither** CLI,
so `chat_mode: harness` degrades to Guided on every turn — visibly, with the
reason shown in the UI. That degradation is by design and safe; the app is fully
functional without a CLI. Install one only if you want Independent mode.

Pick one. `claude` is the simpler install; `codex` is what the Windows box has
been running.

### 3.1 codex

Needs Node in the **runtime** stage, not just the build stage.

```dockerfile
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @openai/codex \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
```

### 3.2 claude

```dockerfile
RUN curl -fsSL https://claude.ai/install.sh | bash
# or, if you already have Node in the image:
# RUN npm install -g @anthropic-ai/claude-code
```

### 3.3 Both need a writable HOME

Each CLI keeps state under the user's home — codex under `CODEX_HOME`
(default `~/.codex`), claude under `CLAUDE_CONFIG_DIR` (default `~/.claude`).
A non-root user with no home directory, or a read-only root filesystem, makes
the CLI fail on first run in a way that reads as an auth error.

```dockerfile
RUN useradd --create-home --uid 10001 app
ENV HOME=/home/app \
    CODEX_HOME=/home/app/.codex \
    CLAUDE_CONFIG_DIR=/home/app/.claude
USER app
```

Put those directories on the data volume if you want a harness conversation to
survive a container restart. Losing them is not fatal — the next turn starts a
fresh conversation — but the chat forgets its history.

### 3.4 Which CLI the app calls

Set on the Configuration page, stored in config as `harness_cli`, defaulting
per provider in `backend/chat/session.py`:

```python
DEFAULT_HARNESS = {"ulproxy": "claude_cli", "fastllm": "codex_cli"}
```

If you install only one, check the stored value matches. A mismatch degrades to
Guided with "not installed or not on PATH", which is honest but easy to miss.

---

## 4. Harness authentication

The app does **not** ask the CLI to log in interactively. It injects the active
provider's endpoint and token into the subprocess environment —
`harness_env()` in `backend/llm/ulproxy.py` and `fastllm.py`:

| Provider | Injected |
|---|---|
| UL AI proxy | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_API_KEY` |
| SIS fastLLM | `OPENAI_BASE_URL`, `OPENAI_API_KEY` only — no Anthropic-compatible endpoint is known, so `claude_cli` cannot be routed there |

**Verify this actually authenticates before trusting it.** It is confirmed
working for codex against the UL proxy on Windows. What has not been proven is
whether a given CLI version prefers a stored `auth.json` over the environment,
in which case it will ignore the injected values and try to reach the public
API — which a locked-down container should refuse, giving a confusing error.
Test with the check in §7 as the first thing after the image builds.

The SIS gateway's `hl-project-id` / `hl-requester-id` attribution headers have
**no equivalent in a CLI's environment**. If that gateway requires them, harness
turns against it will fail and degrade to Guided. This is noted in
`fastllm.py::harness_env` and is a known limitation, not a bug to chase.

---

## 5. Process reaping — a change worth making

`codex` is a Node wrapper that spawns a native child. On Linux, killing only the
wrapper orphans the child to PID 1, where it keeps running, holding its model
connection and its share of memory. Our adapter uses `subprocess.run(...,
timeout=...)`, and on timeout Python kills only the direct child.

On a workstation this is invisible. In a long-lived container, timed-out turns
accumulate orphans until something notices.

The fix, following the pattern in the `hive-edge-minds` repo the adapters are
modelled on: spawn with `start_new_session=True` so the wrapper and its child
share a process group, and on timeout `os.killpg(os.getpgid(proc.pid),
SIGKILL)` rather than killing the one process. That requires moving from
`subprocess.run` to `Popen` with an explicit wait, because `run` does not expose
the process on timeout.

Not yet done, because it cannot be tested meaningfully on Windows, which has no
process groups in that sense. Do it as part of the port, and verify with:

```bash
# start a turn, kill it mid-flight, then confirm nothing survives
ps -eo pid,ppid,cmd | grep -E 'codex|node' | grep -v grep
```

If you keep `subprocess.run`, at least add an `init: true` to the compose
service or `--init` to `docker run`, so PID 1 reaps orphans rather than
accumulating zombies. That is a mitigation, not a fix.

---

## 6. Regenerating the corpus without Word

`tools/build_corpus.py` generates the 48 resumes with PyMuPDF and runs fine on
Linux. The **position description** is the exception: `position-description.pdf`
was produced from `position-description.md` using Word COM automation on the
workstation.

The PDF is committed, so a deployment does not need to regenerate it. You only
need this if the description changes.

Options, best first:

1. **Render it with PyMuPDF**, the way the resumes already are — extend
   `build_corpus.py` with a Markdown-to-PDF pass. Removes the last non-portable
   step and makes the whole corpus reproducible from one command.
2. **LibreOffice headless**, if you want to keep Word-like fidelity:
   ```bash
   soffice --headless --convert-to pdf position-description.md --outdir .
   ```
   Adds ~400 MB to the image; only worth it if the image already has it.
3. **Regenerate on the workstation and commit the result.** Fine as a stopgap,
   but it means the corpus cannot be rebuilt from a clean Linux checkout.

Note that the app itself reads `.docx` natively (`extraction.py`), so a user
uploading a Word position description through the UI needs none of this. This
is only about the committed corpus fixture.

---

## 7. Verification after the port

Run these in order. Each one fails distinctly, so stop at the first failure
rather than reading past it.

```bash
# 1. The app boots and the schema creates
curl -fsS localhost:8000/api/config | head -c 200

# 2. The corpus is where the image says it is — this was wrong once already
curl -fsS localhost:8000/api/audits/corpus \
  | python -c "import json,sys; print(json.load(sys.stdin)['corpora'])"
# expect swe_ii_corpus, 48 resumes, 36 pairs

# 3. The provider is reachable from inside the container
curl -fsS -X POST 'localhost:8000/api/config/test?provider=ulproxy'

# 4. The deterministic half still works
python -m pytest -q          # 170 tests, no network

# 5. Guided chat — exercises the tools as subprocesses
curl -fsS -X POST localhost:8000/api/screenings/<id>/chat/1 \
  -H 'content-type: application/json' \
  -d '{"question":"Why did so many candidates pass this stage?"}' \
  | python -c "import json,sys; d=json.load(sys.stdin); print(d['mode'], d['trace'])"
# expect mode=structured and a non-empty trace

# 6. Independent chat — only if a CLI is installed
# same request with chat mode set to harness; expect:
#   "mode": "harness", "degradedFrom": ""
# a non-empty degradedFrom names the exact failure — read it, do not guess
```

Step 6 is the one that will need iterating. Every failure it produces is
self-describing: `not installed or not on PATH`, an auth message from the CLI,
or a timeout. The degradation is deliberate — a broken harness never costs a
user their answer, it just answers in Guided mode and says so.

---

## 8. Environment variables

Secrets belong in the platform's secret store, injected as environment
variables. The app also accepts provider connection details entered on the
Configuration page, which are stored in the database and **override** the
environment — see `CLAUDE.md`. For a deployment, prefer the environment: it is
auditable and does not put a token in the data volume.

| Variable | Purpose | Notes for the container |
|---|---|---|
| `ULMAIPROXY_BASE_URL`, `ULMAIPROXY_AUTH_TOKEN` | UL AI proxy | Also reaches the harness via `harness_env()` |
| `LLM_GATEWAY_URL`, `LLM_GATEWAY_KEY` | SIS fastLLM gateway | |
| `HL_PROJECT_ID`, `HL_REQUESTER_ID` | Gateway attribution headers | Not available to a CLI harness — see §4 |
| `LLM_MODEL`, `LLM_SEED` | Gateway model and pinned seed | Seed matters for audit reproducibility |
| `LLM_MAX_TOKENS` | Output cap | Unset means no cap; the gateway's own default applies |
| `SCREENING_DB` | SQLite path | Must be on the persistent volume |
| `CHAT_WORKSPACE_ROOT` | Per-session scratch | Volume too, if harness continuity matters |
| `AUDIT_CORPUS_ROOT`, `AUDIT_CORPUS_DIR` | Corpus location and default | Baked into the image; only override if you mount a corpus |
| `BATCH_SIZE`, `BATCH_DELAY_SECONDS` | Evaluation batching | Tune to the gateway's rate limits, not the container's |
| `LANG` | Locale | Set `C.UTF-8` — see §2 |

---

## 9. Reference Dockerfile

The committed `Dockerfile` is the no-harness version and is correct as it
stands. This is what it looks like with codex, a non-root user, and the locale
pinned. Treat it as a starting point, not a drop-in.

```dockerfile
# Stage 1 — build the frontend
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npx vite build

# Stage 2 — runtime
FROM python:3.12-slim
WORKDIR /app

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Node is needed at runtime only for the codex harness. Drop this whole layer
# if you are deploying without Independent chat mode.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @openai/codex \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY test-resumes/ ./test-resumes/
COPY --from=frontend-build /app/dist ./dist

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/data \
 && chown -R app:app /app/data

ENV SCREENING_DB=/app/data/screening.db \
    CHAT_WORKSPACE_ROOT=/app/data/chat \
    AUDIT_CORPUS_ROOT=/app/test-resumes \
    AUDIT_CORPUS_DIR=/app/test-resumes/swe_ii_corpus \
    HOME=/home/app \
    CODEX_HOME=/home/app/.codex

USER app
EXPOSE 8000

# One worker. See §1.1 — this is a correctness constraint.
CMD ["uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker-compose.yaml
services:
  app:
    build: .
    init: true                 # reap orphaned harness children — see §5
    ports: ["8000:8000"]
    env_file: [.env]
    volumes:
      - screening-data:/app/data
    restart: unless-stopped
    deploy:
      replicas: 1              # see §1.1

volumes:
  screening-data:
```

---

## 10. Open questions to settle during the port

Things this document cannot answer from the workstation.

1. **Does the injected environment actually authenticate the CLI?** (§4) The
   first thing to test, because everything else about Independent mode depends
   on it.
2. **Does the SIS gateway require its `hl-*` headers?** If yes, `codex_cli`
   against fastLLM cannot work and that combination should be disabled in the
   UI rather than left to degrade.
3. **How long may a request run at your ingress?** (§1.3) Determines whether
   chat stays synchronous.
4. **Is a persistent volume available?** Without one, screenings and stage
   decisions are lost on every restart — the app is stateful by design.
5. **Who may reach the app?** It has **no authentication of its own**. Anyone
   who can load the page can screen candidates, read resumes, and use the
   configured model. It needs to sit behind whatever SSO or network boundary
   the institution uses.
