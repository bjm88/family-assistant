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

    # ---- Tiered messaging: fast acknowledgement before heavy reply -----
    # Push-style surfaces (Telegram, SMS) leave the user staring at a
    # silent chat for the 5-30 s the heavyweight agent needs to respond.
    # When this flag is on, every inbound that's likely to take a while
    # gets a quick "I'm looking into that..." ack from the lightweight
    # model first, then the full answer arrives as a follow-up message
    # once the agent loop converges. Set false to revert to the original
    # single-reply behaviour.
    AI_FAST_ACK_ENABLED: bool = True
    # Race window. We start the heavy agent immediately and only fire
    # the fast-model ack if the agent hasn't finished within this many
    # seconds. Tune to taste: shorter = more acks (chattier UX), longer
    # = fewer acks (sometimes silent for several seconds).
    AI_FAST_ACK_AFTER_SECONDS: float = 3.0
    # Hard ceiling on how long the fast model itself may run before we
    # give up on the ack and stay silent. ``gemma4:e2b`` warm runs in
    # ~300–700 ms on Apple Silicon, but a cold load (first request
    # of the day, or after Ollama unloads it) can take 3–4 s before
    # the first token. Six seconds gives the cold path enough room
    # without letting a truly stuck call drag the user-visible
    # latency past the heavy reply itself. The lifespan warmup
    # (`api.ai.ollama.warmup_model`) plus `keep_alive=1h` on every
    # ack call keep the model warm so this cap rarely matters.
    AI_FAST_ACK_TIMEOUT_SECONDS: float = 6.0
    # Bound on concurrent heavy-agent runs across all messaging
    # surfaces. Ollama serialises GPU access anyway, so making this
    # large mostly wastes RAM — but a small pool lets a Telegram
    # ack fire even while a long-running SMS agent is still mid-flight.
    AI_BACKGROUND_AGENT_MAX_WORKERS: int = 4
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

    # ---- Email inbox poller --------------------------------------------
    # When ON, a background coroutine polls every connected assistant's
    # Gmail inbox for unread messages from REGISTERED family members and
    # replies via the agent loop. Set OFF to disable Avi's email
    # autopilot entirely (the OAuth connection itself remains intact).
    AI_EMAIL_INBOX_ENABLED: bool = True
    # Polling cadence in seconds. Gmail's per-user quota is generous so
    # 30-60 s feels responsive without being noisy. Set very high (e.g.
    # 3600) to effectively disable while keeping the option to bump it
    # down without a restart in the future.
    AI_EMAIL_INBOX_POLL_SECONDS: int = 60
    # Max unread messages fetched per poll cycle. Keeps a backlog from
    # spawning dozens of agent runs at once if a flood arrives.
    AI_EMAIL_INBOX_MAX_PER_TICK: int = 5

    # ---- SMS inbox (Twilio) --------------------------------------------
    # Master switch for the inbound SMS webhook. When OFF the
    # ``/api/sms/twilio/inbound`` endpoint still exists but returns an
    # empty TwiML response without invoking the agent loop — useful for
    # silencing replies while debugging without disconnecting the number
    # at Twilio.
    AI_SMS_INBOUND_ENABLED: bool = True
    # Twilio account / auth credentials. The auth token signs every
    # webhook so we can verify the request really came from Twilio
    # (rather than someone hitting the endpoint with a forged form
    # post). When the token is empty we *log a warning and accept the
    # request anyway* — that mode is for local-dev only; in production
    # you must set TWILIO_AUTH_TOKEN.
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    # The Twilio phone number Avi sends from. Replies will use this
    # number even if the inbound came in on a different "To" line, so
    # only set TWILIO_PRIMARY_PHONE to a number you actually own.
    TWILIO_PRIMARY_PHONE: Optional[str] = None
    # Public URL Twilio uses to reach our webhook, e.g.
    # https://your-tunnel.ngrok.app/api/sms/twilio/inbound — must match
    # exactly because it is part of the X-Twilio-Signature input. Leave
    # unset to derive it from the incoming request URL (works in most
    # cases but breaks if there is a load balancer between Twilio and
    # us that rewrites the host header).
    TWILIO_WEBHOOK_PUBLIC_URL: Optional[str] = None
    # Hard cap on per-message body length when we send a reply. SMS
    # itself supports up to 1600 chars (= 10 segments) but most users
    # appreciate brevity over a wall of text — pick a number you'd be
    # OK reading on a phone screen.
    AI_SMS_REPLY_MAX_CHARS: int = 480

    # ---- Telegram inbox ------------------------------------------------
    # Master switch for the Telegram long-poll loop. When OFF the bot
    # poller never starts and inbound messages pile up on Telegram's
    # side until the loop is re-enabled (Telegram retains undelivered
    # updates for ~24 h).
    AI_TELEGRAM_INBOUND_ENABLED: bool = True
    # Bot token from BotFather (e.g. ``123456:ABCdef...``). When unset
    # the poller logs once at startup and stays idle — Telegram is the
    # only surface that hard-requires a credential to do anything at
    # all, so we degrade gracefully rather than crash.
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    # Long-poll timeout passed to getUpdates. Telegram holds the
    # request open for up to this many seconds when there's nothing
    # new, which is the cheapest way to get near-instant delivery
    # without webhooks or polling-storms.
    AI_TELEGRAM_LONGPOLL_SECONDS: int = 25
    # Hard cap on updates fetched per getUpdates call. Each one runs
    # the full agent pipeline so a flood from one chatty user can't
    # spawn dozens of LLM runs in parallel.
    AI_TELEGRAM_INBOX_MAX_PER_TICK: int = 10
    # Soft cap on outbound message length. Telegram allows 4096; we
    # keep replies friendlier-sized by default but bump higher than
    # SMS because Telegram has no carrier-segmenting cost.
    AI_TELEGRAM_REPLY_MAX_CHARS: int = 3500

    # When the inbox sees a sender it doesn't recognise, optionally
    # reply with a one-tap "Share my phone number" button so Avi can
    # auto-bind that Telegram identity to a Person row whose
    # mobile/home/work phone matches. Disable to fall back to the
    # original silent-drop behaviour (only out-of-band invites can
    # link a sender). Telegram never exposes a sender's phone or
    # email through any other Bot API surface — explicit consent via
    # this prompt is the only path.
    AI_TELEGRAM_AUTO_LINK_BY_PHONE: bool = True
    # Don't re-prompt the same chat for a contact share more than
    # once inside this window. Prevents Avi from spamming a button
    # at every "what?" message a stranger sends.
    AI_TELEGRAM_CONTACT_PROMPT_COOLDOWN_HOURS: int = 24

    # Two-factor verification of a Telegram contact share via Twilio
    # SMS. The phone number inside `message.contact` is supplied by
    # the user's client and a custom MTProto client could forge it,
    # so we don't trust the contact share alone. Instead we text a
    # one-time code to the matched Person.mobile_phone_number and
    # require the user to echo it back into Telegram before binding
    # `Person.telegram_user_id`.
    #
    # TTL is the wall-clock deadline for replying with the code;
    # 10 min covers "switch apps, copy the code, switch back" without
    # leaving stale challenges around for hours. Max attempts gives
    # the user room for typos but caps brute-force at a 5e-6 success
    # rate against a 6-digit keyspace. Code length matches the
    # universal SMS-2FA convention.
    AI_TELEGRAM_VERIFY_TTL_MINUTES: int = 10
    AI_TELEGRAM_VERIFY_MAX_ATTEMPTS: int = 5
    AI_TELEGRAM_VERIFY_CODE_LENGTH: int = 6

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
