"""Render evidence-linked reports for recruiter review and export.

Every renderer is deterministic and uses only persisted report/transcript data.
HTML output escapes all candidate/model text to prevent stored XSS.
"""

from __future__ import annotations

import csv
import io
import json

from .models import SessionReport, SessionSummary


def _markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _citation_rows(report: SessionReport) -> list[dict[str, str | int | None]]:
    rows: list[dict[str, str | int | None]] = []
    for assessment in report.node_assessments:
        if not assessment.citations:
            rows.append(
                {
                    "node": assessment.node,
                    "description": assessment.description,
                    "status": assessment.status,
                    "strength": assessment.evidence_strength,
                    "uncertainty": assessment.uncertainty,
                    "priority": assessment.priority,
                    "priority_weight": assessment.priority_weight,
                    "priority_status": assessment.priority_status,
                    "turn": None,
                    "quote": "",
                    "model_note": "",
                    "human_decision": None,
                }
            )
            continue
        for citation in assessment.citations:
            rows.append(
                {
                    "node": assessment.node,
                    "description": assessment.description,
                    "status": citation.status,
                    "strength": assessment.evidence_strength,
                    "uncertainty": assessment.uncertainty,
                    "priority": assessment.priority,
                    "priority_weight": assessment.priority_weight,
                    "priority_status": assessment.priority_status,
                    "turn": citation.transcript_turn,
                    "quote": citation.quote,
                    "model_note": citation.note,
                    "human_decision": citation.human_decision,
                }
            )
    return rows


