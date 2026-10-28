"""Editable candidate-question compliance policy.

Default categories block common legally sensitive hiring topics. Operators can
review and edit category labels, descriptions, enabled state, and regex patterns
through the authenticated policy API. This is a defensive filter, not legal
advice; deployments still need jurisdiction-specific legal review.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComplianceCategory:
    id: str
    label: str
    description: str
    patterns: tuple[str, ...]


DEFAULT_CATEGORIES: tuple[ComplianceCategory, ...] = (
    ComplianceCategory(
        "age",
        "Age",
        "Age, birth date, or graduation year used as an age proxy.",
        (
            r"\bhow old are you\b",
            r"\byour age\b",
            r"\bdate of birth\b",
            r"\bwhat year were you born\b",
            r"\bgraduation year\b",
        ),
    ),
    ComplianceCategory(
        "race_ethnicity",
        "Race and ethnicity",
        "Race, ethnicity, or racial/ethnic background.",
        (
            r"\byour race\b",
            r"\bracial background\b",
            r"\bethnic(?:ity| background)\b",
            r"\bwhat is your ethnicity\b",
        ),
    ),
    ComplianceCategory(
        "religion",
        "Religion",
        "Religious belief, affiliation, or practice.",
        (
            r"\breligion\b",
            r"\breligious beliefs?\b",
            r"\bwhich church\b",
            r"\bmosque\b",
            r"\bwhat faith\b",
        ),
    ),
    ComplianceCategory(
        "disability_health",
        "Disability and health",
        "Disability, medical, or mental-health status.",
        (
            r"\bdisabilit(?:y|ies)\b",
            r"\bmedical condition\b",
            r"\bhealth condition\b",
            r"\bmental health\b",
            r"\bmedication\b",
            r"\bdoctor'?s note\b",
        ),
    ),
    ComplianceCategory(
        "family_marital_pregnancy",
        "Family, marital, and pregnancy",
        "Marital status, children, family plans, pregnancy, or childcare.",
        (
            r"\bmarital status\b",
            r"\bare you married\b",
            r"\bdo you have (?:children|kids)\b",
            r"\bfamily plans\b",
            r"\bpregnan(?:t|cy)\b",
            r"\bchildcare\b",
            r"\bspouse\b",
        ),
    ),
    ComplianceCategory(
        "gender_sex_orientation",
        "Gender and sexual orientation",
        "Gender identity, sexual orientation, or pronouns used as a screening criterion.",
        (
            r"\bwhat is your gender\b",
            r"\bsexual orientation\b",
            r"\btransgender\b",
            r"\bwhat are your pronouns\b",
        ),
    ),
    ComplianceCategory(
        "national_origin_citizenship",
        "National origin and citizenship",
        "National origin, citizenship, nationality, or immigration status.",
        (
            r"\bwhere are you from\b",
            r"\bcountry of origin\b",
            r"\bare you a citizen\b",
            r"\bnationality\b",
            r"\bimmigration status\b",
        ),
    ),
    ComplianceCategory(
        "political_union",
        "Political and union affiliation",
        "Political affiliation or union membership.",
        (
            r"\bpolitical (?:party|affiliation|beliefs?)\b",
            r"\bwhich party do you (?:support|vote)\b",
            r"\bunion member(?:ship)?\b",
        ),
    ),
    ComplianceCategory(
        "military",
        "Military service",
        "Military service or veteran status.",
        (
            r"\bmilitary service\b",
            r"\bwere you in the military\b",
            r"\bveteran status\b",
        ),
    ),
    ComplianceCategory(
        "criminal_history",
        "Criminal history",
        "Criminal record, arrest, or conviction history (jurisdiction-dependent).",
        (
            r"\bcriminal (?:record|history|conviction)\b",
            r"\bhave you been arrested\b",
            r"\bfelony\b",
        ),
    ),
    ComplianceCategory(
        "salary_history",
        "Salary history",
        "Current or previous salary/compensation history.",
        (
            r"\bsalary history\b",
            r"\bcurrent (?:salary|compensation)\b",
            r"\bprevious (?:salary|compensation)\b",
            r"\bhow much (?:do|did) you (?:make|earn)\b",
        ),
    ),
    ComplianceCategory(
        "accent_language",
        "Accent and language",
        "Accent or native-speaker status used as a proxy for ability.",
        (
            r"\bnative (?:english )?speaker\b",
            r"\byour accent\b",
            r"\bwhere did you learn english\b",
        ),
    ),
)


@dataclass(frozen=True)
class ComplianceDecision:
    allowed: bool
    categories: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class CompiledCategory:
    id: str
    label: str
    description: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class CompliancePolicy:
    categories: tuple[CompiledCategory, ...]

    def check(self, text: str) -> ComplianceDecision:
        categories = tuple(
            category.id
            for category in self.categories
            if any(pattern.search(text) for pattern in category.patterns)
        )
        if categories:
            return ComplianceDecision(
                allowed=False,
                categories=categories,
                reason="question touches a legally sensitive hiring topic",
            )
        return ComplianceDecision(allowed=True)


def _compile_category(
    category_id: str,
    label: str,
    description: str,
    patterns: Iterable[str],
    enabled: bool,
) -> CompiledCategory | None:
    if not enabled:
        return None
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            logger.warning("ignoring invalid compliance regex in category %s", category_id)
    if not compiled:
        logger.warning("compliance category %s has no valid patterns", category_id)
        return None
    return CompiledCategory(category_id, label, description, tuple(compiled))


def default_policy() -> CompliancePolicy:
    categories = tuple(
        compiled
        for category in DEFAULT_CATEGORIES
        if (
            compiled := _compile_category(
                category.id, category.label, category.description, category.patterns, True
            )
        )
        is not None
    )
    return CompliancePolicy(categories)


def policy_from_rows(rows: Iterable[dict]) -> CompliancePolicy:
    categories = tuple(
        compiled
        for row in rows
        if (
            compiled := _compile_category(
                str(row["category"]),
                str(row["label"]),
                str(row["description"]),
                row["patterns"],
                bool(row["enabled"]),
            )
        )
        is not None
    )
    return CompliancePolicy(categories)


async def load_policy(tenant_subject: str) -> CompliancePolicy:
    from . import db

    rows = await db.list_compliance_categories(tenant_subject)
    return policy_from_rows(rows) if rows else default_policy()


def check_question(text: str, policy: CompliancePolicy | None = None) -> ComplianceDecision:
    return (policy or default_policy()).check(text)


SAFE_FALLBACK_QUESTIONS = (
    "Could you walk me through a specific example from your recent work that is relevant to this role?",
    "What part of that work did you personally own, and what was the outcome?",
    "Can you describe a difficult technical trade-off you made and why?",
)


__all__ = [
    "DEFAULT_CATEGORIES",
    "SAFE_FALLBACK_QUESTIONS",
    "ComplianceCategory",
    "ComplianceDecision",
    "CompliancePolicy",
    "check_question",
    "default_policy",
    "load_policy",
    "policy_from_rows",
]
