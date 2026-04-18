/**
 * AviLive2D — a PixiJS-mounted Live2D character that replaces the
 * static portrait on the live AI assistant page.
 *
 * Features (all built into Live2D Cubism 4 or the pixi-live2d-display
 * fork we use):
 *   • Real-time lip sync driven by the same Kokoro <audio> element
 *     that plays Avi's voice. The fork's `speak()` method hooks an
 *     AnalyserNode into `ParamMouthOpenY` automatically.
 *   • Automatic blinking (the `EyeBlink` parameter group) and
 *     physics-based hair / clothing sway (physics3.json) happen for
 *     free once the model is loaded.
 *   • Eye + head tracking: pupils follow the mouse cursor.
 *   • Gestures: `avi:greet` events (fired when a family member is
 *     recognized) play a random "TapBody" motion — Natori has 5 of
 *     them, mostly waves and head tilts.
 *   • Expression cycling on greet: pops into "Smile" for the hello
 *     then drifts back to "Normal".
 *
 * Load order quirks worth knowing
 * -------------------------------
 * pixi-live2d-display expects two globals on `window` at import time:
 *
 *   1. `Live2DCubismCore` — proprietary runtime loaded via a <script>
 *      tag in index.html. If it's not there, the dynamic import will
 *      throw and this component renders `null` (the parent falls back
 *      to the static Gemini portrait).
 *   2. `PIXI`            — the pixi-live2d-display monkey-patches
 *      PIXI.Application when imported, so we must expose our pixi
 *      import globally before importing the Live2D module.
 *
 * We handle both via a one-shot async bootstrap below.
 */

import { useEffect, useRef, useState } from "react";

export type AviLive2DState = "init" | "loading" | "ready" | "unavailable" | "error";

type AviLive2DProps = {
  /** Model folder relative to /public, e.g. "natori" for /live2d/natori/Natori.model3.json */
  modelPath: string;
  modelFile: string;
  /** The <audio> element actually producing sound. Used for motion sync only. */
  audioEl: HTMLAudioElement | null;
  /**
   * Live RMS amplitude (0..1) of the currently-playing Kokoro clip,
   * updated on every rAF tick by `useAudioQueue`. We read `.current`
   * inside the Pixi ticker callback and write it straight into the
   * model's `ParamMouthOpenY`, giving ~60 Hz lip-sync without the
   * double-playback problem that `model.speak()` would cause.
   */
  amplitudeRef: React.MutableRefObject<number>;
  /** Whether a TTS clip is currently playing — gates chatter motions. */
  isSpeaking: boolean;
  /** Flag that flips true for ~2.8s when a new face is recognized. */
  isWaving: boolean;
  /** Rendered behind the canvas while the model is still loading. */
  fallback?: React.ReactNode;
  /** Debug: show a small status line inside the stage. */
  debug?: boolean;
  /** Called whenever the internal mount/load state transitions. */
  onStateChange?: (state: AviLive2DState, error?: string | null) => void;
};

/**
 * Result of attempting to load the Live2D runtime. On failure we keep
 * a human-readable reason so the UI can surface it instead of silently
 * dropping back to the static avatar.
 */
type BootstrapResult =
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  | { ok: true; PIXI: any; Live2DModel: any }
  | { ok: false; reason: string };

/**
 * Global cache for the dynamically imported `pixi-live2d-display`
 * module + the `pixi.js` Application ctor. We only cache a *successful*
 * bootstrap so that a transient failure (e.g. React mounted a few ms
 * before the Cubism <script> finished executing) doesn't poison the
 * next attempt.
 */
let _live2dBootstrap: Promise<BootstrapResult> | null = null;

/** Poll `window.Live2DCubismCore` for up to `timeoutMs`. */
async function waitForCubismCore(timeoutMs: number): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if ((window as any).Live2DCubismCore) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return !!(window as any).Live2DCubismCore;
}

