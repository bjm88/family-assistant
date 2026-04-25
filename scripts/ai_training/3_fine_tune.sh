#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Fine-tune -> fuse -> GGUF -> Ollama, end to end.
#
# Reads config.yaml for the base model, hyperparams, quantisation, and
# final Ollama tag. Idempotent — each run produces a fresh dated tag
# (e.g. family-fast:2026-04-23) and ALSO updates :latest if configured.
#
# Prereqs (one-time):
#   uv pip install mlx-lm pyyaml       (Apple Silicon only)
#   ollama serve   (already running per the main README)
#
# Usage:
#   ./3_fine_tune.sh           # full pipeline
#   ./3_fine_tune.sh --skip-gguf   # train only (for dev; no Ollama step)
#   SKIP_DATASET_CHECK=1 ./3_fine_tune.sh   # trust existing dataset
# ---------------------------------------------------------------------------
set -euo pipefail

# Resolve the script's own directory so paths work from anywhere.
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$HERE"

# Honour --skip-gguf (dev loop: fine-tune only, skip heavy conversion).
SKIP_GGUF=0
for arg in "$@"; do
    case "$arg" in
        --skip-gguf) SKIP_GGUF=1 ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Config loader — reads config.yaml via a tiny Python helper.
# ---------------------------------------------------------------------------
cfg() {
    uv run python - <<PY
import yaml, sys
with open("config.yaml") as f:
    cfg = yaml.safe_load(f)
keys = "$1".split(".")
v = cfg
for k in keys:
    v = v[k]
print(v)
PY
}

BASE_MODEL="$(cfg base_model)"
OLLAMA_TAG_BASE="$(cfg ollama_tag_base)"
OLLAMA_TAG_LATEST="$(cfg ollama_tag_latest)"
ARTIFACTS_DIR="$(cd "$(cfg artifacts_dir)" 2>/dev/null && pwd || echo "$HERE/artifacts")"
DATASET_DIR="$(cd "$(cfg sft.output_dir)" 2>/dev/null && pwd || echo "$ARTIFACTS_DIR/dataset")"
LORA_RANK="$(cfg lora.rank)"
LORA_LAYERS="$(cfg lora.num_layers)"
LORA_EPOCHS="$(cfg lora.num_epochs)"
LORA_BATCH="$(cfg lora.batch_size)"
LORA_LR="$(cfg lora.learning_rate)"
LORA_REPORT="$(cfg lora.steps_per_report)"
LORA_EVAL="$(cfg lora.steps_per_eval)"
LORA_SEQ="$(cfg lora.max_seq_length)"
QUANT="$(cfg gguf.quantisation)"
LLAMA_CPP_DIR="$(cfg gguf.llama_cpp_dir)"

