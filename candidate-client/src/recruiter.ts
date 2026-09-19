// Recruiter/hiring-manager view (spec §24). Talks to ai-service directly —
// this is the one surface where transcript + evidence are allowed to
// appear (see docs/ARCHITECTURE.md for why that boundary is drawn here
// rather than through the candidate-facing gateway).

const AI_SERVICE_URL = (import.meta as any).env?.VITE_AI_SERVICE_URL ?? "http://127.0.0.1:8000";

interface SessionListItem {
  session_id: string;
  created_at: string;
  resume_claim: string;
  is_complete: boolean;
  turn_count: number;
}

interface TranscriptSegment {
  turn_number: number;
  speaker: "ai" | "candidate";
  text: string;
}

interface EvidenceRecord {
  competency: string;
  node: string;
  status: string;
  note: string;
  turn_number: number;
}

interface SessionSummary {
  session_id: string;
  resume_claim: string;
  is_complete: boolean;
  transcript: TranscriptSegment[];
  evidence: EvidenceRecord[];
}

export async function renderRecruiterView(root: HTMLElement): Promise<void> {
  root.innerHTML = `<div class="card" style="max-width:640px;text-align:left"><h1>Recruiter view</h1><div id="rv-body">Loading sessions…</div></div>`;
  const body = document.getElementById("rv-body")!;

  const params = new URLSearchParams(location.search);
  const preselect = params.get("session");

  let sessions: SessionListItem[] = [];
  try {
    const res = await fetch(`${AI_SERVICE_URL}/sessions`);
    sessions = await res.json();
  } catch {
    body.textContent = "Could not reach ai-service. Is it running on " + AI_SERVICE_URL + "?";
    return;
  }

  if (sessions.length === 0) {
    body.textContent = "No interview sessions yet.";
    return;
  }

  const options = sessions
    .map(
      (s) =>
        `<option value="${s.session_id}" ${s.session_id === preselect ? "selected" : ""}>` +
        `${s.created_at.slice(0, 19)} — ${s.is_complete ? "complete" : "in progress"} (${s.turn_count} turns)` +
        `</option>`,
    )
    .join("");

  body.innerHTML = `
    <select id="rv-select" style="width:100%;padding:8px;margin-bottom:16px">${options}</select>
    <div id="rv-detail"></div>
  `;

  const select = document.getElementById("rv-select") as HTMLSelectElement;
  select.addEventListener("change", () => loadSummary(select.value));
  await loadSummary(select.value || sessions[0].session_id);
}

async function loadSummary(sessionId: string): Promise<void> {
  const detail = document.getElementById("rv-detail")!;
  detail.textContent = "Loading…";

  const res = await fetch(`${AI_SERVICE_URL}/sessions/${sessionId}/summary`);
  if (!res.ok) {
    detail.textContent = "Could not load that session.";
    return;
  }
  const summary: SessionSummary = await res.json();

  const evidenceRows = summary.evidence
    .map(
      (e) =>
        `<tr><td>${e.node}</td><td>${statusBadge(e.status)}</td><td>${e.note}</td><td>turn ${e.turn_number}</td></tr>`,
    )
    .join("");

  const transcriptRows = summary.transcript
    .map(
      (t) =>
        `<p><strong>${t.speaker === "ai" ? "Interviewer" : "Candidate"}:</strong> ${escapeHtml(t.text)}</p>`,
    )
    .join("");

  detail.innerHTML = `
    <p class="hint"><strong>Resume claim:</strong> ${escapeHtml(summary.resume_claim)}</p>
    <p class="hint"><strong>Status:</strong> ${summary.is_complete ? "Complete" : "In progress"}</p>

    <h2 style="font-size:1rem;margin-top:24px">Evidence — RAG Architecture</h2>
    <table style="width:100%;border-collapse:collapse;font-size:0.9rem">
      <thead><tr><th align="left">Node</th><th align="left">Status</th><th align="left">Note</th><th align="left">Turn</th></tr></thead>
      <tbody>${evidenceRows || '<tr><td colspan="4">No evidence recorded yet.</td></tr>'}</tbody>
    </table>

    <h2 style="font-size:1rem;margin-top:24px">Transcript</h2>
    <div style="font-size:0.9rem;line-height:1.6">${transcriptRows}</div>
  `;
}

function statusBadge(status: string): string {
  const colors: Record<string, string> = {
    confirmed: "#4caf50",
    weak: "#ffb300",
    contradicted: "#e53935",
    not_addressed: "#8b93a1",
  };
  const color = colors[status] ?? "#8b93a1";
  return `<span style="color:${color};font-weight:600">${status}</span>`;
}

function escapeHtml(s: string): string {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}
