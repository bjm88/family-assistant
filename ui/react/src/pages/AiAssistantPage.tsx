import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Bot,
  Camera,
  CheckCircle2,
  History,
  Mic,
  MicOff,
  Send,
  Sparkles,
  Square,
  User,
  Users,
  Video,
  VideoOff,
  Volume2,
  VolumeX,
  Zap,
} from "lucide-react";
import { api, resolveApiPath } from "@/lib/api";
import type {
  Assistant,
  Family,
  LiveSession,
  LiveSessionDetail,
} from "@/lib/types";
import { useToast } from "@/components/Toast";
import { cn } from "@/lib/cn";
import AviLive2D, { type AviLive2DState } from "./AviLive2D";
import SpeakingMouth from "./SpeakingMouth";

// Live2D character bundled under /public/live2d. Keep this folder name
// in sync with the directory you drop a new model into. (We use the
// free "Natori" model from Live2D's CubismWebSamples — see
// public/live2d/LICENSE.md.)
const AVI_LIVE2D_MODEL_PATH = "natori";
const AVI_LIVE2D_MODEL_FILE = "Natori.model3.json";

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

type TtsStatus = {
  enabled: boolean;
  engine: string;
  default_voice: string;
  model_present: boolean;
  voices_present: boolean;
  initialized: boolean;
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

// Echo cancellation: how long to keep the microphone disarmed AFTER
// Avi finishes a TTS clip. The browser's mic capture buffer + the
// SpeechRecognition engine's lookback both retain a beat of audio
// past the speaker silencing, so re-arming immediately is what was
// causing Avi's tail words to be transcribed as user input. 700 ms
// is comfortably longer than typical engine lookback (~300-500 ms)
// without feeling laggy in conversation.
const ECHO_GRACE_MS = 700;

// -------------------------------------------------------------------------
// Audio queue — plays Kokoro WAV clips one after another without overlap.
// Returns a handle with `enqueue(text, voiceHint)` and `mute`/`unmute`.
// We keep a single <audio> element and an array of pending blob URLs so
// the "instant greeting" and the "LLM follow-up" can be fired in parallel
// but played sequentially.
// -------------------------------------------------------------------------
type AudioQueueHandle = {
  enqueue: (text: string, opts?: { gender_hint?: string | null }) => void;
  setMuted: (muted: boolean) => void;
  // True while a WAV is actively playing out of the speakers.
  isSpeaking: boolean;
  // Real-time 0..1 audio amplitude (RMS). Drives Avi's speaking
  // animations — aura intensity, echo rings, mouth glow. The ref is
  // updated on every rAF tick for ~60 Hz consumers (e.g. the Live2D
  // Pixi ticker driving ParamMouthOpenY); `amplitude` is the React
  // state mirror for CSS-driven effects.
  amplitude: number;
  amplitudeRef: React.MutableRefObject<number>;
  // The single HTMLAudioElement we play Kokoro clips through. Exposed
  // so AviLive2D can subscribe to play/pause events if it wants to
  // kick off motions precisely in sync with speech.
  audioEl: HTMLAudioElement | null;
};

function useAudioQueue(muted: boolean): AudioQueueHandle {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<
    Array<{ text: string; gender_hint?: string | null; token: number }>
  >([]);
  const playingRef = useRef(false);
  const tokenRef = useRef(0);
  const mutedRef = useRef(muted);
  mutedRef.current = muted;

  const [isSpeaking, setIsSpeaking] = useState(false);
  const [amplitude, setAmplitude] = useState(0);
  const amplitudeRef = useRef(0);
  // Mirror the audio element as React state so consumers (Live2D) can
  // re-run effects when it becomes available on the first render.
  const [audioEl, setAudioEl] = useState<HTMLAudioElement | null>(null);

  // Web Audio analyser — wired the first time we actually play a clip
  // so AudioContext creation happens after a user gesture (avoids the
  // "was not allowed to start" warning in Chrome/Safari).
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!audioRef.current) {
      const el = new Audio();
      el.crossOrigin = "anonymous";
      audioRef.current = el;
      setAudioEl(el);
    }
  }, []);

  const ensureAnalyser = useCallback(() => {
    if (analyserRef.current) return analyserRef.current;
    const audio = audioRef.current;
    if (!audio) return null;
    try {
      const Ctor =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      const ctx = new Ctor();
      const src = ctx.createMediaElementSource(audio);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.6;
      // Keep the audio audible by teeing through the analyser.
      src.connect(analyser);
      analyser.connect(ctx.destination);
      audioCtxRef.current = ctx;
      analyserRef.current = analyser;
      return analyser;
    } catch (e) {
      console.debug("Avi: analyser init failed (animations will be silent)", e);
      return null;
    }
  }, []);

  const startAmplitudeLoop = useCallback(() => {
    const analyser = analyserRef.current;
    if (!analyser) return;
    const buf = new Uint8Array(analyser.fftSize);
    const tick = () => {
      analyser.getByteTimeDomainData(buf);
      let sumSq = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sumSq += v * v;
      }
      const rms = Math.sqrt(sumSq / buf.length);
      // RMS of speech rarely exceeds ~0.3; scale for visual punch.
      const a = Math.min(1, rms * 3.2);
      amplitudeRef.current = a;
      setAmplitude(a);
      rafRef.current = requestAnimationFrame(tick);
    };
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    tick();
  }, []);

  const stopAmplitudeLoop = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    amplitudeRef.current = 0;
    setAmplitude(0);
  }, []);

  const playNext = useCallback(async () => {
    if (playingRef.current) return;
    const next = queueRef.current.shift();
    if (!next) return;
    if (mutedRef.current) {
      return playNext();
    }
    playingRef.current = true;
    try {
      const resp = await fetch(resolveApiPath("/api/aiassistant/tts"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: next.text,
          gender_hint: next.gender_hint ?? undefined,
        }),
      });
      if (!resp.ok) throw new Error(`TTS HTTP ${resp.status}`);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const audio = audioRef.current!;
      audio.src = url;
      audio.onended = () => {
        URL.revokeObjectURL(url);
        playingRef.current = false;
        setIsSpeaking(false);
        stopAmplitudeLoop();
        void playNext();
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        playingRef.current = false;
        setIsSpeaking(false);
        stopAmplitudeLoop();
        void playNext();
      };
      try {
        ensureAnalyser();
        // Resume the context if it was suspended (autoplay policy).
        if (audioCtxRef.current?.state === "suspended") {
          await audioCtxRef.current.resume().catch(() => undefined);
        }
        await audio.play();
        setIsSpeaking(true);
        startAmplitudeLoop();
      } catch (err) {
        // Autoplay was blocked — surface a hint in the console so the
        // user knows to interact with the page once.
        console.warn("Avi: audio playback blocked until first user gesture", err);
        playingRef.current = false;
        setIsSpeaking(false);
        stopAmplitudeLoop();
      }
    } catch (err) {
      console.debug("Avi TTS failed", err);
      playingRef.current = false;
      setIsSpeaking(false);
      stopAmplitudeLoop();
      void playNext();
    }
  }, [ensureAnalyser, startAmplitudeLoop, stopAmplitudeLoop]);

  const enqueue = useCallback(
    (text: string, opts?: { gender_hint?: string | null }) => {
      if (!text.trim()) return;
      tokenRef.current += 1;
      queueRef.current.push({
        text: text.trim(),
        gender_hint: opts?.gender_hint ?? null,
        token: tokenRef.current,
      });
      void playNext();
    },
    [playNext]
  );

  const setMuted = useCallback(
    (m: boolean) => {
      mutedRef.current = m;
      const audio = audioRef.current;
      if (audio && m) {
        try {
          audio.pause();
        } catch {
          /* noop */
        }
        queueRef.current = [];
        playingRef.current = false;
        setIsSpeaking(false);
        stopAmplitudeLoop();
      }
    },
    [stopAmplitudeLoop]
  );

  return { enqueue, setMuted, isSpeaking, amplitude, amplitudeRef, audioEl };
}

