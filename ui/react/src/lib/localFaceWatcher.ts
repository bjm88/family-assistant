/**
 * Browser-side face *detection* + lightweight tracking, used to gate
 * the much heavier backend face *recognition* call.
 *
 * Why this exists
 * ---------------
 * The original live page polled `/api/aiassistant/face/recognize`
 * every 2.5 s unconditionally. Each call invoked InsightFace's
 * `buffalo_l` detector + 512-d ArcFace embedding extractor on the
 * Mac Studio (CoreML / ANE), even when:
 *
 *   - nobody was on screen, or
 *   - the same person had been sitting still for 20 minutes.
 *
 * That stole CoreML / ANE cycles, FastAPI worker slots, and DB
 * sessions away from the agent loop running chat + tools.
 *
 * The fix is a two-stage pipeline:
 *
 *   1. **In the browser (this module)**: run MediaPipe
 *      `BlazeFace short-range` (~225 KB tflite) at ~4 Hz. It only
 *      needs to answer "is there a face right now, and where?".
 *   2. **On the backend**: only call `/face/recognize` when this
 *      module raises a *track event* — a new face has appeared, or
 *      a previously-recognized face was lost and reappeared.
 *
 * The detector + tracker live entirely in the browser and never
 * leave the page; embeddings still happen on the backend so the
 * Fernet-encrypted gallery in `face_embeddings` stays the source of
 * truth for identity.
 *
 * Tracking
 * --------
 * We use a deliberately tiny IoU-based tracker. A typical living-room
 * scene has ≤4 faces, so an O(N·M) greedy matcher is cheaper than
 * importing a "real" tracking lib. State per track:
 *
 *   - bounding box (smoothed across frames)
 *   - last-seen timestamp (older than `TRACK_TTL_MS` → drop)
 *   - "needs recognize" flag (raised when the track is created and
 *     cleared as soon as a backend recognize call has started)
 *
 * The hook this module exports (`useLocalFaceWatcher`) wires the
 * detector to a video element and emits high-level events:
 *
 *   - `onTrackAppeared(trackId, cropBlob)` — fire backend recognize
 *   - `onTrackLost(trackId)`              — let the UI clear badges
 *
 * Crops are produced from a private offscreen canvas to keep the
 * roundtrip small and avoid the "largest face wins" guess on the
 * backend when multiple people are in frame.
 */

import { useEffect, useRef } from "react";
// Heavy MediaPipe types are imported as types-only so the runtime
// bundle stays clean; the actual modules are dynamic-imported on
// first detector use (see `getDetector` below). Visiting the admin
// pages should never download the BlazeFace WASM glue.
import type {
  FaceDetector,
  Detection,
} from "@mediapipe/tasks-vision";

// ---------------------------------------------------------------------------
// Tunables. Conservative defaults — tweak if you see jitter or missed faces.
// ---------------------------------------------------------------------------

/** How often we ask MediaPipe to look at a frame. 4 Hz is plenty for
 *  "did someone walk into the room?" while keeping CPU / GPU load
 *  invisible on an M-series Mac. */
export const DETECT_INTERVAL_MS = 250;

/** Drop a track if it hasn't been re-detected this long. Tuned for
 *  short blinks of MediaPipe missing a face (turning head, hand
 *  passing across) without collapsing two distinct people into one
 *  track when one walks out and another walks in. */
export const TRACK_TTL_MS = 1500;

/** IoU threshold for matching a detection to an existing track. */
export const TRACK_IOU_THRESHOLD = 0.25;

/** Margin added to the detected bbox before cropping for backend
 *  recognition. InsightFace prefers a bit of context around the face. */
export const CROP_MARGIN_RATIO = 0.35;

/** Long edge of the JPEG crop we send to the backend. 320 px is plenty
 *  for ArcFace and keeps the upload tiny. */
export const CROP_MAX_DIM = 320;

/** Confidence floor for accepting a MediaPipe detection. BlazeFace
 *  short-range happily reports 0.8+ on real faces and dips below 0.5
 *  on motion blur / partial occlusion. */
export const MIN_DETECTION_SCORE = 0.55;

/** Where the postinstall script puts the assets on disk. Both paths
 *  resolve relative to the Vite dev server / built site root. */
