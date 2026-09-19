export type InterviewState =
  | "initializing"
  | "ai_speaking"
  | "listening"
  | "processing"
  | "paused"
  | "completed";

export interface WsCallbacks {
  /// `text`, when present, is the AI's own line it's about to speak (never
  /// the candidate's transcript — see gateway/src/ws_handler.rs).
  onState: (state: InterviewState, turn: number, text?: string) => void;
  onAudio: (pcm16: ArrayBuffer) => void;
  onInterrupt: () => void;
  onError: (message: string) => void;
  onClose: () => void;
}

// The only message shapes the gateway ever sends — notably absent: any
// transcript/score/evidence field. See gateway/src/ws_handler.rs.
type ControlMessage =
  | { type: "state"; state: InterviewState; turn: number; text?: string }
  | { type: "interrupt" }
  | { type: "error"; message: string };

export class InterviewSocket {
  private ws: WebSocket;

  constructor(url: string, callbacks: WsCallbacks) {
    this.ws = new WebSocket(url);
    this.ws.binaryType = "arraybuffer";

    this.ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data === "string") {
        const msg = JSON.parse(event.data) as ControlMessage;
        if (msg.type === "state") callbacks.onState(msg.state, msg.turn, msg.text);
        else if (msg.type === "interrupt") callbacks.onInterrupt();
        else if (msg.type === "error") callbacks.onError(msg.message);
      } else {
        callbacks.onAudio(event.data as ArrayBuffer);
      }
    };

    this.ws.onclose = () => callbacks.onClose();
    this.ws.onerror = () => callbacks.onError("Connection error.");
  }

  sendAudio(pcm16: ArrayBuffer): void {
    if (this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(pcm16);
    }
  }

  close(): void {
    this.ws.close();
  }
}
