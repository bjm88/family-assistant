import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Bot,
  Camera,
  CheckCircle2,
  Mic,
  MicOff,
  Send,
  User,
  Video,
  VideoOff,
} from "lucide-react";
import { api, resolveApiPath } from "@/lib/api";
import type { Assistant, Family } from "@/lib/types";
import { AssistantAvatar } from "@/pages/AssistantPage";
import { useToast } from "@/components/Toast";
import { cn } from "@/lib/cn";

/**
 * Live AI assistant page.
 *
 * Three subsystems share this screen:
 *
 *  1. **Camera** — continuous webcam preview, with a background loop that
 *     snaps a JPEG roughly every 2 seconds and posts it to
 *     `/api/aiassistant/face/recognize`. When a new person is identified
 *     we fire a one-shot greeting and append it to the chat.
 *  2. **Microphone** — optional Web Speech API listener that dictates
 *     into the chat input. Off by default; toggling it prompts the
 *     browser mic permission.
 *  3. **Chat** — streaming chat against the local Ollama model. The
 *     most recently recognized person is passed along as RAG context
 *     so Avi's answers can reference them specifically.
 */

type LlmStatus = {
  host: string;
  model: string;
  available: boolean;
  model_pulled: boolean;
  installed_models: string[];
  error: string | null;
};

type FaceStatus = {
  providers: string[];
  mac_studio_optimized: boolean;
  threshold: number;
  enrolled_embeddings: number;
};

type RecognizeResponse = {
  matched: boolean;
  person_id: number | null;
  person_name: string | null;
  similarity: number | null;
  threshold: number;
  reason: string | null;
};

type ChatRole = "user" | "assistant" | "system";
type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  streaming?: boolean;
  meta?: string;
};

// How often we snap a webcam frame for face recognition. Slow enough that
// an M-series Mac doesn't spin up the fans; fast enough to feel "live".
const FACE_RECOG_INTERVAL_MS = 2500;
// After we greet someone, suppress re-greetings for this long so Avi
// doesn't loop "Hi Sam!" every time they blink.
const GREETING_SUPPRESSION_MS = 90_000;

export default function AiAssistantPage() {
  const { familyId: familyIdParam } = useParams();
  const familyId = Number(familyIdParam);

  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyIdParam],
    queryFn: () => api.get<Family>(`/api/families/${familyIdParam}`),
    enabled: Number.isFinite(familyId),
  });

  const { data: assistants } = useQuery<Assistant[]>({
    queryKey: ["assistants", familyIdParam],
    queryFn: () =>
      api.get<Assistant[]>(`/api/assistants?family_id=${familyIdParam}`),
    enabled: Number.isFinite(familyId),
  });
  const assistant = assistants?.[0];
  const assistantName = assistant?.assistant_name ?? "Avi";

  const { data: llmStatus } = useQuery<LlmStatus>({
    queryKey: ["ai-llm-status"],
    queryFn: () => api.get<LlmStatus>("/api/aiassistant/status"),
    refetchInterval: 15_000,
  });

  const { data: faceStatus } = useQuery<FaceStatus>({
    queryKey: ["ai-face-status", familyId],
    queryFn: () =>
      api.get<FaceStatus>(
        `/api/aiassistant/face/status?family_id=${familyId}`
      ),
    enabled: Number.isFinite(familyId),
  });

  return (
    <div className="min-h-screen bg-gradient-to-br from-background to-muted">
      <header className="border-b border-border bg-white">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Link
              to={`/admin/families/${familyId}`}
              className="text-sm text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
            >
              <ArrowLeft className="h-4 w-4" /> Back to admin
            </Link>
            <div>
              <div className="text-xs text-muted-foreground uppercase tracking-wide">
                {family?.family_name ?? "—"} · Live Assistant
              </div>
              <div className="font-semibold text-lg flex items-center gap-2">
                <Bot className="h-5 w-5 text-primary" /> {assistantName}
              </div>
            </div>
          </div>
          <StatusBadges llm={llmStatus} face={faceStatus} />
        </div>
      </header>

      <main className="max-w-7xl mx-auto p-6 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_400px] gap-6">
        <LiveCameraPanel
          familyId={familyId}
          assistant={assistant}
          assistantName={assistantName}
        />
        <ChatPanel
          familyId={familyId}
          assistantName={assistantName}
          llmStatus={llmStatus}
        />
      </main>
    </div>
  );
}

// ============================================================================
// Status
// ============================================================================