const WASM_BASE_URL = "/mediapipe/wasm";
const MODEL_URL = "/mediapipe/models/blaze_face_short_range.tflite";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type BBox = { x: number; y: number; w: number; h: number };

export type LocalTrack = {
  id: number;
  bbox: BBox;
  score: number;
  firstSeenAt: number;
  lastSeenAt: number;
};

export type LocalFaceWatcherCallbacks = {
  /** New face that we've never tracked before. Use this to fire a
   *  backend `/face/recognize` call. The crop is the JPEG you should
   *  upload — already tightened to the face with a margin. */
  onTrackAppeared: (track: LocalTrack, crop: Blob) => void;
  /** A track has gone away (left the frame, looked away, occluded).
   *  Useful for clearing identity badges so they don't stick on a
   *  stale person. */
  onTrackLost?: (trackId: number) => void;
  /** Status callback — surfaced in the camera badge. */
  onStatusChange?: (status: LocalFaceWatcherStatus) => void;
};

export type LocalFaceWatcherStatus =
  | { kind: "loading" }
  | { kind: "ready"; activeTrackCount: number }
  | { kind: "error"; reason: string };

// ---------------------------------------------------------------------------
// Module-level singleton: the FaceDetector is ~2 MB of WASM + a 230 KB
// model. We load it once and share it across page mounts.
// ---------------------------------------------------------------------------

let detectorPromise: Promise<FaceDetector> | null = null;

async function getDetector(): Promise<FaceDetector> {
  if (detectorPromise) return detectorPromise;
  detectorPromise = (async () => {
    // Dynamic import keeps tasks-vision (~200 KB minified) out of
    // the admin-pages bundle. The Live AI page is the only consumer.
    const tv = await import("@mediapipe/tasks-vision");
    const fileset = await tv.FilesetResolver.forVisionTasks(WASM_BASE_URL);
    return tv.FaceDetector.createFromOptions(fileset, {
      baseOptions: {
        modelAssetPath: MODEL_URL,
        // BlazeFace short-range is tiny enough that GPU init cost
        // dwarfs the per-frame win on most laptops; CPU is the
        // safer default and what MediaPipe samples ship with.
        delegate: "CPU",
      },
      runningMode: "VIDEO",
      minDetectionConfidence: MIN_DETECTION_SCORE,
    });
  })();
  return detectorPromise;
}

// ---------------------------------------------------------------------------
// Geometry helpers
// ---------------------------------------------------------------------------

function iou(a: BBox, b: BBox): number {
  const x1 = Math.max(a.x, b.x);
  const y1 = Math.max(a.y, b.y);
  const x2 = Math.min(a.x + a.w, b.x + b.w);
  const y2 = Math.min(a.y + a.h, b.y + b.h);
  const interW = Math.max(0, x2 - x1);
  const interH = Math.max(0, y2 - y1);
  const inter = interW * interH;
  if (inter <= 0) return 0;
  const aArea = a.w * a.h;
  const bArea = b.w * b.h;
  const union = aArea + bArea - inter;
  return union > 0 ? inter / union : 0;
}

function bboxFromDetection(det: Detection): BBox | null {
  const b = det.boundingBox;
  if (!b) return null;
  return {
    x: b.originX,
    y: b.originY,
    w: b.width,
    h: b.height,
  };
}

function bestScore(det: Detection): number {
  const cats = det.categories;
  if (!cats || cats.length === 0) return 0;
  return cats[0].score ?? 0;
}

/** Greedy IoU matcher. Returns assignment from detection index →
 *  track id, plus the list of unmatched detections and unmatched
 *  track ids. O(N·M) but N and M are tiny. */