mkdir -p "$ARTIFACTS_DIR"
# llama_cpp_dir may be relative in config; resolve against HERE so
# subsequent `cd` calls behave.
case "$LLAMA_CPP_DIR" in
    /*) ;;
    *) LLAMA_CPP_DIR="$HERE/$LLAMA_CPP_DIR" ;;
esac

TODAY="$(date +%Y-%m-%d)"
RUN_DIR="$ARTIFACTS_DIR/runs/$TODAY"
ADAPTERS_DIR="$RUN_DIR/adapters"
FUSED_DIR="$RUN_DIR/fused_hf"
GGUF_FILE="$RUN_DIR/${OLLAMA_TAG_BASE}-${TODAY}.${QUANT}.gguf"
MODELFILE="$RUN_DIR/Modelfile"
mkdir -p "$RUN_DIR" "$ADAPTERS_DIR" "$FUSED_DIR"

echo "=================================================================="
echo "Base model:     $BASE_MODEL"
echo "Output tag:     $OLLAMA_TAG_BASE:$TODAY  (+ :${OLLAMA_TAG_LATEST:-<none>})"
echo "Artifacts:      $RUN_DIR"
echo "Dataset:        $DATASET_DIR"
echo "LoRA rank/ep:   $LORA_RANK / $LORA_EPOCHS epochs"
echo "Quantisation:   $QUANT"
echo "=================================================================="

# ---------------------------------------------------------------------------
# Step 0: sanity-check the dataset is present and non-trivial
# ---------------------------------------------------------------------------

if [[ "${SKIP_DATASET_CHECK:-0}" != "1" ]]; then
    for f in train.jsonl valid.jsonl; do
        if [[ ! -s "$DATASET_DIR/$f" ]]; then
            echo "Missing or empty $DATASET_DIR/$f." >&2
            echo "Run: uv run python 2_build_sft_dataset.py" >&2
            exit 3
        fi
    done
    n_train=$(wc -l < "$DATASET_DIR/train.jsonl" | tr -d ' ')
    n_valid=$(wc -l < "$DATASET_DIR/valid.jsonl" | tr -d ' ')
    if [[ "$n_train" -lt 100 ]]; then
        echo "Refusing to train on only $n_train examples. Re-build dataset or set SKIP_DATASET_CHECK=1." >&2
        exit 3
    fi
    echo "Dataset: $n_train train / $n_valid valid"
fi

# ---------------------------------------------------------------------------
# Step 1: LoRA train with mlx_lm
# ---------------------------------------------------------------------------

echo
echo "[1/5] mlx_lm.lora — training adapters"

# Skip if a finished adapter already exists for this run dir — lets
# you re-run the script after fixing a bug in steps 2-5 without
# burning another hour on training. Set FORCE_RETRAIN=1 to bypass.
if [[ -s "$ADAPTERS_DIR/adapters.safetensors" && "${FORCE_RETRAIN:-0}" != "1" ]]; then
    echo "  adapter already present at $ADAPTERS_DIR/adapters.safetensors"
    echo "  → skipping training (set FORCE_RETRAIN=1 to redo)"
else

# mlx-lm >=0.21 dropped the inline --lora-parameters flag and the
# string "all" for --num-layers. We now write a tiny YAML config for
# the LoRA-specific params and translate "all" -> -1 so the new CLI
# is happy. Everything else still flows through CLI flags.
case "$LORA_LAYERS" in
    all|All|ALL) MLX_NUM_LAYERS=-1 ;;
    *) MLX_NUM_LAYERS="$LORA_LAYERS" ;;
esac

MLX_LORA_CONFIG="$RUN_DIR/mlx_lora.yaml"
cat > "$MLX_LORA_CONFIG" <<MLX_EOF
# Auto-generated by 3_fine_tune.sh — do not hand-edit.
lora_parameters:
  rank: $LORA_RANK
  scale: 20.0
  dropout: 0.0
MLX_EOF

uv run mlx_lm.lora \
    --config "$MLX_LORA_CONFIG" \
    --train \
    --model "$BASE_MODEL" \
    --data "$DATASET_DIR" \
    --batch-size "$LORA_BATCH" \
    --num-layers "$MLX_NUM_LAYERS" \
    --iters "$(( LORA_EPOCHS * 1000 ))" \
    --learning-rate "$LORA_LR" \
    --steps-per-report "$LORA_REPORT" \
    --steps-per-eval "$LORA_EVAL" \
    --max-seq-length "$LORA_SEQ" \
    --adapter-path "$ADAPTERS_DIR" \
    --fine-tune-type lora

fi  # end skip-if-adapter-exists guard

# ---------------------------------------------------------------------------
# Step 2: fuse the LoRA adapter into the base weights
# ---------------------------------------------------------------------------

echo
echo "[2/5] mlx_lm.fuse — merging adapters into base weights (HF format)"
# mlx-lm >=0.21 renamed --de-quantize -> --dequantize (no hyphen).
# We don't use --export-gguf here even though mlx-lm 0.31 supports it
# for llama — keeping the convert step in llama.cpp ensures parity
# with whatever Ollama runs at inference time. (We also need
# llama.cpp anyway for quantisation.)
uv run mlx_lm.fuse \
    --model "$BASE_MODEL" \
    --adapter-path "$ADAPTERS_DIR" \
    --save-path "$FUSED_DIR" \
    --dequantize

if [[ "$SKIP_GGUF" -eq 1 ]]; then
    echo
    echo "--skip-gguf set — stopping after fuse. Fused HF weights at $FUSED_DIR"
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 3: llama.cpp — convert HF → GGUF and quantise
# ---------------------------------------------------------------------------

echo
echo "[3/5] llama.cpp convert + quantise"
if [[ ! -d "$LLAMA_CPP_DIR/.git" ]]; then
    echo "  cloning llama.cpp into $LLAMA_CPP_DIR"
    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_CPP_DIR"
fi

# Build the quantise binary if missing. Modern llama.cpp uses CMake.
if [[ ! -x "$LLAMA_CPP_DIR/build/bin/llama-quantize" ]]; then
    echo "  building llama-quantize (first run only)"
    ( cd "$LLAMA_CPP_DIR" && cmake -B build -DCMAKE_BUILD_TYPE=Release >/dev/null \
      && cmake --build build --config Release -j --target llama-quantize >/dev/null )
fi

CONVERT_PY="$LLAMA_CPP_DIR/convert_hf_to_gguf.py"

# Llama 3.x tokenizers are recognised natively by convert_hf_to_gguf.py
# (no pre-tokenizer-hash patching required, unlike our previous Gemma 3
# attempt). If you swap base_model in config.yaml to a model whose
# tokenizer hash isn't in llama.cpp's whitelist, convert will exit
# with "BPE pre-tokenizer was not recognized" — at which point you'll
# need to add a small whitelist patch here for that model's hash.

# Python deps for convert script.
uv run --with "sentencepiece,transformers,torch,safetensors,numpy,protobuf" \
    python "$CONVERT_PY" "$FUSED_DIR" \
        --outfile "$RUN_DIR/fused.gguf" \
        --outtype f16

"$LLAMA_CPP_DIR/build/bin/llama-quantize" \
    "$RUN_DIR/fused.gguf" "$GGUF_FILE" "$QUANT"

rm -f "$RUN_DIR/fused.gguf"   # free ~8 GB
echo "  final GGUF: $GGUF_FILE ($(du -h "$GGUF_FILE" | awk '{print $1}'))"

# ---------------------------------------------------------------------------
# Step 4: Ollama Modelfile + register
# ---------------------------------------------------------------------------

echo
echo "[4/5] ollama create"
cat > "$MODELFILE" <<MODELFILE_EOF
# Generated by scripts/ai_training/3_fine_tune.sh on $TODAY
# Base: $BASE_MODEL
# LoRA rank: $LORA_RANK, epochs: $LORA_EPOCHS
FROM $GGUF_FILE

# Keep the family-assistant persona aligned with ai/ollama.py's
# system_prompt_for_avi(). The fine-tune itself learned Avi's voice,
# but SYSTEM also acts as a safety net at inference time. Llama 3
# supports the system role natively, so this is honoured directly.
SYSTEM """You are Avi, the family-assistant. Answer household questions directly using the structured family data in the prompt. Keep replies short, natural, spoken-English, no markdown. When the ask needs a tool call, attachment analysis, web search, or multi-step research, briefly say you'll hand it off to the full agent."""

# Llama 3.2's recommended sampling per Meta's model card is
# temp=0.6 / top_p=0.9. We pull temperature lower because the fast
# tier prizes recall fidelity (correct names/dates from RAG context)
# over creative phrasing.
PARAMETER temperature 0.3
PARAMETER top_p 0.9
PARAMETER num_ctx 4096
# Llama 3 chat template terminates each turn with <|eot_id|>. Without
# this stop token, Ollama will keep generating past the assistant
# turn into a hallucinated user/assistant exchange.
PARAMETER stop "<|eot_id|>"
PARAMETER stop "<|end_of_text|>"
PARAMETER stop "<|start_header_id|>"
MODELFILE_EOF

DATED_TAG="${OLLAMA_TAG_BASE}:${TODAY}"
ollama create "$DATED_TAG" -f "$MODELFILE"
echo "  registered $DATED_TAG"

if [[ -n "${OLLAMA_TAG_LATEST:-}" && "$OLLAMA_TAG_LATEST" != "None" ]]; then
    LATEST_TAG="${OLLAMA_TAG_BASE}:${OLLAMA_TAG_LATEST}"
    ollama create "$LATEST_TAG" -f "$MODELFILE"
    echo "  registered $LATEST_TAG"
fi

# ---------------------------------------------------------------------------
# Step 5: smoke test
# ---------------------------------------------------------------------------

echo
echo "[5/5] smoke test — one-shot inference"
ollama run "$DATED_TAG" "What does the goals table hold?" | head -20 || true

echo
echo "=================================================================="
echo "Done. To activate the new model in the live app, set"
echo "  AI_FAMILY_QA_MODEL=${OLLAMA_TAG_BASE}:${OLLAMA_TAG_LATEST:-$TODAY}"
echo "in .env and restart the backend:"
echo "  ../restart.sh backend"
echo "=================================================================="