function StatusBadges({
  llm,
  face,
}: {
  llm: LlmStatus | undefined;
  face: FaceStatus | undefined;
}) {
  return (
    <div className="flex items-center gap-2">
      <Badge
        ok={!!llm?.available && !!llm?.model_pulled}
        warn={!!llm?.available && !llm?.model_pulled}
        label={
          llm === undefined
            ? "LLM…"
            : !llm.available
            ? "LLM offline"
            : !llm.model_pulled
            ? `Missing ${llm.model}`
            : `LLM: ${llm.model}`
        }
        title={
          llm === undefined
            ? undefined
            : !llm.available
            ? `Ollama at ${llm.host} is not responding.`
            : !llm.model_pulled
            ? `Run 'ollama pull ${llm.model}' to enable chat.`
            : `Ollama ${llm.host} — installed: ${
                llm.installed_models.join(", ") || "(none)"
              }`
        }
      />
      <Badge
        ok={!!face && face.enrolled_embeddings > 0}
        warn={!!face && face.enrolled_embeddings === 0}
        label={
          face === undefined
            ? "Face…"
            : face.enrolled_embeddings === 0
            ? "0 faces enrolled"
            : `${face.enrolled_embeddings} faces`
        }
        title={
          face === undefined
            ? undefined
            : `Providers: ${face.providers.join(", ")}. Threshold: ${face.threshold}.`
        }
      />
    </div>
  );
}

function Badge({
  ok,
  warn,
  label,
  title,
}: {
  ok: boolean;
  warn?: boolean;
  label: string;
  title?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs border",
        ok
          ? "bg-emerald-50 text-emerald-700 border-emerald-200"
          : warn
          ? "bg-amber-50 text-amber-700 border-amber-200"
          : "bg-rose-50 text-rose-700 border-rose-200"
      )}
      title={title}
    >
      {ok ? (
        <CheckCircle2 className="h-3.5 w-3.5" />
      ) : (
        <AlertTriangle className="h-3.5 w-3.5" />
      )}
      {label}
    </span>
  );
}

// ============================================================================
// Live camera + face recognition loop
// ============================================================================

function LiveCameraPanel({
  familyId,
  assistant,
  assistantName,
}: {
  familyId: number;
  assistant: Assistant | undefined;
  assistantName: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [cameraOn, setCameraOn] = useState(true);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [lastRecognition, setLastRecognition] =
    useState<RecognizeResponse | null>(null);

  // Mount / unmount the getUserMedia stream.
  useEffect(() => {
    if (!cameraOn) {
      const v = videoRef.current;
      const stream = v?.srcObject as MediaStream | null;
      stream?.getTracks().forEach((t) => t.stop());
      if (v) v.srcObject = null;
      return;
    }

    let stopped = false;
    let stream: MediaStream | null = null;
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 1280 }, height: { ideal: 720 } },
          audio: false,
        });
        if (stopped) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        const v = videoRef.current;
        if (v) {
          v.srcObject = stream;
          await v.play().catch(() => undefined);
        }
        setCameraError(null);
      } catch (e) {
        setCameraError(
          e instanceof Error ? e.message : "Camera access was denied."
        );
      }
    })();

    return () => {
      stopped = true;
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, [cameraOn]);

  return (
    <div className="space-y-4">
      <div className="card overflow-hidden">
        <div className="relative bg-black aspect-video">
          <video
            ref={videoRef}
            className="absolute inset-0 h-full w-full object-cover"
            playsInline
            muted
          />
          <canvas ref={canvasRef} className="hidden" />
          {!cameraOn && (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-white/80 gap-2 bg-black/70">
              <VideoOff className="h-10 w-10" />
              <div>Camera is off.</div>
            </div>
          )}
          {cameraError && cameraOn && (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-white/90 gap-2 bg-black/80 text-sm text-center px-8">
              <AlertTriangle className="h-8 w-8" />
              <div>Couldn't start the camera:</div>
              <div className="font-mono text-xs">{cameraError}</div>
            </div>
          )}
          <div className="absolute top-3 left-3 flex items-center gap-2">
            {cameraOn && !cameraError && (
              <span className="inline-flex items-center gap-1 bg-black/60 text-white text-xs px-2 py-1 rounded-full">
                <span className="h-2 w-2 rounded-full bg-rose-500 animate-pulse" />
                LIVE
              </span>
            )}
            {lastRecognition?.matched && (
              <span className="inline-flex items-center gap-1 bg-emerald-500/90 text-white text-xs px-2 py-1 rounded-full">
                <User className="h-3 w-3" />
                {lastRecognition.person_name} ·{" "}
                {Math.round((lastRecognition.similarity ?? 0) * 100)}%
              </span>
            )}
          </div>
          <div className="absolute bottom-3 right-3">
            <button
              className="inline-flex items-center gap-1 bg-white/90 hover:bg-white text-foreground text-xs px-3 py-1.5 rounded-full shadow"
              onClick={() => setCameraOn((c) => !c)}
            >
              {cameraOn ? (
                <>
                  <VideoOff className="h-3.5 w-3.5" /> Pause camera
                </>
              ) : (
                <>
                  <Video className="h-3.5 w-3.5" /> Start camera
                </>
              )}
            </button>
          </div>
        </div>
        <div className="card-body flex items-center gap-4">
          <AssistantAvatar
            assistant={
              assistant ?? {
                assistant_name: assistantName,
                profile_image_path: null,
              }
            }
            size={64}
          />
          <div className="min-w-0">
            <div className="font-semibold">{assistantName}</div>
            <div className="text-xs text-muted-foreground">
              {cameraOn
                ? "Watching for family members. When I recognize someone I'll say hi in chat."
                : "Camera paused — recognition is idle."}
            </div>
          </div>
        </div>
      </div>

      <FaceRecognitionLoop
        familyId={familyId}
        videoRef={videoRef}
        canvasRef={canvasRef}
        cameraOn={cameraOn && !cameraError}
        onRecognize={setLastRecognition}
      />
    </div>
  );
}

