"""Resume/JD parsing and rubric generation.

This is intentionally deterministic and offline: it extracts text from a
plain-text/Markdown file (or PDF when pypdf is installed), identifies skill
mentions from a small taxonomy, and builds an editable competency rubric with
evidence prompts. It does not call an LLM, so it cannot hallucinate claims.

The existing demo evidence graph remains fixed; this module is the seam for a
job-description-driven competency library.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Competency:
    name: str
    description: str
    evidence_prompts: tuple[str, ...]


@dataclass
class ParsedDocument:
    text: str
    kind: str
    path: str | None = None
    skills: list[str] = field(default_factory=list)


@dataclass
class Rubric:
    competencies: list[Competency]
    resume_claims: list[str]

    def to_dict(self) -> dict:
        return {
            "competencies": [
                {
                    "name": c.name,
                    "description": c.description,
                    "evidence_prompts": list(c.evidence_prompts),
                }
                for c in self.competencies
            ],
            "resume_claims": self.resume_claims,
        }


# Small, transparent taxonomy. A production deployment can replace this with a
# customer-specific competency library without changing the rubric shape.
SKILL_TAXONOMY: dict[str, tuple[str, tuple[str, ...]]] = {
    "rag_architecture": (
        "Retrieval-augmented generation architecture",
        (
            "How retrieval is designed and what is fetched vs. generated",
            "How irrelevant or low-quality retrieved documents are handled",
            "How the system behaves under a large traffic/latency increase",
        ),
    ),
    "distributed_systems": (
        "Distributed systems and reliability",
        (
            "How partial failures, retries and idempotency are handled",
            "How consistency and ordering are reasoned about",
            "How a production incident is diagnosed",
        ),
    ),
    "data_engineering": (
        "Data engineering and pipelines",
        (
            "How ingestion, schema evolution and backfills work",
            "How data quality is measured and enforced",
            "How lineage and cost are tracked",
        ),
    ),
    "ml_engineering": (
        "Machine learning engineering",
        (
            "How models are evaluated and monitored",
            "How training/serving skew is detected",
            "How rollback and reproducibility work",
        ),
    ),
    "security": (
        "Application and data security",
        (
            "How authentication/authorization is enforced",
            "How secrets and PII are handled",
            "How abuse and injection risks are mitigated",
        ),
    ),
    "frontend": (
        "Frontend and web performance",
        (
            "How Core Web Vitals are measured and improved",
            "How state and rendering performance are managed",
            "How accessibility is verified",
        ),
    ),
}

_KEYWORDS = {
    "rag_architecture": ("rag", "retrieval", "vector", "embedding", "rerank"),
    "distributed_systems": (
        "distributed",
        "kafka",
        "queue",
        "consistency",
        "kubernetes",
        "microservice",
    ),
    "data_engineering": (
        "etl",
        "elt",
        "airflow",
        "dbt",
        "spark",
        "warehouse",
        "pipeline",
    ),
    "ml_engineering": (
        "pytorch",
        "tensorflow",
        "model",
        "training",
        "mlops",
        "inference",
    ),
    "security": ("oauth", "auth", "security", "encryption", "pii", "gdpr"),
    "frontend": (
        "react",
        "typescript",
        "javascript",
        "frontend",
        "web performance",
        "accessibility",
    ),
}


def extract_text(path: str | Path) -> str:
    """Extract text from txt/md or PDF (pypdf optional)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".rst"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "PDF parsing requires pypdf. Install it with `pip install pypdf`."
            ) from exc
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError(f"unsupported document type: {suffix or '(none)'}")


def parse_document(text_or_path: str | Path, kind: str = "resume") -> ParsedDocument:
    if isinstance(text_or_path, Path) or (
        isinstance(text_or_path, str) and Path(text_or_path).exists()
    ):
        path = Path(text_or_path)
        text = extract_text(path)
        path_value: str | None = str(path)
    else:
        text = str(text_or_path)
        path_value = None
    skills = detect_skills(text)
    return ParsedDocument(text=text, kind=kind, path=path_value, skills=skills)


def detect_skills(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for competency, keywords in _KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(keyword)}", lowered) for keyword in keywords):
            found.append(competency)
    return found


def build_rubric(
    jd_text: str,
    resume_text: str,
    *,
    max_competencies: int = 4,
) -> Rubric:
    """Build a deterministic rubric from JD skills, grounded in resume claims."""
    jd_skills = detect_skills(jd_text)
    resume_skills = detect_skills(resume_text)
    ordered = [skill for skill in jd_skills if skill in SKILL_TAXONOMY]
    for skill in resume_skills:
        if skill not in ordered and skill in SKILL_TAXONOMY:
            ordered.append(skill)
    ordered = ordered[: max(1, max_competencies)]

    competencies = [
        Competency(
            name=skill,
            description=SKILL_TAXONOMY[skill][0],
            evidence_prompts=SKILL_TAXONOMY[skill][1],
        )
        for skill in ordered
    ]
    claims = _resume_claims(resume_text, ordered)
    return Rubric(competencies=competencies, resume_claims=claims)


def _resume_claims(resume_text: str, skills: Iterable[str]) -> list[str]:
    claims: list[str] = []
    sentences = re.split(r"(?<=[.!?])\s+", resume_text.replace("\n", " "))
    for skill in skills:
        keywords = _KEYWORDS.get(skill, ())
        for sentence in sentences:
            lowered = sentence.lower()
            if any(keyword in lowered for keyword in keywords):
                cleaned = sentence.strip()
                if cleaned and cleaned not in claims:
                    claims.append(cleaned[:500])
                    break
    return claims


__all__ = [
    "Competency",
    "ParsedDocument",
    "Rubric",
    "build_rubric",
    "detect_skills",
    "extract_text",
    "parse_document",
]
