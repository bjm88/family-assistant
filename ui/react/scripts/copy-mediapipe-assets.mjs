#!/usr/bin/env node
/**
 * Mirror the MediaPipe Tasks Vision WASM bundle and the BlazeFace
 * short-range face-detector model into `public/mediapipe/` so the
 * browser can load them from our own origin instead of a CDN.
 *
 * Why we do this:
 *   - The Family Assistant runs fully on the local machine; the
 *     project's privacy stance is "nothing leaves the box". Pinning
 *     our own copies of the WASM + .task model means the live
 *     /aiassistant page still works after the very first install
 *     completes, even if the machine is offline.
 *   - The WASM ships inside the npm package. We just copy it.
 *   - The `.task` model is hosted by Google on a stable URL. We
 *     fetch it once on install and cache it under public/.
 *
 * This script is idempotent — re-runs are cheap. It's wired up via
 * the `postinstall` hook in package.json and is also safe to run by
 * hand:
 *
 *     node scripts/copy-mediapipe-assets.mjs
 */
import { mkdir, copyFile, readdir, stat, writeFile, access } from "node:fs/promises";
import { constants as fsConst, createWriteStream } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "..");
const wasmSrcDir = resolve(
  repoRoot,
  "node_modules/@mediapipe/tasks-vision/wasm",
);
const wasmDstDir = resolve(repoRoot, "public/mediapipe/wasm");
const modelDstDir = resolve(repoRoot, "public/mediapipe/models");
const modelDstPath = join(modelDstDir, "blaze_face_short_range.tflite");
const modelUrl =
  "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite";

async function exists(path) {
  try {
    await access(path, fsConst.F_OK);
    return true;
  } catch {
    return false;
  }
}

async function copyWasm() {
  if (!(await exists(wasmSrcDir))) {
    console.warn(
      `[mediapipe-assets] source WASM dir missing: ${wasmSrcDir}\n` +
        "  Did `npm install` finish? Skipping WASM copy.",
    );
    return;
  }
  await mkdir(wasmDstDir, { recursive: true });
  const entries = await readdir(wasmSrcDir);
  let copied = 0;
  for (const name of entries) {
    const src = join(wasmSrcDir, name);
    const dst = join(wasmDstDir, name);
    const s = await stat(src);
    if (!s.isFile()) continue;
    await copyFile(src, dst);
    copied += 1;
  }
  console.log(`[mediapipe-assets] copied ${copied} WASM file(s) → ${wasmDstDir}`);
}

async function downloadModel() {
  await mkdir(modelDstDir, { recursive: true });
  if (await exists(modelDstPath)) {
    console.log(
      `[mediapipe-assets] face-detector model already present (${modelDstPath})`,
    );
    return;
  }
  console.log(`[mediapipe-assets] fetching ${modelUrl} …`);
  let res;
  try {
    res = await fetch(modelUrl);
  } catch (e) {
    console.warn(
      "[mediapipe-assets] could not fetch BlazeFace model (offline?). " +
        "The browser will fall back to backend-only face recognition. " +
        "Re-run `node scripts/copy-mediapipe-assets.mjs` once you have " +
        "internet access.\n  reason:",
      e?.message || e,
    );
    return;
  }
  if (!res.ok) {
    console.warn(
      `[mediapipe-assets] HTTP ${res.status} fetching model — skipping. ` +
        "The browser will fall back to backend-only face recognition.",
    );
    return;
  }
  const buf = Buffer.from(await res.arrayBuffer());
  await writeFile(modelDstPath, buf);
  console.log(
    `[mediapipe-assets] saved face-detector model (${(buf.length / 1024).toFixed(
      1,
    )} KB) → ${modelDstPath}`,
  );
}

await copyWasm();
await downloadModel();