/**
 * Background loop: snap a frame every FACE_RECOG_INTERVAL_MS and post to
 * /api/aiassistant/face/recognize. When a new person is identified, push
 * a greeting into the chat by dispatching a `CustomEvent('avi:greet')`.
 *
 * Intentionally renders nothing — it's an effect-only component so the
 * surrounding presentational code stays readable.
 */
function FaceRecognitionLoop({
  familyId,
  videoRef,
  canvasRef,
  cameraOn,
  onRecognize,
}: {
  familyId: number;
  videoRef: React.RefObject<HTMLVideoElement>;
  canvasRef: React.RefObject<HTMLCanvasElement>;
  cameraOn: boolean;
  onRecognize: (r: RecognizeResponse) => void;
}) {
  const lastGreetedRef = useRef<Map<number, number>>(new Map());

  useEffect(() => {
    if (!cameraOn) return;
    let cancelled = false;
    let inflight = false;

    const tick = async () => {
      if (cancelled || inflight) return;
      const v = videoRef.current;
      const c = canvasRef.current;
      if (!v || !c || v.readyState < 2 || v.videoWidth === 0) return;
      inflight = true;
      try {
        c.width = 640;
        c.height = Math.round((640 * v.videoHeight) / v.videoWidth);
        const ctx = c.getContext("2d");
        if (!ctx) return;
        ctx.drawImage(v, 0, 0, c.width, c.height);
        const blob: Blob | null = await new Promise((resolve) =>
          c.toBlob(resolve, "image/jpeg", 0.8)
        );
        if (!blob) return;

        const form = new FormData();
        form.append("family_id", String(familyId));
        form.append("file", blob, "frame.jpg");
        const result = await api.upload<RecognizeResponse>(
          "/api/aiassistant/face/recognize",
          form
        );
        if (cancelled) return;
        onRecognize(result);

        if (result.matched && result.person_id !== null) {
          const last = lastGreetedRef.current.get(result.person_id) ?? 0;
          if (Date.now() - last > GREETING_SUPPRESSION_MS) {
            lastGreetedRef.current.set(result.person_id, Date.now());
            window.dispatchEvent(
              new CustomEvent("avi:greet", {
                detail: {
                  person_id: result.person_id,
                  person_name: result.person_name,
                },
              })
            );
          }
        }
      } catch (e) {
        // Swallow transient recognition errors — the status badge covers
        // the persistent failure modes (Ollama down, no embeddings).
        console.debug("recognize failed", e);
      } finally {
        inflight = false;
      }
    };

    const id = window.setInterval(tick, FACE_RECOG_INTERVAL_MS);
    void tick();
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [cameraOn, familyId, videoRef, canvasRef, onRecognize]);

  return null;
}

// ============================================================================
// Chat
// ============================================================================

type GreetEventDetail = { person_id: number; person_name: string | null };

function ChatPanel({
  familyId,
  assistantName,
  llmStatus,
}: {
  familyId: number;
  assistantName: string;
  llmStatus: LlmStatus | undefined;
}) {
  const toast = useToast();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [recognizedPersonId, setRecognizedPersonId] = useState<number | null>(
    null
  );
  const [listening, setListening] = useState(false);
  const [speechSupported, setSpeechSupported] = useState(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Auto-scroll to bottom on every new chunk.
  useEffect(() => {
    const el = messagesRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages]);

  // Detect speech recognition support (Chrome / Safari).
  useEffect(() => {
    const W = window as unknown as {
      SpeechRecognition?: unknown;
      webkitSpeechRecognition?: unknown;
    };
    setSpeechSupported(!!(W.SpeechRecognition || W.webkitSpeechRecognition));
  }, []);

  // Listen for face-recognition greetings fired by the camera loop.
  useEffect(() => {
    const handler = (evt: Event) => {
      const detail = (evt as CustomEvent<GreetEventDetail>).detail;
      if (!detail) return;
      setRecognizedPersonId(detail.person_id);
      void triggerGreet(detail.person_id);
    };
    window.addEventListener("avi:greet", handler as EventListener);
    return () =>
      window.removeEventListener("avi:greet", handler as EventListener);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [familyId]);

  const appendMessage = useCallback((m: ChatMessage) => {
    setMessages((prev) => [...prev, m]);
  }, []);

  async function triggerGreet(personId: number) {
    if (!llmStatus?.available || !llmStatus.model_pulled) {
      appendMessage({
        id: crypto.randomUUID(),
        role: "system",
        content: `I spotted someone but can't greet them yet — the local LLM (${
          llmStatus?.model ?? "?"
        }) isn't ready.`,
      });
      return;
    }
    const placeholder: ChatMessage = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      streaming: true,
      meta: "recognized someone",
    };
    appendMessage(placeholder);
    try {
      const resp = await api.post<{
        greeting: string;
        context_preview: string;
      }>("/api/aiassistant/greet", {
        family_id: familyId,
        person_id: personId,
      });
      setMessages((prev) =>
        prev.map((m) =>
          m.id === placeholder.id
            ? { ...m, content: resp.greeting, streaming: false }
            : m
        )
      );
    } catch (e) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === placeholder.id
            ? {
                ...m,
                content:
                  e instanceof Error
                    ? `Could not greet them: ${e.message}`
                    : "Could not greet them.",
                streaming: false,
                role: "system",
              }
            : m
        )
      );
    }
  }

  async function sendUserMessage(text: string) {
    const content = text.trim();
    if (!content || isStreaming) return;
    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content,
    };
    const assistantMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      streaming: true,
    };
    const newMessages = [...messages, userMsg];
    setMessages([...newMessages, assistantMsg]);
    setInput("");
    setIsStreaming(true);

    try {
      const historyForServer = newMessages
        .filter((m) => m.role === "user" || m.role === "assistant")
        .map((m) => ({ role: m.role, content: m.content }));
      const res = await fetch(resolveApiPath("/api/aiassistant/chat"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          family_id: familyId,
          messages: historyForServer,
          recognized_person_id: recognizedPersonId,
        }),
      });
      if (!res.ok || !res.body) {
        throw new Error(`HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let lastError: string | null = null;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // SSE: events separated by blank lines, each starting with "data: ".
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = raw.startsWith("data: ") ? raw.slice(6) : raw;
          try {
            const parsed = JSON.parse(line);
            if (parsed.delta) {
              const delta: string = parsed.delta;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMsg.id
                    ? { ...m, content: m.content + delta }
                    : m
                )
              );
            }
            if (parsed.error) {
              lastError = parsed.error;
            }
          } catch {
            /* ignore keep-alive frames */
          }
        }
      }
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsg.id
            ? {
                ...m,
                streaming: false,
                role: lastError ? "system" : m.role,
                content: lastError ? `LLM error: ${lastError}` : m.content,
              }
            : m
        )
      );
    } catch (e) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsg.id
            ? {
                ...m,
                streaming: false,
                role: "system",
                content:
                  e instanceof Error
                    ? `Chat failed: ${e.message}`
                    : "Chat failed.",
              }
            : m
        )
      );
      toast.error("Chat failed. Is Ollama running?");
    } finally {
      setIsStreaming(false);
    }
  }

  // ---- Microphone / Web Speech API --------------------------------------
  const recognitionRef = useRef<{
    start: () => void;
    stop: () => void;
  } | null>(null);

  useEffect(() => {
    if (!listening || !speechSupported) return;
    const W = window as unknown as {
      SpeechRecognition?: new () => unknown;
      webkitSpeechRecognition?: new () => unknown;
    };
    const Ctor = W.SpeechRecognition ?? W.webkitSpeechRecognition;
    if (!Ctor) return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const rec = new (Ctor as any)();
    rec.lang = "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    let finalText = "";
    rec.onresult = (e: {
      resultIndex: number;
      results: {
        isFinal: boolean;
        [k: number]: { transcript: string };
      }[];
    }) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) {
          finalText += r[0].transcript;
        } else {
          interim += r[0].transcript;
        }
      }
      setInput((finalText + " " + interim).trim());
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => {
      if (finalText.trim()) {
        void sendUserMessage(finalText);
        finalText = "";
      }
      // Auto-restart while the toggle is on.
      if (listening) {
        try {
          rec.start();
        } catch {
          /* noop */
        }
      }
    };
    try {
      rec.start();
    } catch {
      /* noop */
    }
    recognitionRef.current = {
      start: () => rec.start(),
      stop: () => rec.stop(),
    };
    return () => {
      try {
        rec.stop();
      } catch {
        /* noop */
      }
      recognitionRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listening, speechSupported]);

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void sendUserMessage(input);
  };

  const hintLine = useMemo(() => {
    if (!llmStatus) return "Connecting to local LLM…";
    if (!llmStatus.available)
      return `Ollama isn't reachable at ${llmStatus.host}.`;
    if (!llmStatus.model_pulled)
      return `Run 'ollama pull ${llmStatus.model}' to enable chat.`;
    return `Ready. Local model: ${llmStatus.model}.`;
  }, [llmStatus]);

  return (
    <div className="card flex flex-col min-h-[600px]">
      <div className="card-header">
        <div className="card-title flex items-center gap-2">
          <Bot className="h-4 w-4 text-primary" /> Chat with {assistantName}
        </div>
        <div className="text-xs text-muted-foreground">{hintLine}</div>
      </div>

      <div
        ref={messagesRef}
        className="flex-1 overflow-y-auto px-4 py-3 space-y-3 bg-muted/30"
      >
        {messages.length === 0 && (
          <div className="text-sm text-muted-foreground h-full flex flex-col items-center justify-center text-center gap-2 pt-16">
            <Camera className="h-8 w-8" />
            <div>Stand in front of the camera,</div>
            <div>or type a question to start.</div>
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} assistantName={assistantName} />
        ))}
      </div>

      <form
        onSubmit={onSubmit}
        className="border-t border-border p-3 flex items-center gap-2 bg-white"
      >
        <button
          type="button"
          className={cn(
            "h-10 w-10 rounded-full inline-flex items-center justify-center border",
            listening
              ? "bg-rose-500 text-white border-rose-500"
              : "bg-white text-muted-foreground border-border hover:text-foreground"
          )}
          disabled={!speechSupported}
          title={
            speechSupported
              ? listening
                ? "Stop listening"
                : "Start listening"
              : "Speech recognition not supported in this browser"
          }
          onClick={() => setListening((l) => !l)}
        >
          {listening ? (
            <Mic className="h-4 w-4" />
          ) : (
            <MicOff className="h-4 w-4" />
          )}
        </button>
        <input
          ref={inputRef}
          className="input flex-1"
          placeholder={
            isStreaming ? `${assistantName} is thinking…` : "Ask anything…"
          }
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={isStreaming}
        />
        <button
          type="submit"
          className="btn-primary h-10 px-4"
          disabled={isStreaming || !input.trim()}
        >
          <Send className="h-4 w-4" />
        </button>
      </form>
    </div>
  );
}

function MessageBubble({
  message,
  assistantName,
}: {
  message: ChatMessage;
  assistantName: string;
}) {
  if (message.role === "system") {
    return (
      <div className="text-xs text-muted-foreground text-center italic">
        {message.content}
      </div>
    );
  }
  const isUser = message.role === "user";
  return (
    <div className={cn("flex gap-2", isUser && "justify-end")}>
      {!isUser && (
        <div className="h-7 w-7 rounded-full bg-primary/10 text-primary flex items-center justify-center flex-shrink-0">
          <Bot className="h-4 w-4" />
        </div>
      )}
      <div
        className={cn(
          "max-w-[85%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap",
          isUser
            ? "bg-primary text-primary-foreground rounded-br-sm"
            : "bg-white border border-border rounded-bl-sm"
        )}
      >
        {!isUser && message.meta && (
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-0.5">
            {assistantName} · {message.meta}
          </div>
        )}
        {message.content || (message.streaming ? "…" : "")}
      </div>
      {isUser && (
        <div className="h-7 w-7 rounded-full bg-muted flex items-center justify-center flex-shrink-0">
          <User className="h-4 w-4 text-muted-foreground" />
        </div>
      )}
    </div>
  );
}