// ============================================================================
// Live session state
// ----------------------------------------------------------------------------
// The backend opens a `live_session` the first time we either recognize a
// face or post a chat message. This hook:
//   1. Pings `/sessions/ensure-active` on mount so a session always exists
//      while the page is open (and so the backend can sweep stale ones).
//   2. Polls `/sessions/active` every 5 s to surface new participants and
//      refreshed activity timestamps in the session pill.
// The returned `sessionId` is threaded into `/greet`, `/followup`, and
// `/chat` so the server can log the transcript and enforce "greet once
// per session" without the client having to track it.
// ============================================================================
function useLiveSession(familyId: number) {
  const enabled = Number.isFinite(familyId);
  const [session, setSession] = useState<LiveSession | null>(null);
  // Guard against tight retry loops if the backend is down: only one
  // ensure-active call may be in flight at a time, and we wait at
  // least 2s between attempts before re-triggering from the null-poll.
  const ensureInFlightRef = useRef(false);
  const lastEnsureAtRef = useRef(0);

  const ensureActive = useCallback(async () => {
    if (!enabled) return null;
    if (ensureInFlightRef.current) return null;
    if (Date.now() - lastEnsureAtRef.current < 2_000) return null;
    ensureInFlightRef.current = true;
    lastEnsureAtRef.current = Date.now();
    try {
      const s = await api.post<LiveSession>(
        "/api/aiassistant/sessions/ensure-active",
        { family_id: familyId, start_context: "page_opened" }
      );
      setSession(s);
      return s;
    } catch (err) {
      console.warn("[useLiveSession] ensure-active failed", err);
      return null;
    } finally {
      ensureInFlightRef.current = false;
    }
  }, [enabled, familyId]);

  useEffect(() => {
    void ensureActive();
  }, [ensureActive]);

  // Poll the lightweight `/active` endpoint; this is what keeps the
  // participant pill fresh as the camera identifies more people, and —
  // critically — what notices that the backend has no active session
  // (because the user just clicked "End & reset" or the 30-min idle
  // sweep ran). When that happens we open a fresh session inline so
  // the next greet/chat has somewhere to land.
  const { data: activeSession } = useQuery<LiveSession | null>({
    queryKey: ["ai-active-session", familyId],
    queryFn: async () =>
      api.get<LiveSession | null>(
        `/api/aiassistant/sessions/active?family_id=${familyId}`
      ),
    enabled,
    refetchInterval: 5_000,
  });

  useEffect(() => {
    if (activeSession) {
      // Don't overwrite the freshly-created session from ensureActive
      // with a stale ended one returned by an in-flight poll.
      if (activeSession.is_active || !session?.is_active) {
        setSession(activeSession);
      }
    } else if (activeSession === null) {
      // Server explicitly says "no active session" — clear local state
      // and open a new one so the next face-detect / chat can be
      // attributed correctly (with greeted_already=false for everyone).
      setSession(null);
      void ensureActive();
    }
    // `session` is intentionally omitted from deps to avoid re-running
    // the effect on every setSession from inside it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSession, ensureActive]);

  return session;
}

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

  const { data: ttsStatus } = useQuery<TtsStatus>({
    queryKey: ["ai-tts-status"],
    queryFn: () => api.get<TtsStatus>("/api/aiassistant/tts/status"),
    refetchInterval: 20_000,
  });

  // Speaker toggle — persisted across reloads so a family keeps their
  // chosen default. Start muted until the user clicks once, to stay on
  // the right side of browser autoplay policies.
  // Speaker (Avi's voice playback). We intentionally do NOT seed this
  // from localStorage — the family expects "Voice on" every time they
  // open the page, and a stale "0" from a previous testing session was
  // silently muting Avi on reload. The toggle still works mid-session
  // for a deliberate "shh"; it just doesn't survive a refresh.
  const [speakerOn, setSpeakerOn] = useState<boolean>(true);

  // Animation mode — both modes are "live" in the sense of being
  // animated in real time; the distinction is the *fidelity* of the
  // animation:
  //   - "basic"    → the Gemini-generated portrait with an SVG mouth
  //                  driven by audio amplitude. Fast, works everywhere.
  //   - "advanced" → the rigged Live2D character (Natori) with full
  //                  lip-sync, blink, hair physics, gestures. Heavier;
  //                  best on the Mac Studio.
  // Default is "basic" for now — Live2D is still finicky to set up and
  // most family members don't notice the difference. We honour an
  // explicit "advanced" pick from localStorage, but fall back to
  // "basic" otherwise (including the legacy "live" value some browsers
  // may still have stored from the previous build).
  type AnimationMode = "advanced" | "basic";
  const [animationMode, setAnimationMode] = useState<AnimationMode>(() => {
    const v = localStorage.getItem("avi:animationMode");
    return v === "advanced" ? "advanced" : "basic";
  });
  useEffect(() => {
    localStorage.setItem("avi:animationMode", animationMode);
  }, [animationMode]);

  const audio = useAudioQueue(!speakerOn);

  // Gender hint used to pick Kokoro's voice pack. Falls back to female
  // if the admin didn't set a gender on the assistant.
  const voiceGender = assistant?.gender === "male" ? "male" : "female";

  // Live2D character render state — surfaced as a header badge so you
  // can tell at a glance whether the rigged model is on stage or the
  // SVG fallback is doing the work.
  const [live2dState, setLive2dState] = useState<AviLive2DState>("init");
  const [live2dError, setLive2dError] = useState<string | null>(null);
  const onLive2DStateChange = useCallback(
    (s: AviLive2DState, err?: string | null) => {
      setLive2dState(s);
      setLive2dError(err ?? null);
    },
    []
  );

  const liveSession = useLiveSession(familyId);
  const liveSessionId = liveSession?.live_session_id ?? null;

  // Wave-hand flag. Flipped on for ~2.8 s whenever a new face triggers a
  // greeting so Avi visibly waves as he says "Hi".
  const [isWaving, setIsWaving] = useState(false);
  const waveTimeoutRef = useRef<number | null>(null);
  useEffect(() => {
    const handler = () => {
      setIsWaving(true);
      if (waveTimeoutRef.current) {
        window.clearTimeout(waveTimeoutRef.current);
      }
      waveTimeoutRef.current = window.setTimeout(
        () => setIsWaving(false),
        2800
      );
    };
    window.addEventListener("avi:greet", handler);
    return () => {
      window.removeEventListener("avi:greet", handler);
      if (waveTimeoutRef.current) {
        window.clearTimeout(waveTimeoutRef.current);
      }
    };
  }, []);

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
          <div className="flex items-center gap-2">
            {/* Animation mode — two-option segmented toggle. Both
                modes animate in real time; "Basic" is the SVG portrait
                with amplitude-driven mouth + smile, "Advanced" mounts
                the rigged Live2D character. */}
            <div
              className="inline-flex rounded-full border border-border overflow-hidden text-xs bg-white"
              role="tablist"
              aria-label="Animation mode"
            >
              <button
                type="button"
                role="tab"
                aria-selected={animationMode === "basic"}
                onClick={() => setAnimationMode("basic")}
                className={cn(
                  "inline-flex items-center gap-1 px-3 py-1 transition-colors",
                  animationMode === "basic"
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted"
                )}
                title="Lightweight SVG portrait with amplitude-driven mouth + smile. Works on every browser."
              >
                <Zap className="h-3.5 w-3.5" />
                Basic
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={animationMode === "advanced"}
                onClick={() => setAnimationMode("advanced")}
                className={cn(
                  "inline-flex items-center gap-1 px-3 py-1 transition-colors",
                  animationMode === "advanced"
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted"
                )}
                title="Rigged Live2D character — real lip-sync, blink, hair physics, gestures. Heavier, best on the Mac Studio."
              >
                <Sparkles className="h-3.5 w-3.5" />
                Advanced
              </button>
            </div>
            <button
              onClick={() => setSpeakerOn((s) => !s)}
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs border",
                speakerOn
                  ? "bg-primary/10 text-primary border-primary/30 hover:bg-primary/20"
                  : "bg-muted text-muted-foreground border-border hover:bg-muted/70"
              )}
              title={
                speakerOn
                  ? "Avi speaks out loud. Click to mute."
                  : "Avi is muted. Click to enable voice."
              }
            >
              {speakerOn ? (
                <Volume2 className="h-3.5 w-3.5" />
              ) : (
                <VolumeX className="h-3.5 w-3.5" />
              )}
              {speakerOn ? "Voice on" : "Voice off"}
            </button>
            <StatusBadges
              llm={llmStatus}
              face={faceStatus}
              tts={ttsStatus}
              live2d={live2dState}
              live2dError={live2dError}
              animationMode={animationMode}
            />
          </div>
        </div>
        <LiveSessionPill session={liveSession} familyId={familyId} />
      </header>

      <main className="max-w-7xl mx-auto p-6 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_400px] gap-6">
        <div className="flex flex-col gap-6">
          <AviStage
            assistant={assistant}
            assistantName={assistantName}
            isSpeaking={audio.isSpeaking}
            isWaving={isWaving}
            amplitude={audio.amplitude}
            amplitudeRef={audio.amplitudeRef}
            audioEl={audio.audioEl}
            onLive2DStateChange={onLive2DStateChange}
            animationMode={animationMode}
          />
          <LiveCameraPanel familyId={familyId} />
        </div>
        <ChatPanel
          familyId={familyId}
          assistantName={assistantName}
          llmStatus={llmStatus}
          audio={audio}
          voiceGender={voiceGender}
          liveSessionId={liveSessionId}
        />
      </main>
    </div>
  );
}

