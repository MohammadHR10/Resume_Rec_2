"""End-to-end API: JD intake → qualifications → resumes → evaluation → stages → export."""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db, server
from backend.llm import registry

CORPUS = Path(__file__).resolve().parent.parent / "test-resumes" / "SWE_pdf"

JD_TEXT = """\
Software Engineer II

Required Qualifications
- Bachelor's degree in Computer Science
- Three years of professional Python experience

Preferred Qualifications
- Experience with distributed systems
"""


class StubProvider:
    """A provider that answers from canned data, so the API can be tested whole."""

    label = "Stub"

    def __init__(self, verdict: str = "Meets"):
        self.verdict = verdict
        self.chat_calls: list[dict] = []

    def is_configured(self) -> bool:
        return True

    def health(self) -> dict:
        return {"ok": True, "detail": "stub"}

    def list_models(self) -> list[str]:
        return ["stub-model"]

    def structured_extract(self, prompt, schema, *, model=None, system=None, schema_name="Response"):
        if schema_name == "ParsedQualifications":
            return {
                "job_title": "Software Engineer II",
                "required": [
                    {"text": "Bachelor's degree in Computer Science"},
                    {"text": "Three years of professional Python experience"},
                ],
                "preferred": [{"text": "Experience with distributed systems"}],
            }
        labels = schema["properties"]["verdicts"]["items"]["properties"]["qual_id"]["enum"]
        return {
            "candidate_name": "",
            "summary": "Stubbed evaluation.",
            "verdicts": [
                {"qual_id": label, "verdict": self.verdict, "evidence": f"evidence for {label}"}
                for label in labels
            ],
        }

    def chat(self, messages, *, schema=None, model=None, system=None, schema_name="Response"):
        self.chat_calls.append({"messages": messages, "system": system})
        if len(self.chat_calls) == 1:
            return {
                "reasoning": "look at the stage",
                "tool_calls": [{"tool": "stage_stats", "args_json": '{"stage": "1"}'}],
                "answer": "",
            }
        return {"reasoning": "done", "tool_calls": [], "answer": "Everyone met every requirement."}


@pytest.fixture
def stub(monkeypatch):
    provider = StubProvider()
    monkeypatch.setattr(server.registry, "active", lambda: (provider, "ulproxy", "stub-model"))
    from backend.chat import session as chat_session

    monkeypatch.setattr(chat_session.registry, "active", lambda: (provider, "ulproxy", "stub-model"))
    monkeypatch.setattr(chat_session.registry, "chat_mode", lambda *_a: "structured")
    return provider


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/jobs/{job_id}/progress").json()
        if payload["status"] in ("done", "error"):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

def test_app_boots_and_creates_the_schema(client):
    assert db.DB_PATH.exists()
    tables = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"screening", "qualification", "candidate", "evaluation", "stage_state"} <= tables


def test_config_round_trips(client):
    initial = client.get("/api/config").json()
    assert [p["name"] for p in initial["providers"]] == ["ulproxy", "fastllm"]

    updated = client.put(
        "/api/config", json={"provider": "fastllm", "model": "mistral-small", "chatMode": "structured"}
    ).json()
    assert updated["provider"] == "fastllm"
    assert updated["model"] == "mistral-small"
    assert updated["chatModes"]["fastllm:mistral-small"] == "structured"


def test_config_rejects_an_unknown_provider(client):
    assert client.put("/api/config", json={"provider": "nope"}).status_code == 400


# ---------------------------------------------------------------------------
# Provider connections entered in the UI
# ---------------------------------------------------------------------------

def _connection(payload: dict, provider: str) -> dict:
    return next(c for c in payload["connections"] if c["provider"] == provider)


def test_a_connection_saved_in_the_ui_is_used_for_calls(client):
    client.put(
        "/api/config",
        json={
            "connection": {
                "provider": "fastllm",
                "baseUrl": "https://gateway.example/v1",
                "apiKey": "gw-secret-token-1234",
                "projectId": "proj-7",
                "requesterId": "dastewart",
            }
        },
    )
    provider = registry.get_provider("fastllm")
    assert provider.base_url == "https://gateway.example/v1"
    assert provider.api_key == "gw-secret-token-1234"
    assert provider.extra_headers() == {"hl-project-id": "proj-7", "hl-requester-id": "dastewart"}
    assert provider.is_configured() is True