function matchDetectionsToTracks(
  detections: Array<{ bbox: BBox; score: number }>,
  tracks: Map<number, LocalTrack>,
): {
  assignments: Map<number, number>;
  unmatchedDetections: number[];
  unmatchedTrackIds: number[];
} {
  const assignments = new Map<number, number>();
  const remainingDets = new Set<number>(detections.map((_, i) => i));
  const remainingTracks = new Set<number>(tracks.keys());

  type Candidate = { detIdx: number; trackId: number; iou: number };
  const candidates: Candidate[] = [];
  for (const detIdx of remainingDets) {
    for (const trackId of remainingTracks) {
      const t = tracks.get(trackId)!;
      const score = iou(detections[detIdx].bbox, t.bbox);
      if (score >= TRACK_IOU_THRESHOLD) {
        candidates.push({ detIdx, trackId, iou: score });
      }
    }
  }
  candidates.sort((a, b) => b.iou - a.iou);
  for (const c of candidates) {
    if (!remainingDets.has(c.detIdx)) continue;
    if (!remainingTracks.has(c.trackId)) continue;
    assignments.set(c.detIdx, c.trackId);
    remainingDets.delete(c.detIdx);
    remainingTracks.delete(c.trackId);
  }

  return {
    assignments,
    unmatchedDetections: [...remainingDets],
    unmatchedTrackIds: [...remainingTracks],
  };
}

// ---------------------------------------------------------------------------
// Cropping — produce a tight JPEG of one face for backend recognize.
// ---------------------------------------------------------------------------

async function cropBboxToJpeg(
  video: HTMLVideoElement,
  bbox: BBox,
  scratch: HTMLCanvasElement,
): Promise<Blob | null> {
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (vw === 0 || vh === 0) return null;

  const margin = Math.max(bbox.w, bbox.h) * CROP_MARGIN_RATIO;
  const sx = Math.max(0, Math.floor(bbox.x - margin));
  const sy = Math.max(0, Math.floor(bbox.y - margin));
  const sw = Math.min(vw - sx, Math.ceil(bbox.w + 2 * margin));
  const sh = Math.min(vh - sy, Math.ceil(bbox.h + 2 * margin));
  if (sw <= 0 || sh <= 0) return null;

  const longEdge = Math.max(sw, sh);
  const scale = longEdge > CROP_MAX_DIM ? CROP_MAX_DIM / longEdge : 1;
  const dw = Math.max(1, Math.round(sw * scale));
  const dh = Math.max(1, Math.round(sh * scale));

  scratch.width = dw;
  scratch.height = dh;
  const ctx = scratch.getContext("2d");
  if (!ctx) return null;
  ctx.drawImage(video, sx, sy, sw, sh, 0, 0, dw, dh);
  return await new Promise<Blob | null>((resolve) =>
    scratch.toBlob(resolve, "image/jpeg", 0.85),
  );
}

// ---------------------------------------------------------------------------
// React hook — drives the per-frame detect loop.
// ---------------------------------------------------------------------------

export type UseLocalFaceWatcherOptions = {
  videoRef: React.RefObject<HTMLVideoElement>;
  enabled: boolean;
  callbacks: LocalFaceWatcherCallbacks;
};