// ============================================================================
// Avi stage — the big animated avatar that occupies the top half of the
// left column. Composites four layers around the assistant's profile image:
//   1. A soft background glow whose intensity tracks audio amplitude.
//   2. Concentric "echo" rings that pulse outward while speaking.
//   3. The avatar itself, breathing idly and bobbing slightly when speaking.
//   4. A waving hand emoji that fires for a few seconds on greet.
// ============================================================================
function AviStage({
  assistant,
  assistantName,
  isSpeaking,
  isWaving,
  amplitude,
  amplitudeRef,
  audioEl,
  onLive2DStateChange,
  animationMode,
}: {
  assistant: Assistant | undefined;
  assistantName: string;
  isSpeaking: boolean;
  isWaving: boolean;
  amplitude: number;
  amplitudeRef: React.MutableRefObject<number>;
  audioEl: HTMLAudioElement | null;
  onLive2DStateChange?: (
    state: AviLive2DState,
    error?: string | null
  ) => void;
  animationMode: "advanced" | "basic";
}) {
  // When the user picks Basic we want the Avatar status badge to stop
  // claiming "Live2D loading…" (which is misleading) and the AviStage
  // to unmount the Pixi canvas entirely so we're not burning a GPU
  // context needlessly. Push a synthetic state through the callback so
  // the parent reflects the off state in the header.
  useEffect(() => {
    if (animationMode === "basic") {
      onLive2DStateChange?.("unavailable", null);
    }
    // Intentionally only re-notifies when the user flips the mode —
    // while in "advanced" mode, AviLive2D's own effect drives transitions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [animationMode]);
  // Clamp + ease the raw RMS so the glow doesn't feel jittery.
  const amp = Math.min(1, Math.max(0, amplitude));
  const glowPx = 30 + amp * 80;
  const glowAlpha = 0.15 + amp * 0.45;
  const hasImage = !!assistant?.profile_image_path;

  return (
    <div className="card overflow-hidden">
      <div
        className={cn(
          "relative flex items-center justify-center",
          "bg-gradient-to-br from-indigo-50 via-background to-fuchsia-50",
          "aspect-[16/10] lg:aspect-[5/3]"
        )}
      >
        {/* Ambient background glow — also visible behind the Live2D
            canvas, so the stage keeps its "alive" feel whether Avi is
            rendered as a rigged character or the static fallback. */}
        <div
          className="absolute inset-0 flex items-center justify-center pointer-events-none"
          aria-hidden="true"
        >
          <div
            className="rounded-full transition-[filter,opacity] duration-150"
            style={{
              width: "55%",
              height: "80%",
              background: `radial-gradient(circle, rgba(99,102,241,${glowAlpha}) 0%, rgba(99,102,241,0) 70%)`,
              filter: `blur(${glowPx}px)`,
            }}
          />
        </div>

        {/* Sonar echo rings — layered behind the character while speaking. */}
        {isSpeaking && (
          <div
            className="absolute inset-0 flex items-center justify-center pointer-events-none"
            aria-hidden="true"
          >
            {[0, 0.6, 1.2].map((delay) => (
              <span
                key={delay}
                className="absolute rounded-full border-2 border-primary/40 animate-avi-echo"
                style={{
                  width: "42%",
                  height: "72%",
                  animationDelay: `${delay}s`,
                }}
              />
            ))}
          </div>
        )}

        {/* Avi's character. In "advanced" mode we mount the rigged
            Live2D model with a StaticAvatarFallback behind it; if the
            model fails to load, AviLive2D shows the fallback
            automatically. In "basic" mode we skip Live2D entirely so
            the page is lightweight — just the Gemini portrait with the
            amplitude-driven SVG mouth overlay and CSS breathing/bobbing. */}
        <div className="absolute inset-0 z-10">
          {animationMode === "advanced" ? (
            <AviLive2D
              modelPath={AVI_LIVE2D_MODEL_PATH}
              modelFile={AVI_LIVE2D_MODEL_FILE}
              audioEl={audioEl}
              amplitudeRef={amplitudeRef}
              isSpeaking={isSpeaking}
              isWaving={isWaving}
              onStateChange={onLive2DStateChange}
              debug
              fallback={
                <StaticAvatarFallback
                  assistant={assistant}
                  assistantName={assistantName}
                  isSpeaking={isSpeaking}
                  isWaving={isWaving}
                  amplitude={amp}
                />
              }
            />
          ) : (
            <StaticAvatarFallback
              assistant={assistant}
              assistantName={assistantName}
              isSpeaking={isSpeaking}
              isWaving={isWaving}
              amplitude={amp}
            />
          )}
        </div>

        {/* Gemini-generated portrait badge, top-right. Admin can open
            the assistant edit page to regenerate it; on this live page
            it just provides a visual link between the admin console
            and the rigged character on stage. */}
        {hasImage && (
          <div className="absolute top-4 right-4 z-20">
            <div
              className="rounded-full overflow-hidden border-2 border-white shadow-lg bg-white/80 backdrop-blur"
              title="Gemini-generated portrait. Avi is animated with Live2D on this page."
              style={{ width: 64, height: 64 }}
            >
              <img
                src={`/api/media/${assistant!.profile_image_path}`}
                alt={`${assistantName} portrait`}
                className="w-full h-full object-cover"
              />
            </div>
          </div>
        )}

        {/* Bottom caption */}
        <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-20">
          <div className="inline-flex items-center gap-2 bg-white/90 backdrop-blur px-4 py-1.5 rounded-full shadow-sm text-sm">
            <Bot className="h-4 w-4 text-primary" />
            <span className="font-semibold">{assistantName}</span>
            <span
              className={cn(
                "text-xs transition-colors",
                isSpeaking ? "text-primary" : "text-muted-foreground"
              )}
            >
              {isSpeaking ? "speaking…" : "listening"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Original static-avatar presentation, extracted as a standalone block
 * so AviLive2D can use it as its loading/error fallback. Keeps all the
 * charming details — breathing, bobbing, mouth-pulse, waving hand —
 * exactly as they were before we introduced Live2D.
 */
function StaticAvatarFallback({
  assistant,
  assistantName,
  isSpeaking,
  isWaving,
  amplitude,
}: {
  assistant: Assistant | undefined;
  assistantName: string;
  isSpeaking: boolean;
  isWaving: boolean;
  amplitude: number;
}) {
  const amp = amplitude;
  const hasImage = !!assistant?.profile_image_path;

  return (
    <div
      className={cn(
        "relative w-full h-full flex items-center justify-center pointer-events-none"
      )}
    >
      <div
        className={cn(
          "relative animate-avi-breathe",
          isSpeaking && "animate-avi-bob"
        )}
        style={{
          filter: `drop-shadow(0 20px 40px rgba(0,0,0,0.18)) drop-shadow(0 0 ${
            20 + amp * 40
          }px rgba(99,102,241,${0.25 + amp * 0.4}))`,
        }}
      >
        {hasImage ? (
          <img
            src={`/api/media/${assistant!.profile_image_path}`}
            alt={assistant!.assistant_name}
            className="aspect-square rounded-full object-cover border-[5px] border-white shadow-xl"
            style={{
              width: "min(44vh, 360px)",
              height: "min(44vh, 360px)",
            }}
          />
        ) : (
          <div
            className="rounded-full bg-gradient-to-br from-primary/20 via-primary/10 to-transparent border-[5px] border-white shadow-xl flex flex-col items-center justify-center text-primary"
            style={{
              width: "min(44vh, 360px)",
              height: "min(44vh, 360px)",
            }}
          >
            <Bot className="h-24 w-24" />
            <div className="text-lg mt-1 font-semibold">
              {assistantName[0]?.toUpperCase() ?? "?"}
            </div>
          </div>
        )}

        {/* SVG mouth overlay. Sits over the portrait's own mouth, fades
            in when speaking or smiling, morphs shape with amplitude.

            Positioning strategy:
            • Prefer ``assistant.avatar_landmarks.mouth`` — the backend
              ran InsightFace on the Gemini portrait and returned mouth
              center + width as fractions of the image. This keeps the
              SVG aligned even when Gemini draws the face higher or
              lower than average.
            • Fall back to hard-coded "roughly 72% down, 22% wide"
              defaults when no face was detected (stylised portrait,
              Bot placeholder, detector not yet warm).

            Width multiplier ``* 1.0`` leaves the detected mouth box
            as-is; nudge higher if the smile pads need more horizontal
            room. */}
        {(() => {
          const lm = assistant?.avatar_landmarks?.mouth;
          const topPct = lm ? `${(lm.cy * 100).toFixed(2)}%` : "72%";
          const widthPct = lm ? `${Math.max(12, lm.w * 100).toFixed(2)}%` : "22%";
          const heightPct = lm ? `${Math.max(6, lm.h * 100).toFixed(2)}%` : "11%";
          const leftPct = lm ? `${(lm.cx * 100).toFixed(2)}%` : "50%";
          return (
            <div
              className="absolute pointer-events-none"
              style={{
                top: topPct,
                left: leftPct,
                width: widthPct,
                height: heightPct,
                transform: "translate(-50%, -50%)",
              }}
            >
              {/* Smile factor:
                    greeting → full smile (1.0)
                    speaking → gentle engaged smile (0.35)
                    idle     → no smile (0.0), overlay fades out entirely */}
              <SpeakingMouth
                amplitude={amp}
                smile={isWaving ? 1.0 : isSpeaking ? 0.35 : 0}
              />
            </div>
          );
        })()}

        {isWaving && (
          <div
            className="absolute text-7xl select-none pointer-events-none animate-avi-wave"
            style={{ right: "-6%", bottom: "4%" }}
            aria-hidden="true"
          >
            👋
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// Session pill
// ----------------------------------------------------------------------------
// Small, always-visible strip under the header that shows:
//   - the session start time (so the family knows "how long have we been
//     chatting with Avi?")
//   - the list of people Avi has recognised in this session
//   - a link to the session history (past conversations)
// ============================================================================
function LiveSessionPill({
  session,
  familyId,
}: {
  session: LiveSession | null;
  familyId: number;
}) {
  const qc = useQueryClient();
  const formatStart = (iso: string) => {
    const d = new Date(iso);
    // `HH:MM · 4/18` in local time — short enough to sit inline.
    return `${d.toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    })} · ${d.getMonth() + 1}/${d.getDate()}`;
  };

  const endMut = useMutation({
    mutationFn: () => {
      if (!session) throw new Error("no session");
      return api.post(`/api/aiassistant/sessions/${session.live_session_id}/end`, {
        end_reason: "manual",
      });
    },
    onSuccess: () => {
      // Force the hook's `/active` poll to re-run immediately so the
      // pill flips to "Opening session…" and ensure-active can create
      // a fresh one (with greeted_already=false for everyone).
      qc.invalidateQueries({ queryKey: ["ai-active-session", familyId] });
      qc.invalidateQueries({ queryKey: ["ai-sessions-list", familyId] });
    },
  });

  const participants = session?.participants_preview ?? [];
  const hasParticipants = participants.length > 0;

  return (
    <div className="bg-muted/30 border-t border-border">
      <div className="max-w-7xl mx-auto px-6 py-2 flex flex-wrap items-center gap-3 text-xs">
        <div
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1",
            session?.is_active
              ? "bg-emerald-500/15 text-emerald-700 border border-emerald-500/30"
              : "bg-muted text-muted-foreground border border-border"
          )}
          title={
            session
              ? `Session #${session.live_session_id} · ${
                  session.is_active ? "active" : `ended (${session.end_reason ?? "?"})`
                }`
              : "No active session"
          }
        >
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              session?.is_active ? "bg-emerald-500 animate-pulse" : "bg-muted-foreground/50"
            )}
          />
          {session
            ? session.is_active
              ? `Session · started ${formatStart(session.started_at)}`
              : "Session ended"
            : "Opening session…"}
        </div>
        <div className="inline-flex items-center gap-1.5 text-muted-foreground">
          <Users className="h-3.5 w-3.5" />
          {hasParticipants ? (
            <span className="font-medium text-foreground">
              {participants.join(", ")}
            </span>
          ) : (
            <span>Waiting for someone to be recognised…</span>
          )}
        </div>
        <div className="ml-auto flex items-center gap-3">
          {session?.is_active && (
            <button
              type="button"
              onClick={() => endMut.mutate()}
              disabled={endMut.isPending}
              className="inline-flex items-center gap-1 rounded-md border border-amber-500/40 bg-amber-50 px-2 py-0.5 text-amber-800 hover:bg-amber-100 disabled:opacity-60"
              title="Close this session. A new one will open on the next face detection or chat, with greetings reset for everyone."
            >
              <Square className="h-3 w-3" /> End & reset
            </button>
          )}
          <Link
            to={`/aiassistant/${familyId}/sessions`}
            className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
          >
            <History className="h-3.5 w-3.5" /> Session history
          </Link>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Status
// ============================================================================

function StatusBadges({
  llm,
  face,
  tts,
  live2d,
  live2dError,
  animationMode,
}: {
  llm: LlmStatus | undefined;
  face: FaceStatus | undefined;
  tts: TtsStatus | undefined;
  live2d: AviLive2DState;
  live2dError: string | null;
  animationMode: "advanced" | "basic";
}) {
  // When the user explicitly picks Basic we want a dedicated label
  // rather than re-using the "fallback" wording (which implies
  // something went wrong).
  const avatarLabel = (() => {
    if (animationMode === "basic") return "Avatar: basic";
    switch (live2d) {
      case "init":
        return "Avatar…";
      case "loading":
        return "Avatar loading…";
      case "ready":
        return "Avatar: advanced";
      case "unavailable":
      case "error":
        return "Avatar: SVG fallback";
    }
  })();
  const avatarHint = (() => {
    if (animationMode === "basic")
      return "Basic mode — lightweight Gemini portrait with SVG mouth lip-sync. Click Advanced in the header to switch to the rigged Live2D character.";
    switch (live2d) {
      case "init":
        return "Mounting the Live2D stage.";
      case "loading":
        return "Downloading Natori model files…";
      case "ready":
        return "Live2D Cubism 4 character is on stage with real lip-sync + gestures.";
      case "unavailable":
        return "Cubism Core runtime didn't load. Showing the animated SVG portrait — still has live lip-sync and smile.";
      case "error":
        return `Model failed to load${
          live2dError ? ` (${live2dError})` : ""
        }. Showing the animated SVG portrait.`;
    }
  })();
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
      <Badge
        ok={
          !!tts?.enabled && !!tts?.model_present && !!tts?.voices_present
        }
        warn={!!tts?.enabled && (!tts?.model_present || !tts?.voices_present)}
        label={
          tts === undefined
            ? "Voice…"
            : !tts.enabled
            ? "Voice off"
            : !tts.model_present || !tts.voices_present
            ? "Voice: download pending"
            : `Voice: ${tts.engine}`
        }
        title={
          tts === undefined
            ? undefined
            : tts.initialized
            ? `${tts.engine} ready.`
            : `${tts.engine} not yet loaded — first clip downloads ~330 MB.`
        }
      />
      <Badge
        ok={live2d === "ready" || animationMode === "basic"}
        warn={
          animationMode === "advanced" &&
          (live2d === "init" || live2d === "loading")
        }
        label={avatarLabel}
        title={avatarHint}
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

function LiveCameraPanel({ familyId }: { familyId: number }) {
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
        <div className="card-body py-2 px-4">
          <div className="text-xs text-muted-foreground">
            {cameraOn
              ? "Watching for family members. I'll wave and say hi when I recognize someone."
              : "Camera paused — recognition is idle."}
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
  audio,
  voiceGender,
  liveSessionId,
}: {
  familyId: number;
  assistantName: string;
  llmStatus: LlmStatus | undefined;
  audio: AudioQueueHandle;
  voiceGender: "male" | "female";
  liveSessionId: number | null;
}) {
  const toast = useToast();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [recognizedPersonId, setRecognizedPersonId] = useState<number | null>(
    null
  );
  // Mic defaults to ON every page load so the family can just walk up
  // and start talking — same "always on for now" pattern as the
  // speaker. We intentionally do NOT seed from localStorage; an old
  // "0" value from a previous testing session was the reason the mic
  // appeared dead on refresh. The toggle still works mid-session.
  // Note: the browser still requires a user gesture for the permission
  // prompt the first time, which happens automatically when the effect
  // below attempts to `rec.start()` on mount.
  const [listening, setListening] = useState<boolean>(true);
  const [speechSupported, setSpeechSupported] = useState(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Refs the speech-recognition handler relies on. `rec.onresult` is
  // bound exactly once per listening session, so anything it needs from
  // React state has to be mirrored into a ref that we keep in sync on
  // every render.
  const audioSpeakingRef = useRef(audio.isSpeaking);
  useEffect(() => {
    audioSpeakingRef.current = audio.isSpeaking;
  }, [audio.isSpeaking]);

  // Echo-cancellation guard. The browser SpeechRecognition engine
  // happily transcribes whatever the laptop speakers play back —
  // including Avi's own TTS — and reports the result a beat AFTER
  // playback has stopped. Gating onresult only on
  // `audio.isSpeaking` therefore loses the race: the audio was
  // captured during playback but processed afterward. We use this
  // ref as a "ignore any recognition events whose audio could have
  // overlapped Avi" wall clock, and pair it with a hard
  // stop()/start() of the recognizer (see the combined effect
  // below) so the engine doesn't even hear him in the first place.
  const wasSpeakingUntilRef = useRef(0);

  const isStreamingRef = useRef(false);
  useEffect(() => {
    isStreamingRef.current = isStreaming;
  }, [isStreaming]);

  // Assigned just after `sendUserMessage` is defined below.
  const sendUserMessageRef = useRef<(text: string) => void>(() => undefined);

  // Auto-scroll to bottom on every new chunk.
  useEffect(() => {
    const el = messagesRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages]);

  // Rehydrate the chat from the active session's persisted transcript.
  //
  // Why: navigating away from the live page (or refreshing) used to
  // leave you with an empty chat box even though the backend had the
  // full conversation logged. Now we fetch the session detail the
  // moment we know which active session we're attached to and replace
  // the local message list with what's on the server, so the user
  // picks up exactly where they left off.
  //
  // Hydration runs once per `liveSessionId` (a ref tracks which id we
  // already loaded). When the id changes — e.g. after "End & reset"
  // creates a fresh session — we wipe the chat and rehydrate from the
  // (empty) new session. We deliberately skip rehydration mid-stream
  // so we don't yank the streaming bubble out from under the user.
  const hydratedSessionIdRef = useRef<number | null>(null);
  useEffect(() => {
    if (liveSessionId === null) {
      hydratedSessionIdRef.current = null;
      return;
    }
    if (hydratedSessionIdRef.current === liveSessionId) return;
    if (isStreamingRef.current) return;
    let cancelled = false;
    (async () => {
      try {
        const detail = await api.get<LiveSessionDetail>(
          `/api/aiassistant/sessions/${liveSessionId}`
        );
        if (cancelled) return;
        hydratedSessionIdRef.current = liveSessionId;
        const restored: ChatMessage[] = detail.messages.map((m) => ({
          id: `restored-${m.live_session_message_id}`,
          role: m.role,
          content: m.content,
        }));
        setMessages(restored);
        // If anyone in the participants was greeted already, surface
        // the most recent one as our "recognized person" so a follow-up
        // chat carries the right context. Falls back to null when the
        // session has no participants yet.
        const greeted = detail.participants.find((p) => p.greeted_already);
        if (greeted) setRecognizedPersonId(greeted.person_id);
      } catch (err) {
        console.warn("[chat] failed to load session transcript", err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [liveSessionId]);

  // Detect speech recognition support (Chrome / Safari).
  useEffect(() => {
    const W = window as unknown as {
      SpeechRecognition?: unknown;
      webkitSpeechRecognition?: unknown;
    };
    setSpeechSupported(!!(W.SpeechRecognition || W.webkitSpeechRecognition));
  }, []);

  // Listen for face-recognition greetings fired by the camera loop.
  // We stash the greeter in a ref so the event handler always calls
  // the latest closure (audio queue, llmStatus, voiceGender can all
  // change during the session without re-subscribing).
  const triggerGreetRef = useRef<(id: number) => void>(() => undefined);
  useEffect(() => {
    const handler = (evt: Event) => {
      const detail = (evt as CustomEvent<GreetEventDetail>).detail;
      if (!detail) return;
      setRecognizedPersonId(detail.person_id);
      triggerGreetRef.current(detail.person_id);
    };
    window.addEventListener("avi:greet", handler as EventListener);
    return () =>
      window.removeEventListener("avi:greet", handler as EventListener);
  }, []);

  const appendMessage = useCallback((m: ChatMessage) => {
    setMessages((prev) => [...prev, m]);
  }, []);

  async function triggerGreet(personId: number) {
    // Phase 1 — instant template greeting. No LLM in this path, so it
    // returns in ~50 ms and we can ship the audio clip immediately.
    const greetingMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      streaming: true,
      meta: "recognized someone",
    };
    appendMessage(greetingMsg);

    let greetingText = "";
    try {
      const resp = await api.post<{
        greeting: string;
        skipped?: boolean;
        skipped_reason?: string | null;
        context_preview: string;
      }>("/api/aiassistant/greet", {
        family_id: familyId,
        person_id: personId,
        live_session_id: liveSessionId,
      });
      if (resp.skipped) {
        // Server says "already greeted in this session" — silently
        // drop the placeholder instead of announcing a suppressed
        // greeting. We also skip the follow-up question: if they've
        // been in the room with Avi already, we don't want to ambush
        // them again just because they reappeared on camera.
        console.info(
          `[greet] suppressed for person ${personId}: ${resp.skipped_reason ?? "already greeted"}`
        );
        setMessages((prev) => prev.filter((m) => m.id !== greetingMsg.id));
        return;
      }
      greetingText = resp.greeting;
      setMessages((prev) =>
        prev.map((m) =>
          m.id === greetingMsg.id
            ? { ...m, content: greetingText, streaming: false }
            : m
        )
      );
      audio.enqueue(greetingText, { gender_hint: voiceGender });
    } catch (e) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === greetingMsg.id
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
      return;
    }

    // Phase 2 — LLM-generated follow-up question. Runs in parallel with
    // the greeting audio so it's usually queued and ready to play the
    // moment the greeting finishes.
    if (!llmStatus?.available || !llmStatus.model_pulled) {
      return;
    }
    const followupMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      streaming: true,
      meta: "thinking of a follow-up…",
    };
    appendMessage(followupMsg);
    try {
      const resp = await api.post<{
        question: string;
        goal_name: string | null;
      }>("/api/aiassistant/followup", {
        family_id: familyId,
        person_id: personId,
        live_session_id: liveSessionId,
      });
      setMessages((prev) =>
        prev.map((m) =>
          m.id === followupMsg.id
            ? {
                ...m,
                content: resp.question,
                streaming: false,
                meta: resp.goal_name
                  ? `about your goal: ${resp.goal_name}`
                  : undefined,
              }
            : m
        )
      );
      audio.enqueue(resp.question, { gender_hint: voiceGender });
    } catch (e) {
      setMessages((prev) =>
        prev.filter((m) => m.id !== followupMsg.id)
      );
      console.debug("Followup failed", e);
    }
  }

  // Keep the ref pointed at the latest closure on every render.
  triggerGreetRef.current = triggerGreet;

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
          live_session_id: liveSessionId,
        }),
      });
      if (!res.ok || !res.body) {
        throw new Error(`HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let lastError: string | null = null;
      let assistantReply = "";
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
              assistantReply += delta;
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
      // Speak the assistant's reply out loud once the stream settles.
      // `enqueue` is a no-op when the speaker toggle is off, so the
      // user's mute preference is automatically respected. We send the
      // whole reply as a single chunk rather than per-token so Kokoro
      // gets a coherent sentence to inflect (and we don't pile up a
      // hundred tiny TTS requests for one answer).
      const speakable = assistantReply.trim();
      if (!lastError && speakable) {
        audio.enqueue(speakable, { gender_hint: voiceGender });
      }
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

  // Keep the recognition handler pointed at the latest closure so the
  // captured `messages` / `recognizedPersonId` / `liveSessionId` are
  // always fresh when a pause triggers an auto-submit.
  sendUserMessageRef.current = sendUserMessage;

  // ---- Microphone / Web Speech API --------------------------------------
  //
  // Design (deliberately simple):
  //   * Build ONE SpeechRecognition instance per page load — created
  //     in a mount-once effect that fires as soon as we know the
  //     browser supports it. Handlers are bound exactly once and read
  //     React state through refs, so we never tear down + rebuild on
  //     every render.
  //   * The `listening` toggle just calls `.start()` / `.stop()` on
  //     that single instance (separate effect below). No re-creation,
  //     no re-binding, no double-start race.
  //   * Pause-based auto-submit: the browser engine flips `isFinal`
  //     when it hears trailing silence (~700-1000 ms). That's our
  //     "natural pause" detector — no extra VAD library needed.
  //   * Echo suppression: anything captured while Avi is speaking is
  //     dropped on the floor (it's almost certainly his own voice
  //     coming back through the speakers).
  //   * Reconnect with backoff: continuous mode quietly ends every
  //     ~30-60 s on Chrome. We restart it after a short delay; on
  //     consecutive non-benign errors we back off exponentially so a
  //     stuck "not-allowed" can't spin into a toast-spam loop.
  //   * Errors toast at most ONCE per kind per page load.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const recRef = useRef<any | null>(null);
  // What the user *wants*: read by the recognizer's onend callback so
  // it knows whether to restart itself after each chunk.
  const shouldListenRef = useRef(listening);
  useEffect(() => {
    shouldListenRef.current = listening;
  }, [listening]);

  // Mount-once: create the recognizer the moment we detect support.
  useEffect(() => {
    if (!speechSupported) return;
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

    // Accumulates finalised fragments between auto-submits. An
    // utterance can produce more than one `isFinal` chunk in a row,
    // so we concatenate until the user pauses *and* Avi isn't
    // speaking — only then do we submit and clear.
    let pendingFinal = "";
    // Backoff bookkeeping for the auto-restart loop.
    let restartTimer: number | null = null;
    let consecutiveErrors = 0;
    const reportedErrors = new Set<string>();

    const scheduleRestart = () => {
      if (restartTimer !== null) return;
      if (!shouldListenRef.current) return;
      // 250 ms when healthy; exponential up to 15 s on errors.
      const delay =
        consecutiveErrors === 0
          ? 250
          : Math.min(15_000, 500 * 2 ** (consecutiveErrors - 1));
      restartTimer = window.setTimeout(() => {
        restartTimer = null;
        if (!shouldListenRef.current) return;
        try {
          rec.start();
        } catch {
          /* already started — ignore */
        }
      }, delay);
    };

    let running = false;
    rec.onstart = () => {
      running = true;
      consecutiveErrors = 0;
      console.info("[speech] recognizer started");
    };

    rec.onresult = (e: {
      resultIndex: number;
      results: {
        isFinal: boolean;
        [k: number]: { transcript: string };
      }[];
    }) => {
      // Belt-and-suspenders echo cancellation. The combined effect
      // below stops the recognizer during TTS playback so this
      // branch should rarely fire — but if Avi's voice still leaks
      // through (e.g. browser races a buffered result before our
      // stop() lands), drop it on the floor. The grace window also
      // catches results whose audio captured Avi's trailing
      // syllables right before he finished.
      if (
        audioSpeakingRef.current ||
        Date.now() < wasSpeakingUntilRef.current
      ) {
        pendingFinal = "";
        setInput("");
        return;
      }
      let interim = "";
      let sawFinal = false;
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) {
          pendingFinal += r[0].transcript;
          sawFinal = true;
        } else {
          interim += r[0].transcript;
        }
      }
      setInput((pendingFinal + " " + interim).trim());
      if (!sawFinal) return;
      if (isStreamingRef.current) return;
      const toSend = pendingFinal.trim();
      if (!toSend) return;
      pendingFinal = "";
      setInput("");
      sendUserMessageRef.current(toSend);
    };

    rec.onerror = (evt: { error?: string }) => {
      const code = evt?.error;
      // "no-speech" and "aborted" are normal in continuous mode.
      if (!code || code === "no-speech" || code === "aborted") return;
      consecutiveErrors += 1;
      console.warn(`[speech] error #${consecutiveErrors}:`, code);
      // Toast at most once per error kind for the lifetime of the page.
      if (!reportedErrors.has(code)) {
        reportedErrors.add(code);
        if (code === "not-allowed" || code === "service-not-allowed") {
          toast.error(
            "Microphone is blocked. Allow mic access in your browser's site settings, then click the mic button to retry."
          );
        } else if (code === "audio-capture") {
          toast.error("No microphone detected on this device.");
        } else {
          toast.error(`Mic recognition error: ${code}`);
        }
      }
    };

    rec.onend = () => {
      running = false;
      // Flush any tail still parked from a mid-sentence pause before
      // the engine ended (rare but possible on an engine-side timeout).
      const tail = pendingFinal.trim();
      const inEchoWindow =
        audioSpeakingRef.current ||
        Date.now() < wasSpeakingUntilRef.current;
      if (tail && !inEchoWindow && !isStreamingRef.current) {
        pendingFinal = "";
        setInput("");
        sendUserMessageRef.current(tail);
      }
      scheduleRestart();
    };

    // Single helper: attempt to start the recognizer iff the user
    // wants to listen and we're not already running. Logs both the
    // success path (via `onstart`) and any synchronous throw so we
    // can debug "I never saw the prompt" reports from devtools.
    const tryStart = (origin: string) => {
      if (!shouldListenRef.current) return;
      if (running) return;
      try {
        rec.start();
        console.info(`[speech] start() called from ${origin}`);
      } catch (err) {
        console.warn(`[speech] start() threw from ${origin}:`, err);
      }
    };

    recRef.current = rec;

    // Best-effort autostart on mount. In modern Chrome this works
    // when mic permission is already "granted"; if it's "prompt" or
    // gesture-gated, the call is silently rejected (no onerror, no
    // prompt) and we fall back to the first-click listener below.
    tryStart("mount");

    // Permission-state-aware bootstrap. If we can read the permission
    // we log it for debugging, and if it's currently "prompt" we wait
    // for the state to flip to "granted" (e.g., user grants via the
    // address-bar prompt) and start automatically.
    let permStatus: PermissionStatus | null = null;
    const onPermChange = () => {
      console.info(`[speech] mic permission changed → ${permStatus?.state}`);
      if (permStatus?.state === "granted") tryStart("perm-grant");
    };
    (async () => {
      try {
        // The TS dom typings only know a few PermissionName values;
        // "microphone" is valid in Chrome/Edge/Safari but flagged.
        const status = await navigator.permissions.query({
          name: "microphone" as PermissionName,
        });
        permStatus = status;
        console.info(`[speech] mic permission state: ${status.state}`);
        if (status.state === "granted") tryStart("perm-query");
        status.addEventListener("change", onPermChange);
      } catch (err) {
        console.debug("[speech] permission query unsupported", err);
      }
    })();

    // Safety net: the FIRST user interaction with the page is a
    // guaranteed user gesture. If autostart was silently refused we
    // try again here. Any subsequent clicks are no-ops because
    // `running` is true.
    const onFirstGesture = () => {
      tryStart("first-gesture");
    };
    document.addEventListener("pointerdown", onFirstGesture, { once: false });
    document.addEventListener("keydown", onFirstGesture, { once: false });

    return () => {
      document.removeEventListener("pointerdown", onFirstGesture);
      document.removeEventListener("keydown", onFirstGesture);
      if (permStatus) permStatus.removeEventListener("change", onPermChange);
      if (restartTimer !== null) {
        clearTimeout(restartTimer);
        restartTimer = null;
      }
      try {
        rec.onend = null;
        rec.stop();
      } catch {
        /* noop */
      }
      recRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speechSupported]);

  // Drive the existing recognizer from BOTH the user's listening
  // toggle AND Avi's playback state. No tear-down / rebuild — just
  // `.start()` / `.stop()` on the singleton recognizer.
  //
  // Echo-cancellation contract:
  //   * Avi starts speaking → stop the mic immediately, clear any
  //     interim transcript that's already in the form input, and
  //     remember a wall-clock until-time we should stay quiet past
  //     (= now + ECHO_GRACE_MS). The browser's mic buffer + the
  //     SpeechRecognition engine's own lookback both need a beat to
  //     drain after the speakers go silent; jumping back in too
  //     early is what was causing Avi's tail words to land in the
  //     user-message log.
  //   * Avi stops speaking → wait the remainder of the grace window,
  //     then re-arm the mic only if the user still wants to listen.
  //   * User toggles listening off → stop and stay stopped.
  useEffect(() => {
    const rec = recRef.current;
    if (!rec) return;

    const stop = () => {
      try {
        rec.stop();
      } catch {
        /* already stopped — ignore */
      }
    };
    const start = () => {
      if (!shouldListenRef.current) return;
      try {
        rec.start();
      } catch {
        /* already started — ignore */
      }
    };

    if (audio.isSpeaking) {
      wasSpeakingUntilRef.current = Date.now() + ECHO_GRACE_MS;
      setInput("");
      stop();
      console.info("[speech] paused for TTS playback");
      return;
    }

    if (!listening) {
      stop();
      console.info("[speech] stopped (listening toggled off)");
      return;
    }

    const remaining = Math.max(
      0,
      wasSpeakingUntilRef.current - Date.now()
    );
    if (remaining === 0) {
      start();
      return;
    }
    console.info(`[speech] re-arming mic in ${remaining} ms (echo grace)`);
    const t = window.setTimeout(start, remaining);
    return () => window.clearTimeout(t);
  }, [listening, audio.isSpeaking]);

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