def test_the_token_is_never_returned_to_the_browser(client):
    client.put(
        "/api/config",
        json={
            "connection": {
                "provider": "fastllm",
                "baseUrl": "https://gateway.example/v1",
                "apiKey": "gw-secret-token-1234",
            }
        },
    )
    response = client.get("/api/config")
    assert "gw-secret-token-1234" not in response.text

    card = _connection(response.json(), "fastllm")
    assert card["hasKey"] is True
    assert card["keyHint"] == "••••••••1234"
    assert card["keySource"] == "config"


def test_a_blank_token_keeps_the_stored_one(client):
    base = {"provider": "fastllm", "baseUrl": "https://gateway.example/v1"}
    client.put("/api/config", json={"connection": {**base, "apiKey": "original-token"}})
    # The UI submits the form again without re-typing the token.
    client.put("/api/config", json={"connection": {**base, "baseUrl": "https://moved.example/v1"}})

    provider = registry.get_provider("fastllm")
    assert provider.api_key == "original-token"
    assert provider.base_url == "https://moved.example/v1"


def test_an_empty_string_clears_the_stored_token(client):
    base = {"provider": "fastllm", "baseUrl": "https://gateway.example/v1"}
    client.put("/api/config", json={"connection": {**base, "apiKey": "original-token"}})
    client.put("/api/config", json={"connection": {**base, "apiKey": ""}})
    assert registry.get_provider("fastllm").api_key == ""


def test_a_stored_connection_overrides_the_environment(client, monkeypatch):
    monkeypatch.setenv("ULMAIPROXY_BASE_URL", "https://from-env.example")
    assert _connection(client.get("/api/config").json(), "ulproxy")["baseUrlSource"] == "environment"

    client.put(
        "/api/config",
        json={"connection": {"provider": "ulproxy", "baseUrl": "https://from-ui.example"}},
    )
    card = _connection(client.get("/api/config").json(), "ulproxy")
    assert card["baseUrl"] == "https://from-ui.example"
    assert card["baseUrlSource"] == "config"


def test_the_environment_still_works_when_nothing_is_stored(client):
    card = _connection(client.get("/api/config").json(), "ulproxy")
    assert card["baseUrl"] == "https://proxy.example"
    assert card["baseUrlSource"] == "environment"
    assert card["keySource"] == "environment"


def test_only_the_gateway_offers_attribution_fields(client):
    payload = client.get("/api/config").json()
    assert _connection(payload, "fastllm")["supportsAttribution"] is True
    assert _connection(payload, "ulproxy")["supportsAttribution"] is False


def test_a_connection_for_an_unknown_provider_is_rejected(client):
    response = client.put(
        "/api/config", json={"connection": {"provider": "nope", "baseUrl": "https://x"}}
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# JD intake
# ---------------------------------------------------------------------------

def test_parse_jd_produces_two_editable_lists(client, stub):
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": ("jd.txt", JD_TEXT.encode(), "text/plain")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["jobTitle"] == "Software Engineer II"
    labels = [q["label"] for q in payload["qualifications"]]
    assert labels == ["R1", "R2", "P1"]


JD_DOCX = (
    Path(__file__).resolve().parent.parent
    / "test-resumes"
    / "2025 JD - University Lands - Machine Learning & LLM Integration Intern.docx"
)


def test_parse_jd_reads_a_word_document(client, stub):
    """HR sends position descriptions as .docx; feeding those bytes to a UTF-8
    decode produced mojibake and an empty checklist that looked like success."""
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": (JD_DOCX.name, JD_DOCX.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["qualifications"]) == 3

    stored = db.query_one("SELECT jd_text FROM screening WHERE id=?", (screening_id,))["jd_text"]
    assert "Proficiency in Python" in stored
    assert "�" not in stored


def test_parse_jd_rejects_an_unsupported_file_type(client, stub):
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": ("jd.rtf", b"{\\rtf1\\ansi some text}", "application/rtf")},
    )
    assert response.status_code == 400
    assert ".pdf" in response.json()["detail"]


def test_parse_jd_tells_a_doc_user_what_to_do(client, stub):
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": ("jd.doc", b"\xd0\xcf\x11\xe0legacy word", "application/msword")},
    )
    assert response.status_code == 400
    assert "Save the .doc as .docx" in response.json()["detail"]


def test_parse_jd_rejects_binary_masquerading_as_text(client, stub):
    """A file whose extension lies should be refused, not handed to the model."""
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": ("jd.txt", JD_DOCX.read_bytes(), "text/plain")},
    )
    assert response.status_code == 400
    assert "readable text" in response.json()["detail"]