async function bootstrapLive2D(): Promise<BootstrapResult> {
  if (_live2dBootstrap) {
    const cached = await _live2dBootstrap;
    // Retry if the previous attempt failed — the Cubism Core script
    // may have loaded in the meantime (slow network, hot-reload race).
    if (cached.ok) return cached;
  }
  const pending = (async (): Promise<BootstrapResult> => {
    if (typeof window === "undefined") {
      return { ok: false, reason: "no window (SSR)" };
    }

    // The Cubism Core <script> tag in index.html doesn't have `defer`,
    // so it should normally be resolved before React mounts. On first
    // load under Vite's dev server, though, resource ordering can be
    // weird — poll briefly to give it a fair chance.
    console.info("[AviLive2D] waiting for Live2DCubismCore runtime…");
    const coreLoaded = await waitForCubismCore(5000);
    if (!coreLoaded) {
      const reason =
        "Live2DCubismCore not found on window after 5s — /live2d/live2dcubismcore.min.js may be blocked, missing, or failed to execute.";
      console.warn(`[AviLive2D] ${reason}`);
      return { ok: false, reason };
    }
    console.info("[AviLive2D] Live2DCubismCore present — importing pixi modules");

    try {
      const PIXI = await import("pixi.js");
      // pixi-live2d-display expects PIXI on window at import time so it
      // can patch PIXI.Ticker / PIXI.Application with Live2D hooks.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).PIXI = PIXI;
      // IMPORTANT: use the `/cubism4` subpath entry. The package's
      // default export bundles support for BOTH Cubism 2 and Cubism 4
      // runtimes and refuses to initialise unless both are present on
      // `window` (``live2d.min.js`` + ``live2dcubismcore.min.js``).
      // Modern Live2D characters (Natori, Haru, Hiyori, anything made
      // with Cubism Editor 3+) are all Cubism 4, so we pull in the
      // Cubism-4-only entrypoint and avoid shipping the deprecated
      // Cubism 2 runtime at all.
      const mod = await import("pixi-live2d-display-lipsyncpatch/cubism4");
      // CRITICAL: register Pixi's shared Ticker with Live2DModel so
      // motions, physics, auto-blink, and idle animations actually run.
      // Without this the model mounts but freezes as a static pose.
      try {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (mod.Live2DModel as any).registerTicker?.(PIXI.Ticker);
      } catch (e) {
        console.debug("[AviLive2D] registerTicker failed (non-fatal)", e);
      }
      console.info("[AviLive2D] pixi + live2d modules ready");
      return { ok: true, PIXI, Live2DModel: mod.Live2DModel };
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      console.warn("[AviLive2D] bootstrap import failed", err);
      return { ok: false, reason: `import failed: ${reason}` };
    }
  })();
  _live2dBootstrap = pending;
  return pending;
}

