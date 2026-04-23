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
    # Optional third tier — used by AI-owned monitoring tasks
    # (`task_kind="monitoring"`, `owner_kind="ai"`) where a single run
    # can spend 30+ s reasoning over web-search results. Defaults to
    # the heavy model so a fresh install needs no extra `ollama pull`,
    # but you can point this at a beefier reasoner (e.g.
    # ``gpt-oss:120b``, ``qwen2.5:72b``) once it's installed and
    # monitoring runs will use it instead of the conversational model.
    # Empty string means "fall back to AI_OLLAMA_MODEL".
    AI_OLLAMA_THINKING_MODEL: str = ""
    # When true the monitoring agent loop sends ``"think": true`` on
    # /api/chat so Ollama runs the model's extended-reasoning path
    # (Gemma 4 supports this) — slower but markedly better on
    # multi-source research. No effect for models that don't expose
    # the flag; Ollama silently ignores unknown options.
    AI_OLLAMA_THINKING_ENABLED: bool = True

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
    # Polling cadence in seconds. Email is treated as a low-urgency
    # async surface (people don't expect sub-minute replies), so we
    # default to 30 minutes to keep Gmail API quota usage minimal and
    # avoid noisy "tick" log lines. Drop to 60-120 s temporarily when
    # actively testing inbound flows; bump higher (e.g. 3600) to
    # effectively pause the surface without losing the OAuth grant.
    AI_EMAIL_INBOX_POLL_SECONDS: int = 30 * 60
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

    # ---- WhatsApp inbox (Twilio) ---------------------------------------
    # Master switch for the inbound-WhatsApp branch of the Twilio
    # webhook. Off by default — flip to true once
    # TWILIO_WHATSAPP_SENDER_NUMBER is set and the WhatsApp business
    # sender is approved on the Twilio side. When OFF the webhook still
    # logs the inbound (status='failed', status_reason explains the
    # disable) but never invokes the agent loop or sends a reply.
    AI_WHATSAPP_INBOUND_ENABLED: bool = False
    # The WhatsApp Business sender (E.164, e.g. ``+14155238886`` for the
    # Twilio sandbox) Avi sends from. Twilio expects the API to receive
    # this with a ``whatsapp:`` prefix; we add the prefix in
    # ``integrations.twilio_sms.send_whatsapp`` so this value should be
    # the bare E.164 number without the prefix. When unset the webhook
    # records the inbound row but cannot reply.
    TWILIO_WHATSAPP_SENDER_NUMBER: Optional[str] = None
    # WhatsApp permits much longer message bodies than SMS (Twilio caps
    # at 4096 chars for free-form replies). 1024 still keeps replies
    # phone-screen-readable while giving the agent room for richer
    # answers since WhatsApp readers expect chat-style longer messages
    # vs. SMS terseness.
    AI_WHATSAPP_REPLY_MAX_CHARS: int = 1024

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

    # ---- Monitoring tasks (Avi's standing research jobs) ---------------
    # AI-owned tasks of kind "monitoring" run on a cron schedule. The
    # scheduler is a single asyncio loop in
    # ``services/monitoring_scheduler.py`` that wakes every
    # ``AI_MONITORING_TICK_SECONDS`` and submits any due task to the
    # shared background-agent pool. Disable to keep monitoring tasks in
    # the database but skip auto-running them (manual "Run now" still
    # works).
    AI_MONITORING_ENABLED: bool = True
    # How often the scheduler wakes to look for due tasks. 30 s is
    # plenty given the smallest cron resolution is one minute; the
    # only reason to lower this is to make "Run now" feel more
    # responsive on a quiet system (the immediate-run path doesn't
    # depend on the tick).
    AI_MONITORING_TICK_SECONDS: int = 30
    # Default cron expression applied to a new monitoring task when the
    # creator (UI or AI tool) doesn't specify one. Daily at 9am family-
    # local-time is a sensible "get me a fresh briefing each morning"
    # default. Standard 5-field cron: minute, hour, day-of-month,
    # month, day-of-week.
    AI_MONITORING_DEFAULT_CRON: str = "0 9 * * *"
    # Hard ceiling on a single monitoring run's wall-clock time. A
    # runaway research job could otherwise tie up an agent worker
    # forever. The agent loop already enforces per-tool timeouts and
    # ``DEFAULT_MAX_STEPS`` cycles, this is the belt to that
    # suspenders.
    AI_MONITORING_RUN_TIMEOUT_SECONDS: int = 600
    # Max agent reasoning cycles allowed inside a single monitoring
    # run. Chat turns default to ``DEFAULT_MAX_STEPS=5``, but a
    # monitoring job typically needs more — search, read the
    # synthesis, attach citations, post a comment, write the final
    # narrative — and overflowing the budget surfaces as a
    # ``last_run_status='error'`` even when most of the useful work
    # already landed. 10 gives comfortable headroom without letting
    # a wedged agent burn unbounded Ollama time.
    AI_MONITORING_MAX_STEPS: int = 10

    # ---- Web search (for Avi's research tools) -------------------------
    # Pluggable provider behind ``integrations/web_search.py``. Set
    # ``gemini`` (default — uses the existing ``GEMINI_API_KEY`` and
    # Gemini's ``google_search`` tool grounding), ``brave``, ``tavily``,
    # or leave empty to disable the ``web_search`` tool entirely (the
    # agent will say "search not configured" instead of crashing).
    # Adding a new provider is one adapter file.
    FA_SEARCH_PROVIDER: str = "gemini"
    # Brave Search API key — get one free at
    # https://api.search.brave.com (2k queries/mo on the free tier).
    BRAVE_SEARCH_API_KEY: Optional[str] = None
    # Tavily API key — https://tavily.com — purpose-built for AI
    # agents (returns extracted page content alongside the SERP).
    TAVILY_API_KEY: Optional[str] = None
    # Default page size for ``web_search``. The model can request more
    # via the tool args; this is just what it gets when it doesn't ask.
    AI_WEB_SEARCH_DEFAULT_LIMIT: int = 5

    # Fast-path web-search shortcut. When enabled, every inbound user
    # message is first run through the lightweight Gemma classifier
    # (``api.ai.web_search_shortcut``); if it decides the message is
    # a pure web-lookup ask that needs no household context, we skip
    # the heavy agent loop entirely and stream Gemini's grounded
    # answer straight back to the user. Saves ~5-10 s per qualifying
    # turn (no heavy-model invocations, no tool round-trips). Falls
    # through to the normal agent path on classifier "no", on Gemini
    # error, or when the active provider isn't Gemini. Set false to
    # always go through the heavy agent.
    AI_WEB_SEARCH_SHORTCUT_ENABLED: bool = True
    # Hard cap on how long the fast-Gemma classifier may run before
    # we give up and fall through to the heavy agent. The classifier
    # is a one-token reply (`WEB` / `AGENT`) so a warm e2b should
    # finish in 200-500 ms; this cap is the safety net for a cold
    # load. Going over should be RARE — if you see it firing often,
    # the lifespan warmup isn't running or `keep_alive` got cleared.
    AI_WEB_SEARCH_SHORTCUT_CLASSIFIER_TIMEOUT_S: float = 2.5

    # ---- Inbound attachments (vision adapter) --------------------------
    # Master switch for the multi-channel attachment pipeline. When OFF
    # inbound images / PDFs / DOCX still get downloaded and stored, but
    # the agent prompt only sees the filename — no Gemini Vision call,
    # no PDF/DOCX text extraction. Useful kill-switch if Gemini quota
    # runs out or you're auditing model spend.
    AI_VISION_ENABLED: bool = True
    # Gemini model used to caption inbound images. Vision-capable text
    # models work here; ``gemini-2.5-flash`` is the cheapest sweet
    # spot (~1-2 s / image, generous free tier). Override to
    # ``gemini-2.5-pro`` only if you want richer captions and don't
    # mind the latency hit.
    AI_VISION_MODEL: str = "gemini-2.5-flash"
    # Hard upper bound on a single attachment's size, in bytes. Anything
    # bigger is stored on disk but skipped for analysis (we don't want
    # to hand a 50 MB JPEG to Gemini, and a 200-page PDF will hit the
    # context limit anyway). 20 MB matches the largest practical email
    # attachment most providers will accept.
    AI_ATTACHMENT_MAX_BYTES: int = 20 * 1024 * 1024
    # Per-message cap on the number of attachments we describe. Beyond
    # this we still persist them and drop a one-line ``[+N more
    # attachments not analysed]`` note into the prompt so the agent
    # stays aware. Prevents an inbound with 30 photos from spending 60 s
    # on Gemini Vision calls before the agent even starts.
    AI_ATTACHMENT_MAX_PER_MESSAGE: int = 6

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
