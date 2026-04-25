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
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  Bot,
  Camera,
  CheckCircle2,
  Database,
  History,
  Loader2,
  Mail,
  Mic,
  MicOff,
  RefreshCw,
  Search,
  Send,
  SkipForward,
  Sparkles,
  Square,
  User,
  Users,
  Video,
  VideoOff,
  Volume2,
  VolumeX,
} from "lucide-react";
import { api, resolveApiPath } from "@/lib/api";
import type {
  AgentStepView,
  Assistant,
  Family,
  LiveSession,
  LiveSessionDetail,
} from "@/lib/types";
import { useToast } from "@/components/Toast";
import { UserPill } from "@/components/UserPill";
import { useAuth } from "@/lib/auth";
import { useIsMobile } from "@/lib/useIsMobile";
import { cn } from "@/lib/cn";
import SpeakingMouth from "./SpeakingMouth";
import {
  useLocalFaceWatcher,
  type LocalFaceWatcherStatus,
  type LocalTrack,
} from "@/lib/localFaceWatcher";

/**
 * Live AI assistant page.
 *
 * Three subsystems share this screen:
 *
 *  1. **Camera** — continuous webcam preview. A *local* MediaPipe
 *     BlazeFace detector (in `lib/localFaceWatcher.ts`) runs at
 *     ~4 Hz on the video element and only escalates to the backend's
 *     heavyweight `/api/aiassistant/face/recognize` (InsightFace +
 *     ArcFace) when a brand-new face track is born. This frees the
 *     Mac's CoreML / ANE for chat + agent tools instead of burning
 *     them on a 2.5 s polling loop that fired even with nobody on
 *     screen.
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

type RecognizeCandidate = {
  person_id: number;
  person_name: string | null;
  similarity: number;
};

type RecognizeResponse = {
  matched: boolean;
  person_id: number | null;
  person_name: string | null;
  similarity: number | null;
  threshold: number;
  reason: string | null;
  // Best person in the gallery for this frame even if below threshold,
  // so the camera badge can show "almost recognized X" instead of going
  // silent when the user is just under the cut-off.
  top_candidate?: RecognizeCandidate | null;
};

type ChatRole = "user" | "assistant" | "system";
type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  streaming?: boolean;
  meta?: string;
  taskId?: number | null;
  steps?: AgentStepView[];
  // Fast-ack from the lightweight model. Populated by the SSE
  // `fast_ack` event when the heavy agent hasn't started streaming
  // text within AI_FAST_ACK_AFTER_SECONDS. Rendered as a transient
  // placeholder bubble so the user sees Avi acknowledge them within
  // ~1s instead of staring at "thinking…" for 10-30s. Replaced by
  // the real reply the moment `content` starts populating.
  fastAck?: string | null;
};

// Backend-only fallback: how often we re-poll `/face/recognize` when
// the in-browser MediaPipe detector failed to load (e.g. WASM blocked
// by CSP, model file missing, very old browser). The local watcher
// path uses *track events* instead of a wall-clock timer and so has
// no equivalent constant — it only calls the backend when a brand-new
// face appears on camera.
const FACE_RECOG_FALLBACK_INTERVAL_MS = 2500;

// How often the unknown-track re-probe loop runs while the local
// watcher is healthy. We keep this slower than the error-fallback
// interval (above) because we only need to give borderline matches a
// second chance — the optimization rationale for gating on track
// births still holds. Picked at 8s so a stranger doesn't generate
// constant backend load, while a near-miss family member gets ~7
// retries per minute instead of waiting until they leave + return.
const FACE_RECOG_UNKNOWN_RETRY_INTERVAL_MS = 8000;

// When the per-track POST returns "no_face_in_frame" or
// "below_threshold" — typically because MediaPipe gave us a tight
// crop that InsightFace's own detector couldn't lock onto — fire
// ONE whole-frame recognize this many ms later, instead of waiting
// for the next FACE_RECOG_UNKNOWN_RETRY_INTERVAL_MS tick. Without
// this, the worst-case greet latency for a known family member
// is ~8 s + backend round trip; with it the worst-case drops to
// roughly 1 s + 2 backend round trips. The 400 ms breathing room
// lets the user settle into frame (lighting / pose) so we don't
// burn the second round trip on essentially the same image.
const FACE_RECOG_FIRST_MISS_RETRY_DELAY_MS = 400;

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

// End-of-utterance grace window. The browser's SpeechRecognition flips
// `isFinal` after ~700-1000 ms of trailing silence — fast enough to feel
// snappy in dictation, but too aggressive for natural conversation where
// people pause mid-sentence to think. We wait an additional
// SUBMIT_DEBOUNCE_MS after the *last* final or interim result before
// actually sending; any new audio (interim OR final) cancels and restarts
// the timer. Net effect: a continuous talker submits ~1.8 s after they
// genuinely stop, while back-to-back final chunks accumulate into one
// message instead of firing N separate sends.
const SUBMIT_DEBOUNCE_MS = 1800;

// -------------------------------------------------------------------------
// Audio queue — plays Kokoro WAV clips one after another without overlap.
// Returns a handle with `enqueue(text, voiceHint)` and `mute`/`unmute`.
// We keep a single <audio> element and an array of pending blob URLs so
// the "instant greeting" and the "LLM follow-up" can be fired in parallel
// but played sequentially.
// -------------------------------------------------------------------------
type AudioQueueHandle = {
  enqueue: (
    text: string,
    opts?: { gender_hint?: string | null; messageId?: string }
  ) => void;
  setMuted: (muted: boolean) => void;
  // True while a WAV is actively playing out of the speakers.
  isSpeaking: boolean;
  // Real-time 0..1 audio amplitude (RMS). Drives Avi's speaking
  // animations — aura intensity, echo rings, mouth glow. The ref is
  // updated on every rAF tick for ~60 Hz consumers (the SVG mouth
  // overlay sized off it); `amplitude` is the React state mirror for
  // CSS-driven effects.
  amplitude: number;
  amplitudeRef: React.MutableRefObject<number>;
  // The single HTMLAudioElement we play Kokoro clips through. Exposed
  // so any future render-side consumer can subscribe to play/pause
  // events.
  audioEl: HTMLAudioElement | null;
  // Stable id of the chat message currently being read aloud (passed
  // through from `enqueue`). The chat UI uses this to render a
  // "Skip" button on exactly the bubble Avi is reading right now.
  // Null whenever nothing is playing.
  currentMessageId: string | null;
  // Stop reading the currently-playing clip immediately. Does NOT
  // drop later items in the queue (greet → followup chains still
  // play through), so "skip the long calendar dump" doesn't also
  // silence the next conversational turn.
  skipCurrent: () => void;
};

function useAudioQueue(
  muted: boolean,
  // When true, skip the WebAudio analyser + 60 Hz rAF amplitude loop
  // entirely. Mobile browsers (iOS Safari especially) get unhappy
  // when an `<audio>` element is teed through ``createMediaElement
  // Source`` *and* the page is also driving the microphone via
  // SpeechRecognition — symptoms range from stuttering playback to
  // the audio element silently dropping its stream and having to
  // be re-initialised. The cosmetic glow / mouth-amplitude effects
  // aren't worth the regression on a phone, so we just play the
  // bare audio element and leave ``amplitude`` at 0.
  skipAnalyser = false,
): AudioQueueHandle {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<
    Array<{
      text: string;
      gender_hint?: string | null;
      token: number;
      messageId?: string;
    }>
  >([]);
  const playingRef = useRef(false);
  const tokenRef = useRef(0);
  const mutedRef = useRef(muted);
  mutedRef.current = muted;
  // Bumped every time the user clicks Skip on a clip that's already
  // mid-fetch from /tts. The fetch resolver checks the captured
  // token against this ref and aborts the play if they don't match,
  // so a tiny network delay can't cause a "skipped" clip to start
  // talking a moment later.
  const skipTokenRef = useRef(0);

  const [isSpeaking, setIsSpeaking] = useState(false);
  const [currentMessageId, setCurrentMessageId] = useState<string | null>(
    null
  );
  const [amplitude, setAmplitude] = useState(0);
  const amplitudeRef = useRef(0);
  // Mirror the audio element as React state so any consumer can re-run
  // effects when it becomes available on the first render.
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
    if (skipAnalyser) return null;
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
  }, [skipAnalyser]);

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
    // Snapshot the skip generation when this clip is dispatched. If
    // the user clicks Skip while we're mid-fetch the ref will have
    // moved on — we then quietly drop the result instead of letting
    // it start talking a beat after the click.
    const startedAtSkipToken = skipTokenRef.current;
    setCurrentMessageId(next.messageId ?? null);
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
      if (startedAtSkipToken !== skipTokenRef.current) {
        // Skipped while we were waiting on /tts — toss the audio.
        playingRef.current = false;
        setIsSpeaking(false);
        setCurrentMessageId(null);
        stopAmplitudeLoop();
        void playNext();
        return;
      }
      const url = URL.createObjectURL(blob);
      const audio = audioRef.current!;
      audio.src = url;
      audio.onended = () => {
        URL.revokeObjectURL(url);
        playingRef.current = false;
        setIsSpeaking(false);
        setCurrentMessageId(null);
        stopAmplitudeLoop();
        void playNext();
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        playingRef.current = false;
        setIsSpeaking(false);
        setCurrentMessageId(null);
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
        setCurrentMessageId(null);
        stopAmplitudeLoop();
      }
    } catch (err) {
      console.debug("Avi TTS failed", err);
      playingRef.current = false;
      setIsSpeaking(false);
      setCurrentMessageId(null);
      stopAmplitudeLoop();
      void playNext();
    }
  }, [ensureAnalyser, startAmplitudeLoop, stopAmplitudeLoop]);

  const enqueue = useCallback(
    (
      text: string,
      opts?: { gender_hint?: string | null; messageId?: string }
    ) => {
      if (!text.trim()) return;
      tokenRef.current += 1;
      queueRef.current.push({
        text: text.trim(),
        gender_hint: opts?.gender_hint ?? null,
        token: tokenRef.current,
        messageId: opts?.messageId,
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
        setCurrentMessageId(null);
        stopAmplitudeLoop();
      }
    },
    [stopAmplitudeLoop]
  );

  // Stop reading the current clip but keep the rest of the queue.
  // We bump skipTokenRef so any in-flight /tts fetch (clip not yet
  // started playing) bails when it resolves; we also pause the
  // <audio> element so a clip already mid-utterance goes silent
  // immediately. Either way `audio.onended` won't fire — so we
  // manually advance to the next item.
  const skipCurrent = useCallback(() => {
    if (!playingRef.current) return;
    skipTokenRef.current += 1;
    const audio = audioRef.current;
    if (audio) {
      try {
        audio.pause();
        audio.currentTime = 0;
      } catch {
        /* noop */
      }
      // Detach handlers so they can't fire after we've advanced.
      audio.onended = null;
      audio.onerror = null;
    }
    playingRef.current = false;
    setIsSpeaking(false);
    setCurrentMessageId(null);
    stopAmplitudeLoop();
    void playNext();
  }, [playNext, stopAmplitudeLoop]);

  return {
    enqueue,
    setMuted,
    isSpeaking,
    amplitude,
    amplitudeRef,
    audioEl,
    currentMessageId,
    skipCurrent,
  };
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
  const { isAdmin } = useAuth();
  // Drives both the "voice / mic default OFF" UX policy AND the
  // mobile-only audio-pipeline simplifications (no WebAudio analyser,
  // no rAF amplitude loop). See useIsMobile + useAudioQueue.
  const isMobile = useIsMobile();

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

  // Pre-warm both Gemma models the moment the live page mounts and
  // again whenever the tab is foregrounded after being hidden. The
  // backend's lifespan task warms them at process start, but Ollama
  // unloads anything idle longer than its keep_alive window
  // (default 5 min, we pin to 1 h). Without this nudge, the first
  // chat after lunch eats the 3–4 s cold-load cost again — exactly
  // the failure mode the live-chat fast-ack is designed to avoid.
  //
  // Best-effort: errors are swallowed so a momentarily-down Ollama
  // never breaks page mount. The endpoint itself is idempotent and
  // cheap (one ``num_predict=1`` ping per model).
  useEffect(() => {
    let cancelled = false;
    const ping = () => {
      void api
        .post<unknown>("/api/aiassistant/warmup", {})
        .then((res) => {
          if (cancelled) return;
          // Surface the outcome in the console so we can see when
          // a model wasn't pulled / Ollama was down on dev boxes.
          // eslint-disable-next-line no-console
          console.debug("[avi] warmup", res);
        })
        .catch(() => {
          /* swallow — warmup is best-effort */
        });
    };
    ping();
    const onVisible = () => {
      if (document.visibilityState === "visible") ping();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  // Speaker (Avi's voice playback).
  //
  // Defaults are PLATFORM-AWARE, not persisted:
  //
  //   * Desktop  → ON. The family's home setup is a kitchen iMac with
  //                speakers — they expect to walk up and hear Avi greet
  //                them without clicking anything.
  //   * Mobile   → OFF. Phones are used in quiet/private contexts
  //                (bed, couch, school pickup line) where blasting
  //                "Hi Sam!" is the wrong default. Also mitigates the
  //                mobile-only TTS jitter where iOS' audio routing
  //                kept restarting the clip when SpeechRecognition
  //                also wanted the audio session.
  //
  // We deliberately do NOT seed from localStorage in either case — a
  // stale "0" from a previous testing session was silently muting
  // Avi on reload, and on a freshly-opened tab the platform-aware
  // default is what the user actually wants. The toggle still works
  // mid-session for a deliberate "shh" / "talk to me"; it just
  // doesn't survive a refresh.
  const [speakerOn, setSpeakerOn] = useState<boolean>(!isMobile);

  // The page used to support a second "advanced" rendering mode that
  // mounted a rigged Live2D character. It was pulled because the basic
  // SVG-portrait avatar is doing the job well and the Live2D path
  // brought a 200 KB Cubism runtime, ~1 MB of model assets, and a
  // brittle Pixi.js dependency for marginal gain.
  useEffect(() => {
    localStorage.removeItem("avi:animationMode");
  }, []);

  const audio = useAudioQueue(!speakerOn, isMobile);

  // Gender hint used to pick Kokoro's voice pack. Falls back to female
  // if the admin didn't set a gender on the assistant.
  const voiceGender = assistant?.gender === "male" ? "male" : "female";

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
        {/* Top row: back link, family + assistant title, UserPill.
            Padding shrinks on mobile so the title + user pill fit on
            phones without the pill scrolling off the right edge. */}
        <div className="max-w-7xl mx-auto px-3 sm:px-6 py-3 sm:py-4 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 sm:gap-4 min-w-0 flex-1">
            <Link
              to={`/admin/families/${familyId}`}
              className="text-sm text-muted-foreground hover:text-foreground inline-flex items-center gap-1 shrink-0"
              aria-label="Back to overview"
              title="Back to overview"
            >
              <ArrowLeft className="h-4 w-4" />
              <span className="hidden sm:inline">Back to overview</span>
            </Link>
            <div className="min-w-0">
              <div className="text-[10px] sm:text-xs text-muted-foreground uppercase tracking-wide truncate">
                {family?.family_name ?? "—"} · Live Assistant
              </div>
              <div className="font-semibold text-base sm:text-lg flex items-center gap-2 min-w-0">
                <Bot className="h-5 w-5 text-primary shrink-0" />
                <span className="truncate">{assistantName}</span>
              </div>
            </div>
          </div>
          <UserPill compact className="shrink-0" />
        </div>
        {/* Secondary row: voice toggle + status badges. On phones it
            scrolls horizontally so the badges can keep their fixed
            widths instead of forcing the page to grow. */}
        <div className="max-w-7xl mx-auto px-3 sm:px-6 pb-2 flex items-center gap-2 overflow-x-auto no-scrollbar">
          <button
            onClick={() => setSpeakerOn((s) => !s)}
            className={cn(
              "inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs border shrink-0",
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
          {/* Operational diagnostics (Ollama health, face embedding
              count, TTS engine, "Re-enroll faces" admin tool). They're
              busy and only meaningful to maintainers, so members get
              the cleaner header. */}
          {isAdmin && (
            <StatusBadges
              llm={llmStatus}
              face={faceStatus}
              tts={ttsStatus}
              familyId={familyId}
            />
          )}
        </div>
        <LiveSessionPill session={liveSession} familyId={familyId} />
      </header>

      {/* Grid items default to ``align-items: stretch``, so giving the
          right-hand cell ``min-h-0`` lets the chat panel match the
          left column's natural height (Avi + camera) and scroll its
          message list internally rather than push the row taller. */}
      <main className="max-w-7xl mx-auto p-3 sm:p-6 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_400px] gap-4 sm:gap-6 lg:items-stretch">
        <div className="flex flex-col gap-3 sm:gap-6 min-h-0">
          <AviStage
            assistant={assistant}
            assistantName={assistantName}
            isSpeaking={audio.isSpeaking}
            isWaving={isWaving}
            amplitude={audio.amplitude}
            isMobile={isMobile}
          />
          <LiveCameraPanel familyId={familyId} liveSessionId={liveSessionId} />
        </div>
        <ChatPanel
          familyId={familyId}
          assistantName={assistantName}
          llmStatus={llmStatus}
          audio={audio}
          voiceGender={voiceGender}
          liveSessionId={liveSessionId}
          isMobile={isMobile}
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
  isMobile,
}: {
  assistant: Assistant | undefined;
  assistantName: string;
  isSpeaking: boolean;
  isWaving: boolean;
  amplitude: number;
  // On mobile we collapse Avi into a short banner (~120px tall) so the
  // camera + chat both stay visible above the fold. On desktop we keep
  // the cinematic 5:3 portrait stage.
  isMobile: boolean;
}) {
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
          // Mobile: a slim banner so the camera + chat both stay
          // on screen without scrolling. Desktop: the original
          // cinematic stage.
          isMobile
            ? "h-[140px]"
            : "aspect-[16/10] lg:aspect-[5/3]"
        )}
      >
        {/* Ambient background glow — keeps the stage feeling "alive"
            even when Avi is silent. */}
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

        {/* Avi's character — the Gemini portrait with the amplitude-
            driven SVG mouth overlay and CSS breathing/bobbing. */}
        <div className="absolute inset-0 z-10">
          <StaticAvatarFallback
            assistant={assistant}
            assistantName={assistantName}
            isSpeaking={isSpeaking}
            isWaving={isWaving}
            amplitude={amp}
            isMobile={isMobile}
          />
        </div>

        {/* Gemini-generated portrait badge, top-right. Admin can open
            the assistant edit page to regenerate the image; on this
            live page it just sits as a small reminder of the canonical
            avatar source. Hidden on mobile — the banner is too short
            to fit two avatar circles without overlap. */}
        {hasImage && !isMobile && (
          <div className="absolute top-4 right-4 z-20">
            <div
              className="rounded-full overflow-hidden border-2 border-white shadow-lg bg-white/80 backdrop-blur"
              title="Gemini-generated portrait."
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

        {/* Bottom caption — desktop centres a chip; mobile pins a
            compact name + status to the right of the small avatar so
            the banner doesn't waste vertical space stacking the two. */}
        {isMobile ? (
          <div className="absolute inset-y-0 right-3 z-20 flex flex-col justify-center gap-0.5 text-right">
            <div className="font-semibold text-sm flex items-center justify-end gap-1.5">
              <Bot className="h-4 w-4 text-primary" />
              {assistantName}
            </div>
            <div
              className={cn(
                "text-[11px] transition-colors",
                isSpeaking ? "text-primary" : "text-muted-foreground"
              )}
            >
              {isSpeaking ? "speaking…" : "listening"}
            </div>
          </div>
        ) : (
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
        )}
      </div>
    </div>
  );
}

/**
 * Avi's avatar: the Gemini-generated portrait with breathing, bobbing,
 * an amplitude-driven SVG mouth overlay, and a waving-hand emoji on
 * greet. Lightweight (no canvas, no WebGL) and works everywhere —
 * which is why we replaced the experimental rigged Live2D path that
 * used to mount over it.
 */
function StaticAvatarFallback({
  assistant,
  assistantName,
  isSpeaking,
  isWaving,
  amplitude,
  isMobile,
}: {
  assistant: Assistant | undefined;
  assistantName: string;
  isSpeaking: boolean;
  isWaving: boolean;
  amplitude: number;
  isMobile: boolean;
}) {
  const amp = amplitude;
  const hasImage = !!assistant?.profile_image_path;
  // Mobile: ~110 px (fits the 140 px banner). Desktop: original sizing.
  const avatarSize = isMobile ? "110px" : "min(44vh, 360px)";
  // Mobile aligns the avatar left so the caption + status chip we
  // pin to the right have room. Desktop keeps the centred portrait.
  const containerJustify = isMobile ? "justify-start pl-4" : "justify-center";

  return (
    <div
      className={cn(
        "relative w-full h-full flex items-center pointer-events-none",
        containerJustify
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
            className={cn(
              "aspect-square rounded-full object-cover border-white shadow-xl",
              isMobile ? "border-[3px]" : "border-[5px]"
            )}
            style={{
              width: avatarSize,
              height: avatarSize,
            }}
          />
        ) : (
          <div
            className={cn(
              "rounded-full bg-gradient-to-br from-primary/20 via-primary/10 to-transparent border-white shadow-xl flex flex-col items-center justify-center text-primary",
              isMobile ? "border-[3px]" : "border-[5px]"
            )}
            style={{
              width: avatarSize,
              height: avatarSize,
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
          <Link
            to={`/aiassistant/${familyId}/agent-tasks`}
            className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
          >
            <Sparkles className="h-3.5 w-3.5" /> Agent tasks
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
  familyId,
}: {
  llm: LlmStatus | undefined;
  face: FaceStatus | undefined;
  tts: TtsStatus | undefined;
  familyId: number;
}) {
  const qc = useQueryClient();
  const { ok: toastOk, err: toastErr } = useToast();
  // Recompute every face_embedding row from the family's recognition-flagged
  // person_photos. Cheap on a warm InsightFace worker (~100–300 ms / photo)
  // but the very first call can take 5–15 s while the model compiles its
  // ONNX graph. Useful after disaster-recovery rebuilds (where photos are
  // restored from the filesystem but embeddings are lost) or whenever a
  // person's reference photos changed enough to affect matching.
  const enroll = useMutation({
    mutationFn: () =>
      api.post<{
        enrolled: number;
        skipped_unchanged: number;
        skipped_no_face: number;
        errors: number;
      }>(`/api/aiassistant/face/enroll?family_id=${familyId}`, {}),
    onSuccess: (r) => {
      const parts = [`${r.enrolled} embedded`];
      if (r.skipped_unchanged) parts.push(`${r.skipped_unchanged} unchanged`);
      if (r.skipped_no_face) parts.push(`${r.skipped_no_face} no face`);
      if (r.errors) parts.push(`${r.errors} errors`);
      toastOk(`Face enrollment: ${parts.join(", ")}.`);
      qc.invalidateQueries({ queryKey: ["ai-face-status", familyId] });
    },
    onError: (e: Error) => toastErr(e.message),
  });
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
      <button
        type="button"
        onClick={() => enroll.mutate()}
        disabled={enroll.isPending || !Number.isFinite(familyId)}
        className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-1.5 py-0.5 text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted disabled:opacity-60"
        title="Re-extract face embeddings for every recognition-flagged photo. Run this after restoring data from a backup or after adding/swapping reference photos."
      >
        <RefreshCw
          className={cn("h-3 w-3", enroll.isPending && "animate-spin")}
        />
        {enroll.isPending ? "Enrolling…" : "Re-enroll"}
      </button>
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
  liveSessionId,
}: {
  familyId: number;
  // Threaded down so the recognition loop can flush its per-person
  // greet-suppression map whenever a new session begins (after End &
  // reset, the 30-min idle sweep, etc.). Without this the client's
  // 90-second suppression silently swallows the first greet attempt
  // of every fresh session, leaving the user staring at a quiet page.
  liveSessionId: number | null;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [cameraOn, setCameraOn] = useState(true);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [lastRecognition, setLastRecognition] =
    useState<RecognizeResponse | null>(null);
  // Surface the in-browser detector's state in the camera badge so
  // we can tell at a glance whether we're in the efficient path
  // ("Local: 1 face") or the fallback path ("Backend-only").
  const [watcherStatus, setWatcherStatus] =
    useState<LocalFaceWatcherStatus>({ kind: "loading" });
  // Manual "Identify me" button state — separate from the
  // background recognition loop so the button can show a spinner
  // independent of any in-flight automatic /recognize.
  const [forceCheckInflight, setForceCheckInflight] = useState(false);
  const [forceCheckError, setForceCheckError] = useState<string | null>(null);

  // Manual override: snapshot the whole video frame and POST it to
  // /face/recognize directly. Bypasses both the local MediaPipe
  // detector AND the FaceRecognitionDriver's per-person 90s
  // greet-suppression — the user explicitly asked, so honour it.
  //
  // Why this exists: when MediaPipe finds a face but its tight crop
  // doesn't contain enough of the surrounding region for InsightFace's
  // detector to lock on, /recognize comes back as `no_face_in_frame`
  // and the user is silently never greeted. The whole-frame retry
  // does eventually fire, but giving the user a one-click escape
  // hatch is faster and a great debugging aid (the badge + caption
  // immediately reveal whether the issue is "no face in frame",
  // "below threshold", or "matched but greet was suppressed").
  const handleForceRecognize = useCallback(async () => {
    if (forceCheckInflight) return;
    const v = videoRef.current;
    const c = canvasRef.current;
    if (!v || !c || v.readyState < 2 || v.videoWidth === 0) {
      setForceCheckError("Camera isn't ready yet — wait a second and retry.");
      return;
    }
    setForceCheckInflight(true);
    setForceCheckError(null);
    try {
      c.width = 640;
      c.height = Math.round((640 * v.videoHeight) / v.videoWidth);
      const ctx = c.getContext("2d");
      if (!ctx) throw new Error("Could not get canvas context");
      ctx.drawImage(v, 0, 0, c.width, c.height);
      const blob: Blob | null = await new Promise((resolve) =>
        c.toBlob(resolve, "image/jpeg", 0.85),
      );
      if (!blob) throw new Error("Could not capture a frame");
      const form = new FormData();
      form.append("family_id", String(familyId));
      form.append("file", blob, "manual-frame.jpg");
      const result = await api.upload<RecognizeResponse>(
        "/api/aiassistant/face/recognize",
        form,
      );
      setLastRecognition(result);
      if (result.matched && result.person_id !== null) {
        // Bypass the per-person 90s suppression intentionally — the
        // user clicked the button, they want the greeting. ChatPanel
        // listens on this event and POSTs /greet, which still enforces
        // server-side "greet once per session" via greeted_already.
        window.dispatchEvent(
          new CustomEvent("avi:greet", {
            detail: {
              person_id: result.person_id,
              person_name: result.person_name,
            },
          }),
        );
      }
    } catch (e) {
      setForceCheckError(
        e instanceof Error ? e.message : "Recognize call failed",
      );
    } finally {
      setForceCheckInflight(false);
    }
  }, [familyId, forceCheckInflight]);

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
            {cameraOn && !cameraError && (
              <LocalWatcherBadge status={watcherStatus} />
            )}
            {lastRecognition?.matched && (
              <span className="inline-flex items-center gap-1 bg-emerald-500/90 text-white text-xs px-2 py-1 rounded-full">
                <User className="h-3 w-3" />
                {lastRecognition.person_name} ·{" "}
                {Math.round((lastRecognition.similarity ?? 0) * 100)}%
              </span>
            )}
            {lastRecognition &&
              !lastRecognition.matched &&
              lastRecognition.top_candidate && (
                <span
                  className="inline-flex items-center gap-1 bg-amber-500/90 text-white text-xs px-2 py-1 rounded-full"
                  title={`Best guess was ${
                    lastRecognition.top_candidate.person_name ?? "unknown"
                  } at similarity ${lastRecognition.top_candidate.similarity}, threshold ${lastRecognition.threshold}. Re-enroll a current photo of them to lift the score.`}
                >
                  <User className="h-3 w-3" />
                  Almost {lastRecognition.top_candidate.person_name} ·{" "}
                  {Math.round(
                    lastRecognition.top_candidate.similarity * 100
                  )}
                  % (need{" "}
                  {Math.round(lastRecognition.threshold * 100)}%)
                </span>
              )}
          </div>
          <div className="absolute bottom-3 right-3 flex items-center gap-2">
            {cameraOn && !cameraError && (
              <button
                className="inline-flex items-center gap-1 bg-white/90 hover:bg-white disabled:opacity-60 disabled:cursor-not-allowed text-foreground text-xs px-3 py-1.5 rounded-full shadow"
                onClick={handleForceRecognize}
                disabled={forceCheckInflight}
                title="Send the current camera frame straight to the backend recognizer. Use when the local detector has found a face but Avi hasn't greeted you."
              >
                <User className="h-3.5 w-3.5" />
                {forceCheckInflight ? "Identifying…" : "Identify me"}
              </button>
            )}
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
            {forceCheckError
              ? `Identify request failed: ${forceCheckError}`
              : cameraOn
              ? lastRecognition?.reason === "no_enrolled_embeddings"
                ? "No face embeddings yet. Click Re-enroll to build them from photos."
                : lastRecognition?.reason === "no_face_in_frame"
                ? "Camera is on, but I can't see a face yet — step closer, improve lighting, or click Identify me to send this exact frame."
                : lastRecognition?.reason === "below_threshold" &&
                  lastRecognition.top_candidate
                ? `Almost recognized ${
                    lastRecognition.top_candidate.person_name ?? "someone"
                  } (${Math.round(
                    lastRecognition.top_candidate.similarity * 100
                  )}%). I need ${Math.round(
                    lastRecognition.threshold * 100
                  )}% to greet — try Re-enroll on a current photo, or Identify me to retry now.`
                : "Watching for family members. I'll wave and say hi when I recognize someone."
              : "Camera paused — recognition is idle."}
          </div>
        </div>
      </div>

      <FaceRecognitionDriver
        familyId={familyId}
        liveSessionId={liveSessionId}
        videoRef={videoRef}
        canvasRef={canvasRef}
        cameraOn={cameraOn && !cameraError}
        onRecognize={setLastRecognition}
        onWatcherStatus={setWatcherStatus}
      />
    </div>
  );
}

/**
 * Tiny visual indicator that surfaces which face pipeline is live:
 *
 *   - "Local: N face(s)" — MediaPipe is detecting faces in the
 *     browser. Backend recognize calls are gated on track births
 *     and are happening only when something genuinely changes.
 *   - "Local: standby" — detector is up, no faces in frame; the
 *     backend is completely idle for face work.
 *   - "Backend only" — local detector failed to load; we've fallen
 *     back to the legacy 2.5s polling loop.
 *   - "Detector loading" — first-mount initialization (~1-2s while
 *     the WASM warms up).
 */
function LocalWatcherBadge({
  status,
}: {
  status: LocalFaceWatcherStatus;
}) {
  let label: string;
  let cls: string;
  let title: string;
  if (status.kind === "loading") {
    label = "Detector loading";
    cls = "bg-black/60 text-white";
    title = "Loading the in-browser face detector (MediaPipe BlazeFace).";
  } else if (status.kind === "error") {
    label = "Backend only";
    cls = "bg-amber-500/90 text-white";
    title =
      "Local detector failed to load — falling back to backend polling. " +
      "Check the console for the WASM/model load error.";
  } else if (status.activeTrackCount === 0) {
    label = "Local: standby";
    cls = "bg-black/60 text-white";
    title =
      "In-browser detector active. No faces in frame — backend recognition is idle.";
  } else {
    label = `Local: ${status.activeTrackCount} face${
      status.activeTrackCount === 1 ? "" : "s"
    }`;
    cls = "bg-sky-500/90 text-white";
    title =
      "In-browser detector tracking " +
      `${status.activeTrackCount} face(s). Backend recognize is only called ` +
      "when a brand-new track appears.";
  }
  return (
    <span
      className={cn("inline-flex items-center gap-1 text-xs px-2 py-1 rounded-full", cls)}
      title={title}
    >
      <Camera className="h-3 w-3" />
      {label}
    </span>
  );
}

/**
 * Wires the in-browser face detector (`useLocalFaceWatcher`) to the
 * backend recognize endpoint, plus a fallback path for when the
 * detector can't load. Renders nothing — it's effects all the way
 * down so the surrounding camera markup stays readable.
 *
 * Track-event-driven flow (the happy path):
 *
 *   1. MediaPipe spots a new face in the video element.
 *   2. Watcher births a track and hands us a tight JPEG crop.
 *   3. We POST that crop to /face/recognize. Single round-trip per
 *      track instead of one every 2.5s indefinitely.
 *   4. If backend says "matched", we dispatch `avi:greet` (with the
 *      same per-person 90s suppression as before).
 *   5. If backend says "unknown", we leave a short cooldown so the
 *      same stranger doesn't get re-checked on every frame.
 *
 * Fallback flow (only when MediaPipe init failed):
 *
 *   - Run the legacy whole-frame poll at FACE_RECOG_FALLBACK_INTERVAL_MS
 *     so the assistant keeps recognizing people even if the local
 *     detector is unavailable. Same `avi:greet` semantics.
 */
function FaceRecognitionDriver({
  familyId,
  liveSessionId,
  videoRef,
  canvasRef,
  cameraOn,
  onRecognize,
  onWatcherStatus,
}: {
  familyId: number;
  // Triggers a suppression-map flush whenever it changes — see the
  // effect below for the rationale.
  liveSessionId: number | null;
  videoRef: React.RefObject<HTMLVideoElement>;
  // Reused by the fallback path for whole-frame JPEG snapshots. The
  // local watcher uses its own offscreen canvas so the two never
  // contend for the same DOM node.
  canvasRef: React.RefObject<HTMLCanvasElement>;
  cameraOn: boolean;
  onRecognize: (r: RecognizeResponse) => void;
  onWatcherStatus: (s: LocalFaceWatcherStatus) => void;
}) {
  // Per-person "we already said hi to them within the last 90s"
  // suppression. Stops a re-detected family member from spamming
  // /greet every time MediaPipe re-acquires a lost track.
  //
  // CRITICAL: this map is reset whenever the session id changes so
  // a fresh session re-greets everyone. The backend already enforces
  // "greet once per session" via greeted_already, but the client
  // suppression sits *in front* of that check — without the reset,
  // the client never even calls /greet for the new session, so the
  // server never gets a chance to say "yes, this is a new session,
  // greet them again". That's why "End & reset" used to leave the
  // page silent until the suppression naturally aged out 90s later.
  const lastGreetedRef = useRef<Map<number, number>>(new Map());
  // Last-known status from the local watcher. Drives whether the
  // fallback effect arms its timer.
  const [watcherStatusInternal, setWatcherStatusInternal] =
    useState<LocalFaceWatcherStatus>({ kind: "loading" });

  // Latest props in refs so the long-lived watcher callbacks below
  // don't have to redeclare on every render.
  const familyIdRef = useRef(familyId);
  familyIdRef.current = familyId;
  const onRecognizeRef = useRef(onRecognize);
  onRecognizeRef.current = onRecognize;

  useEffect(() => {
    lastGreetedRef.current.clear();
  }, [liveSessionId]);

  // ── Helper: POST one crop to /face/recognize and fire greet on match.
  const recognizeBlob = useCallback(
    async (blob: Blob, _trackId: number | null) => {
      try {
        const form = new FormData();
        form.append("family_id", String(familyIdRef.current));
        form.append("file", blob, "frame.jpg");
        const result = await api.upload<RecognizeResponse>(
          "/api/aiassistant/face/recognize",
          form,
        );
        onRecognizeRef.current(result);
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
              }),
            );
          }
        }
        return result;
      } catch (e) {
        // Swallow transient errors — the status badge surfaces the
        // persistent ones (Ollama down, no enrolled embeddings).
        console.debug("recognize failed", e);
        return null;
      }
    },
    [],
  );

  // ── Per-track recognition state. Tracks here are local IDs from
  //   useLocalFaceWatcher; we use this to avoid re-querying the
  //   backend for a face we've already identified, and to retry on
  //   a slow cadence when the backend says "unknown".
  type TrackRecState =
    | { kind: "pending" }
    | { kind: "matched"; personId: number }
    | { kind: "unknown"; lastCheckedAt: number };
  const trackStateRef = useRef<Map<number, TrackRecState>>(new Map());

  // Mutex for whole-frame recognize calls — shared by both the 8 s
  // interval below AND the immediate first-miss kick from
  // handleTrackAppeared, so the two paths can never overlap and
  // double-bill the backend on a borderline frame.
  const wholeFrameInflightRef = useRef(false);

  // Snapshot the live <video> at 640px wide, JPEG-encode it, and POST
  // to /face/recognize. On a successful match, mark every currently-
  // unknown track as matched (we don't know which on-screen face the
  // backend identified, so we mark them all — worst case is one
  // false-positive identity badge that resolves on the next frame).
  // Returns void; results are wired through `recognizeBlob` which
  // dispatches `avi:greet` as a side-effect on a positive match.
  //
  // Used by:
  //   1. The 8 s interval below — long-tail "still here, still
  //      borderline, try again" loop.
  //   2. handleTrackAppeared, fired ~400 ms after a per-track POST
  //      came back unknown — the fast path that gets the user
  //      greeted in ~1.5 s instead of ~8 s when MediaPipe's tight
  //      crop wasn't enough for InsightFace's detector.
  const runWholeFrameRecognize = useCallback(async () => {
    if (wholeFrameInflightRef.current) return;
    const states = [...trackStateRef.current.values()];
    const hasUnknown = states.some((s) => s.kind === "unknown");
    const hasMatched = states.some((s) => s.kind === "matched");
    // If everyone in frame is already identified, there's nothing to
    // gain from a whole-frame recognize — let the camera idle. (The
    // `hasMatched` short-circuit is a known limitation in multi-
    // person frames; preserving the v1 behavior for now.)
    if (!hasUnknown || hasMatched) return;

    const v = videoRef.current;
    const c = canvasRef.current;
    if (!v || !c || v.readyState < 2 || v.videoWidth === 0) return;

    wholeFrameInflightRef.current = true;
    try {
      c.width = 640;
      c.height = Math.round((640 * v.videoHeight) / v.videoWidth);
      const ctx = c.getContext("2d");
      if (!ctx) return;
      ctx.drawImage(v, 0, 0, c.width, c.height);
      const blob: Blob | null = await new Promise((resolve) =>
        c.toBlob(resolve, "image/jpeg", 0.8),
      );
      if (!blob) return;
      const result = await recognizeBlob(blob, null);
      if (result?.matched && result.person_id !== null) {
        for (const [trackId, state] of trackStateRef.current.entries()) {
          if (state.kind === "unknown") {
            trackStateRef.current.set(trackId, {
              kind: "matched",
              personId: result.person_id,
            });
          }
        }
      } else {
        for (const [trackId, state] of trackStateRef.current.entries()) {
          if (state.kind === "unknown") {
            trackStateRef.current.set(trackId, {
              kind: "unknown",
              lastCheckedAt: Date.now(),
            });
          }
        }
      }
    } finally {
      wholeFrameInflightRef.current = false;
    }
  }, [recognizeBlob, videoRef, canvasRef]);

  const handleTrackAppeared = useCallback(
    async (track: LocalTrack, blob: Blob) => {
      trackStateRef.current.set(track.id, { kind: "pending" });
      const result = await recognizeBlob(blob, track.id);
      if (!result) {
        trackStateRef.current.delete(track.id);
        return;
      }
      if (result.matched && result.person_id !== null) {
        trackStateRef.current.set(track.id, {
          kind: "matched",
          personId: result.person_id,
        });
      } else {
        trackStateRef.current.set(track.id, {
          kind: "unknown",
          lastCheckedAt: Date.now(),
        });
        // Fast path for the common "MediaPipe crop was too tight"
        // failure mode: kick a single whole-frame recognize ~400 ms
        // from now. Without this the only retry is the 8 s interval
        // below, so a known family member who fails the first crop
        // waits the full 8 s for their greeting. The wholeFrameInflightRef
        // mutex prevents this from racing the interval tick.
        window.setTimeout(() => {
          void runWholeFrameRecognize();
        }, FACE_RECOG_FIRST_MISS_RETRY_DELAY_MS);
      }
    },
    [recognizeBlob, runWholeFrameRecognize],
  );

  const handleTrackLost = useCallback(
    (trackId: number) => {
      trackStateRef.current.delete(trackId);
    },
    [],
  );

  const handleStatusChange = useCallback(
    (s: LocalFaceWatcherStatus) => {
      setWatcherStatusInternal(s);
      onWatcherStatus(s);
    },
    [onWatcherStatus],
  );

  useLocalFaceWatcher({
    videoRef,
    enabled: cameraOn,
    callbacks: {
      onTrackAppeared: handleTrackAppeared,
      onTrackLost: handleTrackLost,
      onStatusChange: handleStatusChange,
    },
  });

  // ── Unknown-track re-probe: when the local watcher has identified
  //   one or more faces but the backend said "below_threshold" /
  //   "no_face_in_frame", we periodically re-run a whole-frame
  //   recognize so a slight head-turn or lighting change can finally
  //   cross the cosine-similarity bar. Without this, a borderline
  //   match (say similarity 0.38, threshold 0.40) means the user is
  //   never greeted for the entire duration of that track — they have
  //   to physically leave the camera frame and come back. The loop
  //   short-circuits the moment any track flips to "matched".
  //
  // The deps below are STABLE PRIMITIVES, not the whole status
  // object, so this effect doesn't churn on every detect tick.
  // Even with the watcher itself now de-duping status emissions,
  // depending on a derived boolean keeps the contract single-sourced
  // here — anyone wiring a new status field can't accidentally
  // bring back the "8s timer reset every 250ms" regression.
  const watcherReady = watcherStatusInternal.kind === "ready";
  const watcherHasTracks =
    watcherStatusInternal.kind === "ready" &&
    watcherStatusInternal.activeTrackCount > 0;
  useEffect(() => {
    if (!cameraOn) return;
    if (!watcherReady) return;
    if (!watcherHasTracks) return;
    const id = window.setInterval(
      () => void runWholeFrameRecognize(),
      FACE_RECOG_UNKNOWN_RETRY_INTERVAL_MS,
    );
    return () => window.clearInterval(id);
  }, [cameraOn, watcherReady, watcherHasTracks, runWholeFrameRecognize]);

  // ── Fallback timer: only arms when the local watcher entered the
  //   error state. Runs the legacy whole-frame poll so the page
  //   keeps recognizing people even if MediaPipe is unavailable.
  const watcherErrored = watcherStatusInternal.kind === "error";
  useEffect(() => {
    if (!cameraOn) return;
    if (!watcherErrored) return;

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
          c.toBlob(resolve, "image/jpeg", 0.8),
        );
        if (!blob || cancelled) return;
        await recognizeBlob(blob, null);
      } finally {
        inflight = false;
      }
    };

    const id = window.setInterval(tick, FACE_RECOG_FALLBACK_INTERVAL_MS);
    void tick();
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [
    cameraOn,
    watcherErrored,
    videoRef,
    canvasRef,
    recognizeBlob,
    liveSessionId,
  ]);

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
  isMobile,
}: {
  familyId: number;
  assistantName: string;
  llmStatus: LlmStatus | undefined;
  audio: AudioQueueHandle;
  voiceGender: "male" | "female";
  liveSessionId: number | null;
  // Drives the "mic default OFF on phones, ON on desktops" UX rule.
  // Members on mobile usually want to type quietly (school pickup,
  // bedtime, in-public); the kitchen iMac wants to walk-up-and-talk.
  isMobile: boolean;
}) {
  const toast = useToast();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [recognizedPersonId, setRecognizedPersonId] = useState<number | null>(
    null
  );
  // Mic default is PLATFORM-AWARE, not persisted:
  //
  //   * Desktop  → ON. Family iMac in the kitchen — they want to walk
  //                up and start talking without having to click a mic
  //                button first.
  //   * Mobile   → OFF. Phones get used in quiet/private contexts
  //                (bed, library, school pickup) where auto-listening
  //                is rude AND it conflicts with the device's audio
  //                routing while Avi is also speaking. Toggle still
  //                works mid-session for "OK, listen now".
  //
  // We deliberately do NOT seed from localStorage; an old "0" value
  // from a previous testing session was the reason the mic appeared
  // dead on refresh. Note: the browser still requires a user gesture
  // for the permission prompt the first time, which happens
  // automatically when the effect below attempts to `rec.start()`.
  const [listening, setListening] = useState<boolean>(!isMobile);
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
  //
  // Important: this is set to `now + ECHO_GRACE_MS` whenever Avi
  // STOPS speaking, not when he starts. Setting it on the start
  // edge made the window expire mid-clip on anything longer than
  // ~700 ms (e.g. the new "Hi <name>, how can I help you?"
  // greeting), which is exactly when we need it most.
  const wasSpeakingUntilRef = useRef(0);
  // Tracks the previous value of `audio.isSpeaking` so the combined
  // listening/speaking effect can detect the true→false transition
  // and arm the post-speech echo-grace window from the correct
  // moment. Plain `useRef` (not state) so updating it doesn't
  // schedule another render.
  const prevIsSpeakingRef = useRef(false);

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

  // Pending "ask a follow-up question" timer. Set by triggerGreet,
  // cancelled the moment the user does anything that proves they're
  // engaged (typing, speaking, sending a message). Held in a ref so
  // any handler in the component can clear it without re-rendering.
  const followupTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelPendingFollowup = useCallback((reason?: string) => {
    if (followupTimerRef.current != null) {
      clearTimeout(followupTimerRef.current);
      followupTimerRef.current = null;
      if (reason) {
        console.debug(`[followup] cancelled (${reason})`);
      }
    }
  }, []);
  // Cancel any pending follow-up when the page unmounts so the
  // setTimeout callback doesn't fire against a torn-down React tree.
  useEffect(() => {
    return () => cancelPendingFollowup("unmount");
  }, [cancelPendingFollowup]);
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
    // A fresh face means any pending follow-up from a previous greet
    // is now stale — drop it before scheduling a new one.
    cancelPendingFollowup("new greet");

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
      audio.enqueue(greetingText, {
        gender_hint: voiceGender,
        messageId: greetingMsg.id,
      });
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

    // Note: we deliberately do NOT chain an LLM-generated "how's your
    // diet going?" follow-up here. The greeting itself ("Hi Sam, how
    // can I help you?") is the prompt — Avi waits for the user to
    // answer rather than ambushing them with a goal-specific question.
    // The /api/aiassistant/followup endpoint and the cancel-on-typing
    // hooks below are kept in place so we can re-enable this flow
    // cheaply if we change our mind.
  }

  // Keep the ref pointed at the latest closure on every render.
  triggerGreetRef.current = triggerGreet;

  async function sendUserMessage(text: string) {
    const content = text.trim();
    if (!content || isStreaming) return;
    // The user just took the floor — drop any pending "ask a follow-up
    // question" timer so Avi doesn't talk over them.
    cancelPendingFollowup("user sent message");
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
            // First frame from /chat carries the agent task id so the
            // bubble can deep-link to /tasks/<id> for full audit.
            if (typeof parsed.task_id === "number" && !parsed.type) {
              const taskId = parsed.task_id as number;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMsg.id ? { ...m, taskId } : m
                )
              );
            }
            // Fast-ack from the lightweight model. At most one per
            // turn, fired by the backend race-and-ack watchdog when
            // the heavy agent hasn't streamed any text yet by the
            // configured threshold (default 3s). We surface it as a
            // placeholder inside the in-flight assistant bubble so
            // the user sees Avi react quickly; the bubble's real
            // content takes over when the heavy reply lands.
            if (parsed.type === "fast_ack" && typeof parsed.text === "string") {
              const ackText = parsed.text as string;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMsg.id ? { ...m, fastAck: ackText } : m
                )
              );
            }
            // Agent step (thought, tool_call, tool_result, final, error).
            // The backend already persists these to agent_steps so the
            // UI list is just a mirror of what's auditable in the DB.
            if (parsed.type === "step" && parsed.step) {
              const step = parsed.step as AgentStepView;
              setMessages((prev) =>
                prev.map((m) => {
                  if (m.id !== assistantMsg.id) return m;
                  const existing = m.steps ?? [];
                  // Replace by step_index to keep the list compact when
                  // the same step is re-emitted (rare, but safe).
                  const filtered = existing.filter(
                    (s) => s.step_index !== step.step_index
                  );
                  return {
                    ...m,
                    steps: [...filtered, step].sort(
                      (a, b) => a.step_index - b.step_index
                    ),
                  };
                })
              );
            }
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
        audio.enqueue(speakable, {
          gender_hint: voiceGender,
          messageId: assistantMsg.id,
        });
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
    // Debounced auto-submit: armed when we see an `isFinal`, cancelled
    // by any subsequent speech (interim or final). See SUBMIT_DEBOUNCE_MS.
    let submitTimer: number | null = null;
    const cancelSubmitTimer = () => {
      if (submitTimer !== null) {
        clearTimeout(submitTimer);
        submitTimer = null;
      }
    };
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
        // Don't auto-restart the recognizer while Avi is speaking
        // (or during the post-speech echo-grace window). The other
        // effect that watches `audio.isSpeaking` owns re-arming us
        // once Avi finishes, so bailing out here just prevents the
        // engine from chewing on Avi's own voice for ~95% of the
        // clip and racing onresult against the audio.isSpeaking
        // ref that gates it.
        if (
          audioSpeakingRef.current ||
          Date.now() < wasSpeakingUntilRef.current
        ) {
          return;
        }
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
        cancelSubmitTimer();
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
      // Real transcript made it past the echo gate — the user is
      // talking, so cancel any pending goal-question timer before Avi
      // tries to talk over them and reset the submit debounce so a
      // mid-sentence pause never auto-fires.
      if (interim || pendingFinal) {
        cancelPendingFollowup("user speaking");
        cancelSubmitTimer();
      }
      setInput((pendingFinal + " " + interim).trim());
      if (!sawFinal) return;
      if (isStreamingRef.current) return;
      // Arm (or re-arm) the debounce. The actual submit fires only
      // after SUBMIT_DEBOUNCE_MS of true silence — any new audio above
      // cancels this timer first, so a thinking pause keeps the floor.
      submitTimer = window.setTimeout(() => {
        submitTimer = null;
        if (
          audioSpeakingRef.current ||
          Date.now() < wasSpeakingUntilRef.current ||
          isStreamingRef.current
        ) {
          return;
        }
        const toSend = pendingFinal.trim();
        if (!toSend) return;
        pendingFinal = "";
        setInput("");
        sendUserMessageRef.current(toSend);
      }, SUBMIT_DEBOUNCE_MS);
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
      // Engine timed out (Chrome ends the recognizer every ~30-60 s in
      // continuous mode). If a debounce is in flight let it run — the
      // restart loop below will spin a fresh recognizer in parallel and
      // the timer will fire on its own clock. Otherwise flush any tail
      // parked from a mid-sentence pause that never got debounce-armed.
      if (submitTimer !== null) {
        scheduleRestart();
        return;
      }
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
      cancelSubmitTimer();
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

    const wasSpeaking = prevIsSpeakingRef.current;
    prevIsSpeakingRef.current = audio.isSpeaking;

    if (audio.isSpeaking) {
      // Push the until-time out so onresult drops anything that
      // sneaks through during this clip. We refresh it again on the
      // false-edge below — that's the one that actually matters,
      // since SR engine lookback can deliver Avi's tail syllables a
      // beat AFTER playback ends.
      wasSpeakingUntilRef.current = Date.now() + ECHO_GRACE_MS;
      setInput("");
      stop();
      console.info("[speech] paused for TTS playback");
      return;
    }

    // Just transitioned from speaking → not speaking. Arm the
    // echo-grace window from NOW so any audio still buffered in the
    // SR engine's lookback (typically 300-500 ms) gets dropped
    // instead of being submitted as a "user message" containing
    // Avi's own last words.
    if (wasSpeaking) {
      wasSpeakingUntilRef.current = Date.now() + ECHO_GRACE_MS;
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
    // On large screens we fill the grid row (matching the left
    // column's Avi + camera height) and let ``min-h-0`` give the
    // messages area room to shrink so long chat threads scroll
    // internally instead of pushing the row taller. On narrow
    // screens the column stack has no parent to inherit from, so we
    // fall back to a fixed floor.
    <div className="card flex flex-col min-h-[500px] lg:min-h-0 lg:h-full">
      <div className="card-header shrink-0">
        <div className="card-title flex items-center gap-2">
          <Bot className="h-4 w-4 text-primary" /> Chat with {assistantName}
        </div>
        <div className="text-xs text-muted-foreground">{hintLine}</div>
      </div>

      <div
        ref={messagesRef}
        // ``min-h-0`` is the magic that lets ``flex-1`` actually shrink
        // below its content's intrinsic size — otherwise long chat
        // threads would push the panel taller than the row.
        className="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-3 bg-muted/30"
      >
        {messages.length === 0 && (
          <div className="text-sm text-muted-foreground h-full flex flex-col items-center justify-center text-center gap-2 pt-16">
            <Camera className="h-8 w-8" />
            <div>Stand in front of the camera,</div>
            <div>or type a question to start.</div>
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble
            key={m.id}
            message={m}
            assistantName={assistantName}
            isSpeakingThis={
              audio.currentMessageId === m.id && audio.isSpeaking
            }
            onSkipSpeech={audio.skipCurrent}
          />
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
          onChange={(e) => {
            // Any keystroke (or a speech-driven input update) means the
            // user is engaged — kill the pending goal-question timer.
            if (e.target.value) cancelPendingFollowup("user typing");
            setInput(e.target.value);
          }}
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

// Three dots that gently bounce while Avi is composing a reply.
// CSS-only (see `.chat-typing-dots` in src/index.css). Inherits
// `currentColor` so the same component looks right in both the
// muted-foreground placeholder text AND a dimmer variant if we
// ever drop it into a different bubble background.
function TypingDots({ className }: { className?: string }) {
  return (
    <span
      className={cn("chat-typing-dots", className)}
      role="status"
      aria-label="Avi is typing"
    >
      <span />
      <span />
      <span />
    </span>
  );
}

function MessageBubble({
  message,
  assistantName,
  isSpeakingThis,
  onSkipSpeech,
}: {
  message: ChatMessage;
  assistantName: string;
  // True only for the assistant bubble whose audio clip is the one
  // currently playing through the speakers. Drives the Skip button.
  isSpeakingThis?: boolean;
  // Stops the current TTS clip mid-utterance without affecting
  // anything queued behind it.
  onSkipSpeech?: () => void;
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
            : "bg-white border border-border rounded-bl-sm",
          isSpeakingThis && !isUser && "ring-2 ring-primary/40"
        )}
      >
        {!isUser && message.meta && (
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-0.5">
            {assistantName} · {message.meta}
          </div>
        )}
        {!isUser && message.steps && message.steps.length > 0 && (
          <AgentStepsList
            steps={message.steps}
            taskId={message.taskId ?? null}
            streaming={message.streaming}
          />
        )}
        {/*
         * Render priority for an in-flight assistant bubble:
         *   1. Real `content` once the heavy reply starts streaming.
         *   2. Otherwise the fast-ack placeholder + animated dots
         *      so the user sees both the contextual hint AND that
         *      Avi is still composing the full reply.
         *   3. Otherwise just the animated dots (covers the gap
         *      before any tool steps or ack arrive).
         *   4. Nothing if the bubble is settled and content-less
         *      (e.g. greet skipped).
         */}
        {message.content ? (
          message.content
        ) : !isUser && message.streaming && message.fastAck ? (
          <span className="flex items-center gap-2 italic text-muted-foreground">
            <span>{message.fastAck}</span>
            <TypingDots />
          </span>
        ) : message.streaming ? (
          <TypingDots />
        ) : (
          ""
        )}
        {isSpeakingThis && !isUser && onSkipSpeech && (
          <div className="mt-1.5 flex justify-end">
            <button
              type="button"
              onClick={onSkipSpeech}
              className="inline-flex items-center gap-1 rounded-full border border-primary/30 bg-primary/5 px-2 py-0.5 text-[11px] text-primary hover:bg-primary/15 transition"
              title="Stop reading this message aloud — keep the text on screen"
            >
              <SkipForward className="h-3 w-3" />
              Skip voice
            </button>
          </div>
        )}
      </div>
      {isUser && (
        <div className="h-7 w-7 rounded-full bg-muted flex items-center justify-center flex-shrink-0">
          <User className="h-4 w-4 text-muted-foreground" />
        </div>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// AgentStepsList — inline timeline of plan/execute/observe steps shown
// inside an assistant bubble whenever the chat turn called any tools.
// Pairs each tool_call with its tool_result so the eye sees one row per
// action rather than two.
// ---------------------------------------------------------------------------

const TOOL_LABEL: Record<string, { label: string; icon: typeof Mail }> = {
  gmail_send: { label: "Send email", icon: Mail },
  lookup_person: { label: "Look up person", icon: Search },
  sql_query: { label: "Query database", icon: Database },
  calendar_list_upcoming: { label: "List calendar", icon: History },
};

function AgentStepsList({
  steps,
  taskId,
  streaming,
}: {
  steps: AgentStepView[];
  taskId: number | null;
  streaming?: boolean;
}) {
  // Pair tool_call with the matching tool_result by step_index proximity:
  // the agent always emits tool_call(s) immediately followed by tool_result(s)
  // in the same order, so we walk the list and merge them.
  type Row =
    | {
        kind: "tool";
        callIndex: number;
        toolName: string;
        input?: Record<string, unknown> | null;
        result?: AgentStepView | null;
      }
    | { kind: "thinking"; content: string }
    | { kind: "error"; content: string };

  const rows: Row[] = [];
  const usedResults = new Set<number>();
  for (const s of steps) {
    if (s.step_type === "tool_call") {
      const result = steps.find(
        (r) =>
          r.step_type === "tool_result" &&
          r.tool_name === s.tool_name &&
          r.step_index > s.step_index &&
          !usedResults.has(r.step_index)
      );
      if (result) usedResults.add(result.step_index);
      rows.push({
        kind: "tool",
        callIndex: s.step_index,
        toolName: s.tool_name ?? "tool",
        input: s.tool_input,
        result: result ?? null,
      });
    } else if (s.step_type === "thinking" && s.content) {
      rows.push({ kind: "thinking", content: s.content });
    } else if (s.step_type === "error" && s.error) {
      rows.push({ kind: "error", content: s.error });
    }
    // tool_result rows are absorbed by their pair above; final step
    // becomes the regular bubble text via the delta channel.
  }

  if (rows.length === 0) return null;

  const stillRunning = streaming && rows.some((r) => r.kind === "tool" && !r.result);

  return (
    <div className="mb-2 -mx-1 rounded-lg border border-border bg-muted/40">
      <div className="flex items-center justify-between px-2 py-1 border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span className="flex items-center gap-1">
          {stillRunning ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Sparkles className="h-3 w-3" />
          )}
          Agent steps
        </span>
        {taskId != null && (
          <Link
            to={`agent-tasks/${taskId}`}
            className="text-primary hover:underline"
          >
            Audit ›
          </Link>
        )}
      </div>
      <ol className="divide-y divide-border/60">
        {rows.map((row, idx) => (
          <li key={idx} className="px-2 py-1.5">
            {row.kind === "tool" && <ToolStepRow row={row} />}
            {row.kind === "thinking" && (
              <div className="text-xs text-muted-foreground italic">
                {row.content}
              </div>
            )}
            {row.kind === "error" && (
              <div className="text-xs text-rose-600 flex items-start gap-1.5">
                <AlertCircle className="h-3.5 w-3.5 flex-shrink-0 mt-0.5" />
                {row.content}
              </div>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

function ToolStepRow({
  row,
}: {
  row: {
    kind: "tool";
    callIndex: number;
    toolName: string;
    input?: Record<string, unknown> | null;
    result?: AgentStepView | null;
  };
}) {
  const meta = TOOL_LABEL[row.toolName] ?? { label: row.toolName, icon: Sparkles };
  const Icon = meta.icon;
  const result = row.result;
  const isError = !!result?.error;
  const isPending = !result;

  return (
    <div className="flex items-start gap-2">
      <div
        className={cn(
          "h-5 w-5 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5",
          isError
            ? "bg-rose-100 text-rose-600"
            : isPending
              ? "bg-amber-100 text-amber-600"
              : "bg-emerald-100 text-emerald-600"
        )}
      >
        {isPending ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : isError ? (
          <AlertCircle className="h-3 w-3" />
        ) : (
          <CheckCircle2 className="h-3 w-3" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
          <span>{meta.label}</span>
          {result?.duration_ms != null && (
            <span className="text-[10px] text-muted-foreground font-normal">
              · {Math.max(1, Math.round(result.duration_ms))}ms
            </span>
          )}
        </div>
        {row.input && Object.keys(row.input).length > 0 && (
          <div className="text-[11px] text-muted-foreground mt-0.5 truncate">
            {summariseToolInput(row.toolName, row.input)}
          </div>
        )}
        {result?.content && (
          <div
            className={cn(
              "text-[11px] mt-0.5 truncate",
              isError ? "text-rose-600" : "text-foreground/80"
            )}
          >
            {result.content}
          </div>
        )}
        {result?.error && (
          <div className="text-[11px] text-rose-600 mt-0.5">
            {result.error}
          </div>
        )}
      </div>
    </div>
  );
}

function summariseToolInput(
  toolName: string,
  input: Record<string, unknown>
): string {
  if (toolName === "gmail_send") {
    const to = input.to ?? "";
    const subject = input.subject ?? "";
    return `to ${to} — “${subject}”`;
  }
  if (toolName === "lookup_person") {
    return `name: ${input.name ?? ""}`;
  }
  if (toolName === "sql_query") {
    const sql = String(input.sql ?? "");
    return sql.length > 120 ? sql.slice(0, 117) + "…" : sql;
  }
  if (toolName === "calendar_list_upcoming") {
    const hrs = input.hours_ahead ?? 72;
    return `next ${hrs}h`;
  }
  // Fallback: show first 1-2 keys.
  const entries = Object.entries(input).slice(0, 2);
  return entries.map(([k, v]) => `${k}: ${String(v).slice(0, 40)}`).join(", ");
}