def test_a_parse_that_finds_nothing_is_not_reported_as_success(client, monkeypatch):
    class EmptyProvider(StubProvider):
        def structured_extract(self, prompt, schema, *, model=None, system=None, schema_name="Response"):
            if schema_name == "ParsedQualifications":
                return {"job_title": "ML Intern", "required": [], "preferred": []}
            return super().structured_extract(prompt, schema, model=model, system=system, schema_name=schema_name)

    monkeypatch.setattr(server.registry, "active", lambda: (EmptyProvider(), "ulproxy", "stub-model"))
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": ("jd.txt", JD_TEXT.encode(), "text/plain")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["qualifications"] == []
    assert "no qualifications could be identified" in payload["warning"]


def test_a_successful_parse_carries_no_warning(client, stub):
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": ("jd.txt", JD_TEXT.encode(), "text/plain")},
    )
    assert response.json()["warning"] == ""


def test_parse_jd_rejects_a_file_with_no_text(client, stub):
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": ("empty.txt", b"   ", "text/plain")},
    )
    assert response.status_code == 400


def test_qualifications_can_be_reworded_reordered_and_moved_between_lists(client, stub):
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    client.post(
        f"/api/screenings/{screening_id}/parse-jd",
        files={"file": ("jd.txt", JD_TEXT.encode(), "text/plain")},
    )
    response = client.put(
        f"/api/screenings/{screening_id}/qualifications",
        json={
            "qualifications": [
                {"text": "Experience with distributed systems", "kind": "required"},
                {"text": "Bachelor's degree in Computer Science or equivalent", "kind": "required"},
                {"text": "Three years of professional Python experience", "kind": "preferred"},
            ],
            "confirmed": True,
        },
    )
    payload = response.json()
    assert payload["confirmed"] is True
    assert [q["label"] for q in payload["qualifications"]] == ["R1", "R2", "P1"]
    assert payload["qualifications"][0]["text"] == "Experience with distributed systems"
    assert payload["qualifications"][2]["kind"] == "preferred"


# ---------------------------------------------------------------------------
# The full funnel
# ---------------------------------------------------------------------------

def _seed_screening(client: TestClient) -> str:
    screening_id = client.post("/api/screenings", json={"jobTitle": "SWE II"}).json()["id"]
    client.put(
        f"/api/screenings/{screening_id}/qualifications",
        json={
            "qualifications": [
                {"text": "Bachelor's degree in Computer Science", "kind": "required"},
                {"text": "Three years of Python", "kind": "required"},
                {"text": "Distributed systems", "kind": "preferred"},
            ],
            "confirmed": True,
        },
    )
    return screening_id


def _upload_resumes(client: TestClient, screening_id: str, names: list[str]):
    files = [
        ("files", (name, (CORPUS / name).read_bytes(), "application/pdf")) for name in names
    ]
    return client.post(f"/api/screenings/{screening_id}/candidates", files=files, data={"replace": "true"})


def test_evaluation_requires_a_confirmed_checklist(client, stub):
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post(f"/api/screenings/{screening_id}/evaluate")
    assert response.status_code == 400
    assert "Confirm" in response.json()["detail"]


def test_evaluation_requires_at_least_one_resume(client, stub):
    screening_id = _seed_screening(client)
    response = client.post(f"/api/screenings/{screening_id}/evaluate")
    assert response.status_code == 400
    assert "resume" in response.json()["detail"]


def test_resume_upload_extracts_text_from_pdfs(client, stub):
    screening_id = _seed_screening(client)
    payload = _upload_resumes(
        client, screening_id, ["Resume_1_Ayaan_Rahman.pdf", "Resume_2_Jordan_Lee.pdf"]
    ).json()
    assert payload["added"] == 2
    assert payload["skipped"] == []
    rows = db.query("SELECT name, resume_text FROM candidate WHERE screening_id=?", (screening_id,))
    assert {r["name"] for r in rows} == {"Ayaan Rahman", "Jordan Lee"}
    assert all(len(r["resume_text"]) > 100 for r in rows)


def test_a_zip_upload_is_flattened_into_its_pdfs(client, stub):
    screening_id = _seed_screening(client)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ["Resume_1_Ayaan_Rahman.pdf", "Resume_3_Rohan_Mehta.pdf"]:
            archive.writestr(f"batch/{name}", (CORPUS / name).read_bytes())
    response = client.post(
        f"/api/screenings/{screening_id}/candidates",
        files=[("files", ("batch.zip", buffer.getvalue(), "application/zip"))],
    )
    assert response.json()["added"] == 2


