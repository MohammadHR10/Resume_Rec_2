# Stage 1 — build the frontend
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npx vite build

# Stage 2 — Python runtime serving the API and the built frontend
FROM python:3.12-slim
WORKDIR /app

# The chat harness adapters shell out to the claude/codex CLIs when chat is in
# harness mode. They are optional: without them a harness turn degrades to the
# structured loop, which needs nothing beyond Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY test-resumes/ ./test-resumes/
COPY --from=frontend-build /app/dist ./dist

# SQLite and the per-session chat workspaces; mount a volume over this to keep
# screenings across container rebuilds.
RUN mkdir -p /app/data
ENV SCREENING_DB=/app/data/screening.db \
    CHAT_WORKSPACE_ROOT=/app/data/chat \
    AUDIT_CORPUS_DIR=/app/test-resumes/swe_ii_corpus \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]
