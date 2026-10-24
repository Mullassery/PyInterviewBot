import { Mic } from "./mic";
import { Player } from "./player";
import { InterviewSocket, type InterviewState } from "./ws-client";
import { renderRecruiterView } from "./recruiter";

const GATEWAY_WS_URL = (import.meta as any).env?.VITE_GATEWAY_WS_URL ?? "ws://127.0.0.1:8787/ws/interview";

const app = document.getElementById("app")!;

if (location.pathname.startsWith("/recruiter")) {
  renderRecruiterView(app);
} else {
  renderCandidateView(app);
}

function renderCandidateView(root: HTMLElement) {
  root.innerHTML = `
    <div class="card" id="welcome-card">
      <h1>AI Interviewer</h1>
      <p class="hint">
        You will have a conversational interview with an AI interviewer.<br />
        Estimated duration: 5&ndash;10 minutes.<br />
        You will answer using your microphone.
      </p>
      <div class="meter"><div class="meter-fill" id="meter-fill"></div></div>
      <label class="hint" for="device-select">Microphone</label>
      <select id="device-select"><option value="">Default microphone</option></select>
      <button id="test-mic-btn">Test Microphone</button>
      <label class="consent">
        <input type="checkbox" id="consent-check" />
        I consent to my microphone audio being processed for this interview.
        Audio is not recorded by this deployment; transcript and evidence are stored
        for the interview and can be deleted.
      </label>
      <button id="begin-btn" class="secondary" disabled>Begin Interview</button>
    </div>
  `;

  const mic = new Mic();
  const testMicBtn = document.getElementById("test-mic-btn") as HTMLButtonElement;
  const beginBtn = document.getElementById("begin-btn") as HTMLButtonElement;
  const meterFill = document.getElementById("meter-fill") as HTMLDivElement;
  const deviceSelect = document.getElementById("device-select") as HTMLSelectElement;
  const consentCheck = document.getElementById("consent-check") as HTMLInputElement;

  let meterInterval: number | undefined;
  let micTested = false;

  function refreshBeginState() {
    beginBtn.disabled = !(micTested && consentCheck.checked);
  }

  consentCheck.addEventListener("change", refreshBeginState);

  testMicBtn.addEventListener("click", async () => {
    testMicBtn.disabled = true;
    testMicBtn.textContent = "Listening for your voice...";
    try {
      await mic.start(() => {}); // discard chunks during the test — just want the level meter
      meterInterval = window.setInterval(() => {
        meterFill.style.width = `${Math.min(100, mic.level() * 300)}%`;
      }, 60);
      micTested = true;
      refreshBeginState();
      testMicBtn.textContent = "Microphone working";
      const devices = await navigator.mediaDevices.enumerateDevices();
      const inputs = devices.filter((device) => device.kind === "audioinput");
      deviceSelect.innerHTML =
        '<option value="">Default microphone</option>' +
        inputs
          .map(
            (device, index) =>
              `<option value="${device.deviceId}">${device.label || `Microphone ${index + 1}`}</option>`,
          )
          .join("");
    } catch (err) {
      micTested = false;
      refreshBeginState();
      testMicBtn.textContent = "Microphone access denied — check browser permissions";
      testMicBtn.disabled = false;
    }
  });

  beginBtn.addEventListener("click", () => {
    if (meterInterval) window.clearInterval(meterInterval);
    mic.stop();
    startInterview(root, deviceSelect.value || undefined);
  });
}

function startInterview(root: HTMLElement, deviceId?: string) {
  root.innerHTML = `
    <div class="card">
      <h1>AI Interviewer</h1>
      <div class="question" id="question-text">Connecting...</div>
      <div class="status-icon" id="status-icon">&#128266;</div>
      <div class="status-label" id="status-label"></div>
      <div class="timer" id="timer">0:00</div>
      <button id="end-btn" class="secondary">End Interview</button>
    </div>
  `;

  const questionText = document.getElementById("question-text")!;
  const statusIcon = document.getElementById("status-icon")!;
  const statusLabel = document.getElementById("status-label")!;
  const timerEl = document.getElementById("timer")!;
  const endBtn = document.getElementById("end-btn") as HTMLButtonElement;

  const player = new Player();
  const mic = new Mic();
  let socket: InterviewSocket;
  let micStarted = false;
  let startedAt = 0;
  let timerInterval: number | undefined;

  function setStatus(state: InterviewState) {
    const labels: Record<InterviewState, string> = {
      initializing: "Connecting…",
      ai_speaking: "",
      listening: "Listening",
      processing: "",
      paused: "Paused",
      completed: "Interview complete",
    };
    const icons: Record<InterviewState, string> = {
      initializing: "\u{1F50C}",
      ai_speaking: "\u{1F50A}",
      listening: "\u{1F3A4}",
      processing: "\u{2026}",
      paused: "⏸",
      completed: "✅",
    };
    statusLabel.textContent = labels[state];
    statusIcon.textContent = icons[state];
    statusIcon.classList.toggle("pulse", state === "listening" || state === "ai_speaking");
  }

  function tick() {
    const elapsedSec = Math.floor((Date.now() - startedAt) / 1000);
    const m = Math.floor(elapsedSec / 60);
    const s = elapsedSec % 60;
    timerEl.textContent = `${m}:${s.toString().padStart(2, "0")}`;
  }

  socket = new InterviewSocket(GATEWAY_WS_URL, {
    onState: async (state, _turn, text) => {
      setStatus(state);
      if (text) questionText.textContent = text;
      if (!startedAt) {
        startedAt = Date.now();
        timerInterval = window.setInterval(tick, 1000);
      }
      if (!micStarted && (state === "listening" || state === "ai_speaking")) {
        micStarted = true;
        await mic.start((chunk) => socket.sendAudio(chunk), deviceId);
      }
      if (state === "completed") {
        if (timerInterval) window.clearInterval(timerInterval);
        mic.stop();
        endBtn.textContent = "Done";
        endBtn.disabled = true;
      }
    },
    onAudio: (pcm16) => player.enqueue(pcm16),
    onInterrupt: () => player.interrupt(),
    onError: (message) => {
      questionText.textContent = message;
    },
    onClose: () => {
      if (timerInterval) window.clearInterval(timerInterval);
      mic.stop();
    },
  });

  endBtn.addEventListener("click", () => {
    if (timerInterval) window.clearInterval(timerInterval);
    mic.stop();
    socket.close();
    endBtn.textContent = "Ended";
    endBtn.disabled = true;
  });
}