def test_full_funnel_evaluate_promote_and_export(client, stub):
    screening_id = _seed_screening(client)
    _upload_resumes(
        client,
        screening_id,
        ["Resume_1_Ayaan_Rahman.pdf", "Resume_2_Jordan_Lee.pdf", "Resume_3_Rohan_Mehta.pdf"],
    )

    started = client.post(f"/api/screenings/{screening_id}/evaluate").json()
    assert started["candidates"] == 3
    progress = _wait_for_job(client, started["jobId"])
    assert progress["status"] == "done", progress

    stage1 = client.get(f"/api/screenings/{screening_id}/stages/1").json()
    assert len(stage1["candidates"]) == 3
    assert stage1["stageCounts"]["1"] == 3
    first = stage1["candidates"][0]
    assert first["required_met"] == 2 and first["required_total"] == 2
    assert first["ai_pass"] is True
    assert first["rank"] == 1
    assert len(first["verdicts"]) == 3

    # Nothing advances on its own.
    assert client.get(f"/api/screenings/{screening_id}/stages/2").json()["candidates"] == []

    ids = [c["id"] for c in stage1["candidates"][:2]]
    moved = client.post(
        f"/api/screenings/{screening_id}/stages/1/actions",
        json={"candidateIds": ids, "action": "promote"},
    ).json()
    assert [m["to"] for m in moved["moved"]] == ["2", "2"]
    assert moved["stageCounts"] == {"1": 1, "2": 2, "3": 0, "rejected": 0}

    rejected = client.post(
        f"/api/screenings/{screening_id}/stages/1/actions",
        json={"candidateIds": [stage1["candidates"][2]["id"]], "action": "reject", "note": "not a fit"},
    ).json()
    # The AI passed this candidate, so a rejection is recorded as an override.
    assert rejected["moved"][0]["override"] is True

    trail = client.get(f"/api/screenings/{screening_id}/audit-trail").json()
    assert len(trail) == 3
    assert {t["action"] for t in trail} == {"promote", "reject"}
    assert any(t["note"] == "not a fit" for t in trail)

    excel = client.get(f"/api/screenings/{screening_id}/stages/2/export")
    assert excel.status_code == 200
    assert excel.content[:2] == b"PK"
    assert "stage2.xlsx" in excel.headers["content-disposition"]

    csv = client.get(f"/api/screenings/{screening_id}/stages/2/export?format=csv")
    assert csv.status_code == 200
    assert "Candidate" in csv.text.splitlines()[0]
    assert len(csv.text.strip().splitlines()) == 3


def test_promoting_a_candidate_the_model_failed_is_recorded_as_an_override(client, monkeypatch):
    provider = StubProvider(verdict="No")
    monkeypatch.setattr(server.registry, "active", lambda: (provider, "ulproxy", "stub-model"))
    screening_id = _seed_screening(client)
    _upload_resumes(client, screening_id, ["Resume_1_Ayaan_Rahman.pdf"])
    started = client.post(f"/api/screenings/{screening_id}/evaluate").json()
    _wait_for_job(client, started["jobId"])

    stage1 = client.get(f"/api/screenings/{screening_id}/stages/1").json()
    assert stage1["candidates"][0]["ai_pass"] is False

    moved = client.post(
        f"/api/screenings/{screening_id}/stages/1/actions",
        json={"candidateIds": [stage1["candidates"][0]["id"]], "action": "promote"},
    ).json()
    assert moved["moved"][0]["override"] is True