def render_markdown(report: SessionReport, summary: SessionSummary) -> str:
    lines = [
        "# Interview assessment report",
        "",
        f"- Session: `{report.session_id}`",
        f"- Template: `{report.template_id}`",
        f"- Competency: `{report.competency}`",
        f"- Interview complete: {summary.is_complete}",
        f"- Evidence coverage: {report.evidence_coverage:.0%}",
        f"- Priority-weighted coverage: {report.priority_weighted_coverage:.0%}",
        f"- Overall uncertainty: {report.overall_uncertainty}",
        f"- Red flags: {report.red_flag_count}",
        f"- Contradictions: {report.contradictions}",
        "",
        "> Decision support only. A human reviewer must confirm or reject evidence.",
        "",
        "## Rubric evidence",
        "",
        "| Node | Priority | Status | Priority status | Strength | Uncertainty | Turn | Candidate quote |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in _citation_rows(report):
        turn = "" if row["turn"] is None else str(row["turn"])
        lines.append(
            "| {node} | {priority} | {status} | {priority_status} | {strength} | {uncertainty} | {turn} | {quote} |".format(
                node=_markdown_escape(str(row["node"])),
                priority=_markdown_escape(str(row["priority"])),
                status=_markdown_escape(str(row["status"])),
                priority_status=_markdown_escape(str(row["priority_status"])),
                strength="" if row["strength"] is None else row["strength"],
                uncertainty=_markdown_escape(str(row["uncertainty"])),
                turn=turn,
                quote=_markdown_escape(str(row["quote"] or "")),
            )
        )
    lines.extend(["", "## Red flags", ""])
    if report.red_flags:
        lines.extend(
            "| Severity | Code | Node | Message | Turn |",
        )
        lines.extend("| --- | --- | --- | --- | --- |")
        for flag in report.red_flags:
            lines.append(
                "| {severity} | {code} | {node} | {message} | {turn} |".format(
                    severity=flag.severity,
                    code=_markdown_escape(flag.code),
                    node=_markdown_escape(flag.node or "session"),
                    message=_markdown_escape(flag.message),
                    turn="" if flag.evidence_turn is None else flag.evidence_turn,
                )
            )
    else:
        lines.append("- No red flags recorded.")
    lines.extend(["", "## Priority gaps", ""])
    if report.priority_gaps:
        lines.extend(f"- {_markdown_escape(gap)}" for gap in report.priority_gaps)
    else:
        lines.append("- No priority gaps recorded.")
    lines.extend(["", "## Model notes and human review", ""])
    for assessment in report.node_assessments:
        lines.append(f"### {_markdown_escape(assessment.node)}")
        lines.append("")
        if assessment.citations:
            for citation in assessment.citations:
                lines.append(
                    f"- Model inference (turn {citation.transcript_turn}): "
                    f"{_markdown_escape(citation.note)}"
                )
        else:
            lines.append("- No evidence recorded.")
        for review in assessment.human_review:
            lines.append(
                f"- Human {review.decision} by {_markdown_escape(review.reviewer)}"
                + (f": {_markdown_escape(review.note)}" if review.note else "")
            )
        lines.append("")
    lines.extend(["## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in report.limitations)
    lines.append("")
    return "\n".join(lines)


def render_html(report: SessionReport, summary: SessionSummary) -> str:
    from html import escape

    rows = []
    for row in _citation_rows(report):
        rows.append(
            "<tr>"
            f"<td>{escape(str(row['node']))}</td>"
            f"<td>{escape(str(row['priority']))}</td>"
            f"<td>{escape(str(row['status']))}</td>"
            f"<td>{escape(str(row['priority_status']))}</td>"
            f"<td>{'' if row['strength'] is None else row['strength']}</td>"
            f"<td>{escape(str(row['uncertainty']))}</td>"
            f"<td>{'' if row['turn'] is None else row['turn']}</td>"
            f"<td>{escape(str(row['quote'] or ''))}</td>"
            "</tr>"
        )
    reviews = []
    for assessment in report.node_assessments:
        for review in assessment.human_review:
            reviews.append(
                "<li>"
                f"<strong>{escape(assessment.node)}</strong> — "
                f"{escape(review.decision)} by {escape(review.reviewer)}"
                f"{(': ' + escape(review.note)) if review.note else ''}"
                "</li>"
            )
    limitations = "".join(f"<li>{escape(item)}</li>" for item in report.limitations)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Interview report {escape(str(report.session_id))}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #1f2933; }}
table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
th, td {{ border: 1px solid #cbd5e1; padding: .45rem; text-align: left; vertical-align: top; }}
th {{ background: #f1f5f9; }}
.notice {{ background: #fff7ed; border-left: 4px solid #f97316; padding: .75rem 1rem; }}
</style></head><body>
<h1>Interview assessment report</h1>
<p class="notice"><strong>Decision support only.</strong> A human reviewer must confirm or reject evidence before any employment decision.</p>
<ul>
<li><strong>Session:</strong> {escape(str(report.session_id))}</li>
<li><strong>Template:</strong> {escape(report.template_id)}</li>
<li><strong>Competency:</strong> {escape(report.competency)}</li>
<li><strong>Interview complete:</strong> {summary.is_complete}</li>
<li><strong>Evidence coverage:</strong> {report.evidence_coverage:.0%}</li>
<li><strong>Priority-weighted coverage:</strong> {report.priority_weighted_coverage:.0%}</li>
<li><strong>Overall uncertainty:</strong> {escape(report.overall_uncertainty)}</li>
<li><strong>Red flags:</strong> {report.red_flag_count}</li>
<li><strong>Contradictions:</strong> {report.contradictions}</li>
</ul>
<h2>Rubric evidence</h2>
<table><thead><tr><th>Node</th><th>Priority</th><th>Status</th><th>Priority status</th><th>Strength</th><th>Uncertainty</th><th>Turn</th><th>Candidate quote</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Red flags</h2>
<table><thead><tr><th>Severity</th><th>Code</th><th>Node</th><th>Message</th><th>Turn</th></tr></thead>
<tbody>{''.join(
    f"<tr><td>{escape(flag.severity)}</td><td>{escape(flag.code)}</td>"
    f"<td>{escape(flag.node or 'session')}</td><td>{escape(flag.message)}</td>"
    f"<td>{'' if flag.evidence_turn is None else flag.evidence_turn}</td></tr>"
    for flag in report.red_flags
) or '<tr><td colspan="5">No red flags recorded.</td></tr>'}</tbody></table>
<h2>Priority gaps</h2><ul>{''.join(f'<li>{escape(gap)}</li>' for gap in report.priority_gaps) or '<li>No priority gaps recorded.</li>'}</ul>
<h2>Human review</h2><ul>{''.join(reviews) or '<li>No corrections recorded.</li>'}</ul>
<h2>Limitations</h2><ul>{limitations}</ul>
</body></html>"""


def render_csv(report: SessionReport, summary: SessionSummary) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "session_id",
            "template_id",
            "competency",
            "complete",
            "node",
            "description",
            "status",
            "evidence_strength",
            "uncertainty",
            "priority",
            "priority_weight",
            "priority_status",
            "transcript_turn",
            "candidate_quote",
            "model_note",
            "human_decision",
            "red_flag_count",
            "red_flag_codes",
            "red_flag_messages",
        ]
    )
    for row in _citation_rows(report):
        writer.writerow(
            [
                report.session_id,
                report.template_id,
                report.competency,
                summary.is_complete,
                row["node"],
                row["description"],
                row["status"],
                row["strength"],
                row["uncertainty"],
                row["priority"],
                row["priority_weight"],
                row["priority_status"],
                row["turn"],
                row["quote"],
                row["model_note"],
                row["human_decision"],
                report.red_flag_count,
                ";".join(
                    flag.code
                    for flag in report.red_flags
                    if flag.node == row["node"] or flag.node is None
                ),
                ";".join(
                    flag.message
                    for flag in report.red_flags
                    if flag.node == row["node"] or flag.node is None
                ),
            ]
        )
    return output.getvalue()


def render_json(report: SessionReport, summary: SessionSummary) -> str:
    payload = {
        "report": json.loads(report.model_dump_json()),
        "session": {
            "session_id": str(summary.session_id),
            "template_id": summary.template_id,
            "resume_claim": summary.resume_claim,
            "is_complete": summary.is_complete,
        },
    }
    return json.dumps(payload, indent=2)


RENDERERS = {
    "markdown": (render_markdown, "text/markdown; charset=utf-8", "md"),
    "html": (render_html, "text/html; charset=utf-8", "html"),
    "csv": (render_csv, "text/csv; charset=utf-8", "csv"),
    "json": (render_json, "application/json", "json"),
}


__all__ = ["RENDERERS", "render_csv", "render_html", "render_json", "render_markdown"]