export function useLocalFaceWatcher({
  videoRef,
  enabled,
  callbacks,
}: UseLocalFaceWatcherOptions) {
  // Latest callbacks live in refs so the long-lived effect below
  // doesn't tear down + re-create the detect loop every render.
  const cbRef = useRef(callbacks);
  cbRef.current = callbacks;

  useEffect(() => {
    if (!enabled) {
      cbRef.current.onStatusChange?.({ kind: "ready", activeTrackCount: 0 });
      return;
    }

    let cancelled = false;
    let timer: number | null = null;
    const tracks = new Map<number, LocalTrack>();
    let nextTrackId = 1;
    const scratchCanvas = document.createElement("canvas");
    let detector: FaceDetector | null = null;

    // Dedupe status emissions — without this we publish a fresh
    // {kind:"ready",activeTrackCount:N} object on every 250ms tick,
    // which made every consumer that listed the status in a
    // useEffect dep array tear down + rebuild on every tick. The
    // FaceRecognitionDriver's 8-second "unknown track retry"
    // timer was the casualty: its effect re-mounted at 4 Hz and
    // never let the 8 s setInterval fire, so a face that came in
    // as `no_face_in_frame` on its first /recognize was never
    // re-probed and the user was silently never greeted.
    let lastStatus: LocalFaceWatcherStatus | null = null;
    const setStatus = (s: LocalFaceWatcherStatus) => {
      if (
        lastStatus &&
        lastStatus.kind === s.kind &&
        (s.kind !== "ready" ||
          (lastStatus as { activeTrackCount: number }).activeTrackCount ===
            s.activeTrackCount) &&
        (s.kind !== "error" ||
          (lastStatus as { reason: string }).reason ===
            (s as { reason: string }).reason)
      ) {
        return;
      }
      lastStatus = s;
      cbRef.current.onStatusChange?.(s);
    };

    const tick = async () => {
      if (cancelled || !detector) return;
      const v = videoRef.current;
      if (!v || v.readyState < 2 || v.videoWidth === 0) {
        timer = window.setTimeout(tick, DETECT_INTERVAL_MS);
        return;
      }

      let detections: Detection[] = [];
      try {
        const result = detector.detectForVideo(v, performance.now());
        detections = result.detections ?? [];
      } catch (e) {
        console.debug("[localFaceWatcher] detect failed", e);
        timer = window.setTimeout(tick, DETECT_INTERVAL_MS);
        return;
      }

      const now = Date.now();
      const filtered: Array<{ bbox: BBox; score: number }> = [];
      for (const d of detections) {
        const bbox = bboxFromDetection(d);
        if (!bbox) continue;
        const score = bestScore(d);
        if (score < MIN_DETECTION_SCORE) continue;
        filtered.push({ bbox, score });
      }

      const { assignments, unmatchedDetections, unmatchedTrackIds } =
        matchDetectionsToTracks(filtered, tracks);

      // Update matched tracks in place.
      for (const [detIdx, trackId] of assignments) {
        const t = tracks.get(trackId);
        const det = filtered[detIdx];
        if (!t) continue;
        // Light EMA so jittery bboxes don't trigger crop refreshes.
        const alpha = 0.6;
        t.bbox = {
          x: alpha * det.bbox.x + (1 - alpha) * t.bbox.x,
          y: alpha * det.bbox.y + (1 - alpha) * t.bbox.y,
          w: alpha * det.bbox.w + (1 - alpha) * t.bbox.w,
          h: alpha * det.bbox.h + (1 - alpha) * t.bbox.h,
        };
        t.score = det.score;
        t.lastSeenAt = now;
      }

      // Birth: new tracks for unmatched detections, fire onTrackAppeared.
      for (const detIdx of unmatchedDetections) {
        const det = filtered[detIdx];
        const id = nextTrackId++;
        const track: LocalTrack = {
          id,
          bbox: det.bbox,
          score: det.score,
          firstSeenAt: now,
          lastSeenAt: now,
        };
        tracks.set(id, track);

        try {
          const blob = await cropBboxToJpeg(v, det.bbox, scratchCanvas);
          if (blob && !cancelled) {
            cbRef.current.onTrackAppeared(track, blob);
          }
        } catch (e) {
          console.debug("[localFaceWatcher] crop failed", e);
        }
      }

      // Death: drop tracks whose last detection is older than the TTL.
      for (const trackId of unmatchedTrackIds) {
        const t = tracks.get(trackId);
        if (!t) continue;
        if (now - t.lastSeenAt > TRACK_TTL_MS) {
          tracks.delete(trackId);
          cbRef.current.onTrackLost?.(trackId);
        }
      }

      setStatus({ kind: "ready", activeTrackCount: tracks.size });

      if (!cancelled) {
        timer = window.setTimeout(tick, DETECT_INTERVAL_MS);
      }
    };

    setStatus({ kind: "loading" });
    getDetector()
      .then((d) => {
        if (cancelled) return;
        detector = d;
        setStatus({ kind: "ready", activeTrackCount: 0 });
        void tick();
      })
      .catch((e) => {
        // Detector failed to load — typically because the WASM /
        // model files weren't shipped (offline first-run before the
        // postinstall script could reach Google's CDN). Surface the
        // error so the camera panel can fall back to backend-only
        // polling instead of going silent forever.
        console.warn(
          "[localFaceWatcher] detector init failed; falling back",
          e,
        );
        cbRef.current.onStatusChange?.({
          kind: "error",
          reason: e instanceof Error ? e.message : String(e),
        });
      });

    return () => {
      cancelled = true;
      if (timer != null) window.clearTimeout(timer);
      // Notify any remaining tracks as lost so the UI can clear
      // identity badges on camera-off / tab navigation.
      for (const trackId of tracks.keys()) {
        cbRef.current.onTrackLost?.(trackId);
      }
    };
  }, [enabled, videoRef]);
}