def test_editing_the_checklist_after_evaluating_clears_stale_verdicts(client, stub):
    screening_id = _seed_screening(client)
    _upload_resumes(client, screening_id, ["Resume_1_Ayaan_Rahman.pdf"])
    _wait_for_job(client, client.post(f"/api/screenings/{screening_id}/evaluate").json()["jobId"])
    assert db.query_one("SELECT COUNT(*) AS n FROM evaluation")["n"] == 1

    client.put(
        f"/api/screenings/{screening_id}/qualifications",
        json={"qualifications": [{"text": "A brand new requirement", "kind": "required"}], "confirmed": True},
    )
    assert db.query_one("SELECT COUNT(*) AS n FROM evaluation")["n"] == 0


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def test_structured_chat_runs_a_real_tool_and_answers(client, stub):
    screening_id = _seed_screening(client)
    _upload_resumes(client, screening_id, ["Resume_1_Ayaan_Rahman.pdf", "Resume_2_Jordan_Lee.pdf"])
    _wait_for_job(client, client.post(f"/api/screenings/{screening_id}/evaluate").json()["jobId"])

    opened = client.get(f"/api/screenings/{screening_id}/chat/1").json()
    assert opened["messages"] == []

    response = client.post(
        f"/api/screenings/{screening_id}/chat/1", json={"question": "why did so many pass?"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "structured"
    assert payload["answer"] == "Everyone met every requirement."
    assert payload["trace"][0]["tool"] == "stage_stats"

    transcript = client.get(f"/api/screenings/{screening_id}/chat/1").json()["messages"]
    assert [m["role"] for m in transcript] == ["user", "assistant"]


def test_chat_rejects_an_empty_question(client, stub):
    screening_id = _seed_screening(client)
    response = client.post(f"/api/screenings/{screening_id}/chat/1", json={"question": "  "})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Audit endpoints
# ---------------------------------------------------------------------------

def test_corpus_endpoint_lists_every_corpus(client):
    corpora = {c["name"]: c for c in client.get("/api/audits/corpus").json()["corpora"]}
    assert {"swe_ii_corpus", "SWE_pdf"} <= set(corpora)

    old = corpora["SWE_pdf"]
    assert old["files"] == 34 and old["pairs"] == 24
    assert {a["attribute"] for a in old["byAttribute"]} == {"G", "R", "RA", "control"}

    new = corpora["swe_ii_corpus"]
    assert new["isDefault"] is True
    assert new["skillLevels"] == {"senior": 4, "junior": 4, "unqualified": 4}


def test_a_screening_can_be_built_from_a_corpus_without_any_upload(client, stub):
    """A corpus ships the position description its resumes were written
    against; making someone drag those same files into a browser is busywork."""
    payload = client.post("/api/audits/corpus/swe_ii_corpus/screening").json()

    assert payload["candidates"] == 24
    assert payload["qualifications"]
    rows = db.query(
        "SELECT name, source_files FROM candidate WHERE screening_id=?", (payload["screeningId"],)
    )
    files = {json.loads(r["source_files"])[0] for r in rows}
    assert "position-description.pdf" not in files, "the JD is not a candidate"
    assert "Jordan_Avery_senior.pdf" in files


def test_building_a_screening_from_an_unknown_corpus_is_refused(client, stub):
    # Path traversal is covered at the resolver in test_audit.py.
    assert client.post("/api/audits/corpus/nope/screening").status_code == 400


def test_an_audit_refuses_a_corpus_with_nothing_to_compare(client, stub, monkeypatch):
    """A pass from a corpus with no variants would be a clean bill of health
    from a test that never ran."""
    monkeypatch.setattr(server.audit, "load_pairs", lambda _dir: ([], [], []))
    screening_id = _seed_screening(client)
    response = client.post(
        "/api/audits", json={"screeningId": screening_id, "corpus": "swe_ii_corpus"}
    )
    assert response.status_code == 400
    assert "no baseline/variant pairs" in response.json()["detail"]


def test_an_audit_rejects_a_corpus_outside_the_corpus_root(client, stub):
    screening_id = _seed_screening(client)
    response = client.post(
        "/api/audits", json={"screeningId": screening_id, "corpus": "../backend"}
    )
    assert response.status_code == 400


def test_an_audit_can_name_its_own_model(client, stub, monkeypatch):
    """The audit page benchmarks models, so a run must not disturb — or be
    disturbed by — whichever model is active for screening."""
    started: dict = {}

    async def capture(_job, _audit, _screening, _provider, provider_name, model, *_a, **_k):
        started.update(provider=provider_name, model=model)

    monkeypatch.setattr(server.audit, "run_audit", capture)
    screening_id = _seed_screening(client)

    response = client.post(
        "/api/audits",
        json={"screeningId": screening_id, "provider": "ulproxy", "model": "gpt-5.4-mini",
              "corpus": "SWE_pdf"},
    )
    assert response.status_code == 200
    assert response.json()["model"] == "gpt-5.4-mini"

    # The globally active selection is untouched.
    assert client.get("/api/config").json()["model"] != "gpt-5.4-mini"


def test_an_audit_falls_back_to_the_active_model(client, stub, monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(server.audit, "run_audit", noop)
    screening_id = _seed_screening(client)
    response = client.post(
        "/api/audits", json={"screeningId": screening_id, "corpus": "SWE_pdf"}
    )
    assert response.json()["model"] == "stub-model"


def test_an_audit_rejects_an_unknown_provider(client, stub):
    screening_id = _seed_screening(client)
    response = client.post(
        "/api/audits",
        json={"screeningId": screening_id, "provider": "nope", "model": "x", "corpus": "SWE_pdf"},
    )
    assert response.status_code == 400


def test_audit_requires_a_screening_with_qualifications(client, stub):
    screening_id = client.post("/api/screenings", json={}).json()["id"]
    response = client.post("/api/audits", json={"screeningId": screening_id})
    assert response.status_code == 400
