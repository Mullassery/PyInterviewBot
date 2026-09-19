"""Resume claim seeding.

This MVP slice does not implement resume upload or a real resume parser
(spec §15 describes claim extraction from an uploaded document generally —
that's out of scope here). Instead it seeds the one claim used by the §36
demo scenario. A future resume parser would produce `ResumeClaim` objects
the same shape as this one; nothing downstream needs to change.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ResumeClaim:
    competency: str
    text: str


SEED_CLAIM = ResumeClaim(
    competency="rag_architecture",
    text="Designed an enterprise RAG platform using Azure OpenAI, Azure AI Search and AKS.",
)


def seed_claim() -> ResumeClaim:
    return SEED_CLAIM
