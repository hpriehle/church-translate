export type LiveSender = { name: string };

export type LiveEnglishModuleEvents = {
  onSenders: (event: { senders: LiveSender[] }) => void;
};

export type LiveStats = {
  active: boolean;
  phase?: string; // "connecting" | "playing" | "noAudio" | "failed: …"
  delayMs?: number;
  bufferMs?: number;
  jitterMs?: number;
  lossPercent?: number;
  rttMs?: number;
  listeners?: number;
  outputMs?: number;
  wireless: boolean;
  outputName: string;
};
