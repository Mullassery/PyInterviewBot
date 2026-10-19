"""Resume/JD parsing and rubric tests (offline, deterministic)."""

from __future__ import annotations

from pyinterviewbot_ai.documents import build_rubric, detect_skills, parse_document

RESUME = """
Built an enterprise RAG platform with Azure AI Search and vector reranking.
Led a Kafka-based distributed ingestion pipeline with idempotent retries.
Implemented OAuth authentication and PII redaction for customer data.
"""

JD = """
We need a senior engineer with RAG, retrieval and embedding experience.
You will own data pipelines, Airflow and dbt models, and production reliability.
Experience with security, OAuth and PII handling is required.
"""


def test_detect_skills():
    skills = detect_skills(RESUME)
    assert "rag_architecture" in skills
    assert "distributed_systems" in skills
    assert "security" in skills


def test_build_rubric_is_grounded_and_ordered_by_jd():
    rubric = build_rubric(JD, RESUME, max_competencies=3)
    names = [c.name for c in rubric.competencies]
    assert names[0] == "rag_architecture"
    assert "data_engineering" in names
    assert rubric.resume_claims
    assert any("RAG" in claim for claim in rubric.resume_claims)


def test_parse_document_text():
    parsed = parse_document(RESUME, kind="resume")
    assert parsed.kind == "resume"
    assert "rag_architecture" in parsed.skills
    assert parsed.path is None


def test_parse_markdown_file(tmp_path):
    path = tmp_path / "resume.md"
    path.write_text(RESUME)
    parsed = parse_document(path)
    assert parsed.path == str(path)
    assert parsed.text == RESUME
