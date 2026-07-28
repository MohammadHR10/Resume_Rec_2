"""Test fixtures.

``SCREENING_DB`` has to be set before ``backend.db`` is imported, because the
path is read at module import — hence the environment mutation at conftest
import time rather than in a fixture.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="screening_tests_"))
os.environ["SCREENING_DB"] = str(_TMP / "test.db")
os.environ["CHAT_WORKSPACE_ROOT"] = str(_TMP / "chat")
os.environ.setdefault("ULMAIPROXY_BASE_URL", "https://proxy.example")
os.environ.setdefault("ULMAIPROXY_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402

from backend import db  # noqa: E402

TABLES = [
    "chat_action",
    "chat_message",
    "chat_session",
    "stage_action",
    "stage_state",
    "evaluation",
    "candidate",
    "qualification",
    "audit_run",
    "screening",
    "config",
]


@pytest.fixture(autouse=True)
def clean_database():
    db.init_db()
    with db.cursor() as cur:
        for table in TABLES:
            cur.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture
def screening_id() -> str:
    screening = db.new_id()
    db.execute(
        "INSERT INTO screening (id, job_title, quals_confirmed, created_at) VALUES (?,?,?,?)",
        (screening, "Software Engineer II", 1, db.now()),
    )
    return screening


@pytest.fixture
def quals(screening_id: str) -> list[dict]:
    rows = [
        ("Bachelor's degree in Computer Science", "required", 0),
        ("Three years of professional Python experience", "required", 1),
        ("Experience with distributed systems", "preferred", 0),
    ]
    out = []
    for text, kind, position in rows:
        qual_id = db.new_id()
        db.execute(
            "INSERT INTO qualification (id, screening_id, text, kind, position) VALUES (?,?,?,?,?)",
            (qual_id, screening_id, text, kind, position),
        )
        out.append({"id": qual_id, "text": text, "kind": kind, "position": position})
    return out
