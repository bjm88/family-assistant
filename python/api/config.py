"""Application configuration loaded from environment variables.

All family-assistant-specific env vars are prefixed with FA_. The values are
validated by pydantic-settings and exposed via ``get_settings()`` as a cached
singleton so every subsystem (API, Alembic migrations, CLI tools) reads the
exact same values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    FA_DB_HOST: str = "localhost"
    FA_DB_PORT: int = 5432
    FA_DB_USER: str = "family_assistant"
    FA_DB_PWD: str = ""
    FA_DB_NAME: str = "family_assistant"

    FA_ENCRYPTION_KEY: str = Field(
        default="",
        description=(
            "url-safe base64 32-byte Fernet key used to encrypt sensitive "
            "columns (SSNs, account numbers, VINs, etc.)."
        ),
    )

    FA_STORAGE_ROOT: str = "./resources/family"
    FA_CORS_ORIGINS: str = "http://localhost:5173"

    # Third-party model providers. These are unprefixed because they are
    # shared with other experiments in the same repo.
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_PROJECT_ID: Optional[str] = None

    # ---- Google OAuth (Gmail + Calendar for the assistant) -------------
    # Created in Google Cloud Console under
    #   APIs & Services → Credentials → Create OAuth client ID
    #     Application type: Web application
    #     Authorized redirect URI: <GOOGLE_OAUTH_REDIRECT_URI>
    # The redirect URI must match exactly. ``http://localhost`` is allowed
    # by Google specifically for desktop/local-dev workflows; no public
    # domain is required.
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None
    GOOGLE_OAUTH_CLIENT_SECRET: Optional[str] = None
    GOOGLE_OAUTH_REDIRECT_URI: str = (
        "http://localhost:8000/api/admin/google/oauth/callback"
    )
    # After the callback succeeds, where to send the user's browser back
    # to in the React admin app. Leave at the default for ``npm run dev``;
    # adjust if you serve the UI from a different origin.
    GOOGLE_OAUTH_POST_LOGIN_REDIRECT: str = "http://localhost:5173/admin"

    # ---- Local AI assistant (Avi) --------------------------------------
    # Base URL for the local Ollama daemon that hosts the chat LLM.
    AI_OLLAMA_HOST: str = "http://localhost:11434"
    # Model pulled via ``ollama pull <name>``. Default matches what the
    # user has running (``gemma4``); override in .env to point at any
    # other tag like ``gemma3:27b`` or a custom local model.
    AI_OLLAMA_MODEL: str = "gemma4"
    # Lightweight companion model for fast/structured tasks: the RAG
    # planner ("which SELECTs should I run?"), greeting follow-up
    # generation, query classification, etc. Pick something an order
    # of magnitude faster than the main chat model — ``gemma4:e2b`` is
    # the natural choice when the user has pulled it; falls back to
    # the main model if this one isn't installed in Ollama.
    AI_OLLAMA_FAST_MODEL: str = "gemma4:e2b"
    # Cosine-similarity threshold for a face recognition match. Higher =
    # stricter. 0.40–0.45 is a good default for InsightFace buffalo_l
    # embeddings (ArcFace, 512-dim).
    AI_FACE_MATCH_THRESHOLD: float = 0.40
    # Apple-Silicon / Mac Studio optimization. When true we initialize
    # InsightFace with the CoreML execution provider so face detection +
    # embedding runs on the GPU / ANE instead of CPU. Set to false on a
    # plain Linux box; the code auto-falls-back to CPU if CoreML isn't
    # actually available.
    AI_MAC_STUDIO_OPTIMIZED: bool = True
    # Where InsightFace stores downloaded model packs (~300 MB on first run).
    AI_INSIGHTFACE_HOME: Optional[str] = None

    # ---- Local text-to-speech ------------------------------------------
    # Master switch for on-device TTS. When false the ``/tts`` endpoint
    # still responds but returns 503, and the UI falls back to silent text.
    AI_TTS_ENABLED: bool = True
    # Which engine to use. Only "kokoro" is implemented today; "chattts"
    # is reserved for a richer expressive voice later.
    AI_TTS_ENGINE: str = "kokoro"
    # Default voice pack. Kokoro ships dozens (af_bella, af_nicole,
    # am_adam, bm_lewis, …). The assistant's ``gender`` picks a gendered
    # default when this is left at "auto".
    AI_TTS_VOICE: str = "auto"
    # Playback speed multiplier. 1.0 = natural, 1.1 feels a touch snappier.
    AI_TTS_SPEED: float = 1.0
    # Where Kokoro ONNX weights + voice pack live. Lazy-downloaded on
    # first use. Relative paths are resolved against the project root.
    AI_TTS_MODEL_DIR: str = "./resources/models/kokoro"

    # Minutes of inactivity after which a live AI-assistant session is
    # automatically closed with end_reason="timeout". Activity = new
    # participant, new message, or an explicit ensure-active ping from
    # the live page. Override via env when testing: AI_LIVE_SESSION_IDLE_MINUTES=2
    AI_LIVE_SESSION_IDLE_MINUTES: int = 30

    # Dynamic-SQL planner. When true the chat endpoint makes a quick
    # non-streaming LLM call before each message asking which SELECT
    # queries (if any) to run for additional context. With a 26B model
    # this adds 5-10 s of latency per turn, which is rarely worth it
    # because the static RAG block already dumps every household entity
    # (people, vehicles, pets, residences, insurance, accounts) into
    # the system prompt. Leave OFF unless you're experimenting with
    # tool-use prompts. The /api/aiassistant/sql endpoint and the
    # underlying sandboxed sql_tool are always available either way.
    AI_RAG_PLANNER_ENABLED: bool = False

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.FA_DB_USER}:{self.FA_DB_PWD}"
            f"@{self.FA_DB_HOST}:{self.FA_DB_PORT}/{self.FA_DB_NAME}"
        )

    @property
    def storage_root(self) -> Path:
        p = Path(self.FA_STORAGE_ROOT)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tts_model_dir(self) -> Path:
        p = Path(self.AI_TTS_MODEL_DIR)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tts_cache_dir(self) -> Path:
        p = self.storage_root / "tts_cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.FA_CORS_ORIGINS.split(",") if o.strip()]

    @field_validator("FA_ENCRYPTION_KEY")
    @classmethod
    def _warn_if_unset(cls, v: str) -> str:
        # Allow empty at import time so alembic/CLI tools can run, but the
        # crypto module will hard-fail on first use if still unset.
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