export default function AviLive2D({
  modelPath,
  modelFile,
  audioEl,
  amplitudeRef,
  isSpeaking,
  isWaving,
  fallback = null,
  debug = false,
  onStateChange,
}: AviLive2DProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const appRef = useRef<any>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const modelRef = useRef<any>(null);
  const [state, setState] = useState<AviLive2DState>("init");
  const [errMsg, setErrMsg] = useState<string | null>(null);

  // Notify the parent on every transition so it can render a status badge.
  useEffect(() => {
    onStateChange?.(state, errMsg);
  }, [state, errMsg, onStateChange]);

  // One-shot: bootstrap + mount the model.
  useEffect(() => {
    let cancelled = false;
    setState("loading");

    (async () => {
      const bootstrap = await bootstrapLive2D();
      if (cancelled) return;
      if (!bootstrap.ok) {
        setErrMsg(bootstrap.reason);
        setState("unavailable");
        return;
      }
      const { PIXI, Live2DModel } = bootstrap;

      try {
        const canvas = canvasRef.current;
        const wrap = wrapRef.current;
        if (!canvas || !wrap) return;

        // Pixi application sized to the parent. We'll resize on window
        // events too via the ResizeObserver below.
        const app = new PIXI.Application({
          view: canvas,
          width: wrap.clientWidth,
          height: wrap.clientHeight,
          backgroundAlpha: 0,
          antialias: true,
          resolution: Math.min(window.devicePixelRatio || 1, 2),
          autoDensity: true,
        });
        appRef.current = app;

        const url = `/live2d/${modelPath}/${modelFile}`;
        console.info(`[AviLive2D] loading model from ${url}`);
        const model = await Live2DModel.from(url, { autoInteract: false });
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const internal = (model as any).internalModel;
        console.info(
          `[AviLive2D] model loaded — logical size ${model.width}×${model.height}px, motions:`,
          internal?.motionManager?.motionGroups
            ? Object.keys(internal.motionManager.motionGroups)
            : "(unknown)"
        );
        if (cancelled) {
          app.destroy(true, { children: true });
          return;
        }
        modelRef.current = model;
        app.stage.addChild(model);

        // Scale + center the model to fit the canvas. We reset scale
        // to 1 first so `model.width/height` are in the model's own
        // logical units (typically a few thousand pixels for Cubism
        // busts); then pick a uniform scale that lets the whole bust
        // fit with a little margin, and center the result. The Live2D
        // container's origin is its top-left in local coords, so plain
        // (w - w')/2 centering works.
        const fitModel = () => {
          if (!wrapRef.current) return;
          const w = wrapRef.current.clientWidth;
          const h = wrapRef.current.clientHeight;
          if (w === 0 || h === 0) return;
          app.renderer.resize(w, h);
          model.scale.set(1);
          const scale = Math.min(w / model.width, h / model.height) * 0.95;
          model.scale.set(scale);
          model.x = (w - model.width) / 2;
          // Anchor the bust toward the bottom — Cubism heads are drawn
          // at the top of their logical canvas, so dropping the y by a
          // small amount keeps the eyes roughly in the upper third.
          model.y = (h - model.height) / 2;
        };
        fitModel();
        const ro = new ResizeObserver(fitModel);
        ro.observe(wrap);

        // Pointer-based eye + head tracking. The Live2D model exposes
        // `focus(x, y)` in normalized screen coords.
        const onPointerMove = (e: PointerEvent) => {
          const rect = wrap.getBoundingClientRect();
          const x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
          const y = ((e.clientY - rect.top) / rect.height) * 2 - 1;
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (model as any).focus?.(x, -y);
        };
        window.addEventListener("pointermove", onPointerMove);

        // Clickable hit areas — tapping the character plays a random
        // TapBody motion. Fun for kids.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (model as any).on?.("hit", (hitAreaNames: string[]) => {
          if (hitAreaNames.includes("Head") || hitAreaNames.includes("Body")) {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (model as any).motion?.("TapBody");
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (model as any).expression?.("Smile");
          }
        });
        // Forward pointer clicks on the canvas to the model's hit test.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (canvas as any).style.touchAction = "none";
        canvas.addEventListener("pointerdown", (e) => {
          const rect = canvas.getBoundingClientRect();
          const x = e.clientX - rect.left;
          const y = e.clientY - rect.top;
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (model as any).tap?.(x, y);
        });

        // Start in the Normal expression.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (model as any).expression?.("Normal");

        // Lip-sync ticker: on every Pixi frame after the model's own
        // motion/physics pass, override ParamMouthOpenY (and a tiny
        // bit of ParamMouthForm so the lips don't just gape open) with
        // the current audio amplitude. This runs at display refresh
        // rate and feels genuinely "in-sync" with the Kokoro voice.
        const tickerFn = () => {
          const amp = amplitudeRef?.current ?? 0;
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const core = (model as any).internalModel?.coreModel;
          if (!core?.setParameterValueById) return;
          // Modest scaling; values >1 are clipped by the Cubism runtime.
          const mouthOpen = Math.min(1, amp * 1.9);
          // Slight smile when speaking so the mouth animation reads as
          // speech rather than yawning.
          const mouthForm = amp > 0.05 ? 0.3 : 0.0;
          try {
            core.setParameterValueById("ParamMouthOpenY", mouthOpen);
            core.setParameterValueById("ParamMouthForm", mouthForm);
          } catch {
            /* parameter id not present — ignore */
          }
        };
        app.ticker.add(tickerFn);

        // Store cleanup handles on the model object so the teardown
        // effect can reach them.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (model as any)._aviCleanup = () => {
          window.removeEventListener("pointermove", onPointerMove);
          ro.disconnect();
          try {
            app.ticker.remove(tickerFn);
          } catch {
            /* noop */
          }
        };

        setState("ready");
      } catch (err) {
        console.error("AviLive2D: model load failed", err);
        setErrMsg(err instanceof Error ? err.message : String(err));
        setState("error");
      }
    })();

    return () => {
      cancelled = true;
      try {
        modelRef.current?._aviCleanup?.();
      } catch {
        /* noop */
      }
      try {
        appRef.current?.destroy(true, { children: true });
      } catch {
        /* noop */
      }
      modelRef.current = null;
      appRef.current = null;
    };
    // modelPath + modelFile are effectively static per mount; changing
    // them mid-session would require a full teardown which we don't
    // currently expose in the UI, so deps list is intentionally empty.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Greet motion trigger — every time isWaving flips to true, pick a
  // random TapBody motion and briefly smile.
  const wavingRef = useRef(false);
  useEffect(() => {
    if (state !== "ready") return;
    const model = modelRef.current;
    if (!model) return;
    if (isWaving && !wavingRef.current) {
      wavingRef.current = true;
      try {
        // Priority 3 (Force) so the greeting motion cuts in over the
        // default idle loop without waiting for it to finish.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (model as any).motion?.("TapBody", undefined, 3);
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (model as any).expression?.("Smile");
      } catch (e) {
        console.debug("Avi motion() failed", e);
      }
      // Drift back to Normal after a beat so subsequent greets feel fresh.
      window.setTimeout(() => {
        try {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (model as any).expression?.("Normal");
        } catch {
          /* noop */
        }
        wavingRef.current = false;
      }, 3500);
    }
  }, [isWaving, state]);

  // `audioEl` is currently only accepted to keep a future door open
  // (e.g. triggering a "talking pose" motion the moment playback
  // starts). For now lip-sync is handled via the amplitude ref, so we
  // just touch the prop so TS doesn't complain.
  useEffect(() => {
    void audioEl;
  }, [audioEl]);

  // When Avi starts speaking a non-greeting reply, nudge an idle
  // chatter motion so he doesn't just stare back. Kept low-priority
  // so an active greeting motion isn't interrupted.
  useEffect(() => {
    if (state !== "ready") return;
    const model = modelRef.current;
    if (!model) return;
    if (!isSpeaking) return;
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (model as any).motion?.("Idle", undefined, 1);
    } catch {
      /* noop */
    }
  }, [isSpeaking, state]);

  const wrapperClass =
    "relative w-full h-full flex items-center justify-center";

  return (
    <div ref={wrapRef} className={wrapperClass}>
      <canvas
        ref={canvasRef}
        className="absolute inset-0 w-full h-full"
        style={{ display: state === "ready" ? "block" : "none" }}
      />
      {state !== "ready" && (
        <div className="absolute inset-0 flex items-center justify-center">
          {fallback}
        </div>
      )}
      {debug && (
        <div
          className="absolute top-2 left-2 max-w-[calc(100%-1rem)] text-[11px] font-mono bg-black/75 text-white px-2 py-1 rounded shadow-md leading-snug"
          role="status"
        >
          <div>
            <span className="opacity-70">Live2D: </span>
            <span
              className={
                state === "ready"
                  ? "text-emerald-300"
                  : state === "error" || state === "unavailable"
                    ? "text-amber-300"
                    : "text-sky-300"
              }
            >
              {state}
            </span>
          </div>
          <div className="opacity-70 truncate">
            model: /live2d/{modelPath}/{modelFile}
          </div>
          {errMsg && (
            <div className="text-amber-300 whitespace-pre-wrap break-words">
              {errMsg}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
