"""Resume claim seeding for the built-in templates."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ResumeClaim:
    competency: str
    text: str


SEED_CLAIMS: dict[str, ResumeClaim] = {
    "rag_architecture": ResumeClaim(
        competency="rag_architecture",
        text="Designed an enterprise RAG platform using Azure OpenAI, Azure AI Search and AKS.",
    ),
    "distributed_systems": ResumeClaim(
        competency="distributed_systems",
        text="Led a Kafka-based distributed ingestion platform with retries and idempotent writes.",
    ),
    "data_engineering": ResumeClaim(
        competency="data_engineering",
        text="Built an Airflow and dbt data platform with quality checks and schema evolution.",
    ),
}

SEED_CLAIM = SEED_CLAIMS["rag_architecture"]


def seed_claim(competency: str | None = None) -> ResumeClaim:
    if competency and competency in SEED_CLAIMS:
        return SEED_CLAIMS[competency]
    return SEED_CLAIM


__all__ = ["SEED_CLAIM", "SEED_CLAIMS", "ResumeClaim", "seed_claim"]
