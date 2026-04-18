# Family Assistant

A local Family Assistant AI with a private admin console for managing household
people, identity records, vehicles, insurance policies, financial accounts, and
documents, plus (in future milestones) a visual avatar assistant named **Avi**
that uses the local camera for face recognition and coordinates tasks via LLMs.

```
family-assistant/
├── python/api/          FastAPI backend, SQLAlchemy models, Alembic migrations
├── ui/react/            Vite + React + TypeScript admin console
├── resources/family/    Uploaded photos and documents (gitignored, private)
├── alembic.ini          Migration config
├── pyproject.toml       Python deps, managed by uv
└── .env                 DB + encryption secrets (gitignored)
```

## First-time setup

### 1. Command-line tools

```bash
xcode-select --install
```

### 2. Python (backend)

```bash
brew install uv
uv python pin 3.12
uv sync
```

### 3. Postgres (local database)

```bash
brew install postgresql@16
brew services start postgresql@16
# one-time: create the role and database
createuser -s family_assistant
createdb -O family_assistant family_assistant
psql -d family_assistant -c "ALTER USER family_assistant WITH PASSWORD 'Avi123!';"
```

The connection settings live in `.env` as `FA_DB_HOST`, `FA_DB_PORT`,
`FA_DB_USER`, `FA_DB_PWD`, `FA_DB_NAME`.

### 4. Node (frontend)

```bash
brew install node
cd ui/react && npm install
```

### 5. Configure secrets in `.env`

Generate a Fernet key for encrypting sensitive columns:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the output into `.env` as the value of `FA_ENCRYPTION_KEY`.
Never commit `.env`. If you lose the key you will not be able to decrypt
existing SSNs, policy numbers, VINs, or account numbers.

### 6. Run database migrations

```bash
uv run alembic upgrade head
```

This creates every table and a read-only `llm_schema_catalog` view that
exposes each table/column along with its natural-language description.
A local LLM can `SELECT * FROM llm_schema_catalog` to discover the schema
when generating dynamic SQL.

## Running

Open two terminals:

```bash
# terminal 1 — backend (http://localhost:8000, docs at /docs)
uv run uvicorn api.main:app --app-dir python --reload
```

```bash
# terminal 2 — frontend (http://localhost:5173)
cd ui/react && npm run dev
```

Visit <http://localhost:5173>. Create your first family, add people, upload
profile photos, and start filling in vehicles, insurance, finances, and
documents.

## Architecture

### Data model (LLM-friendly)

Table and column names are **verbose natural-language snake_case**, and every
table/column has a Postgres `COMMENT` describing what it holds. This makes
dynamic SQL generation by a local LLM reliable — the LLM can read
`llm_schema_catalog` and see things like:

- `people.primary_family_relationship` — "spouse, child, parent, …" (convenience label — the authoritative tree lives in `person_relationships`)
- `person_relationships.relationship_type` — atomic edges: `parent_of` (directional) and `spouse_of` (symmetric); siblings/cousins/etc. are derived
- `person_photos.use_for_face_recognition` — flags which photos Avi should use for enrollment
- `insurance_policies.policy_type` — "auto, home, renters, health, …"
- `vehicles.registration_expiration_date` — "Used by Avi to proactively warn …"

Tables today: `families`, `people`, `addresses`, `identity_documents`,
`sensitive_identifiers`, `vehicles`, `insurance_policies`,
`insurance_policy_people`, `insurance_policy_vehicles`, `financial_accounts`,
`documents`.

### Encryption of sensitive columns

- Sensitive values (SSN, policy numbers, account/routing numbers, VINs,
  license plates, ID document numbers) are encrypted at the application
  layer with **Fernet** (AES-128-CBC + HMAC-SHA256) using `FA_ENCRYPTION_KEY`.
- Ciphertext is stored in `*_encrypted` `bytea` columns. The plaintext is
  **never** logged, returned from the API, or usable from SQL.
- For display and for LLM-generated SQL filters we keep a paired plaintext
  `*_last_four` column (e.g. `policy_number_last_four`, `account_number_last_four`).
  The LLM writes queries like `WHERE policy_number_last_four = '1234'` without
  ever touching ciphertext.

### Backend (`python/api/`)

- `config.py` — settings from `.env` via pydantic-settings.
- `db.py` — SQLAlchemy 2.0 engine + session.
- `crypto.py` — Fernet encrypt/decrypt helpers.
- `storage.py` — filesystem storage for photos/documents under
  `resources/family/<family_id>/…`.
- `models/` — one file per table. Every column has a `comment=` that is
  materialized into Postgres `COMMENT ON COLUMN`.
- `schemas/` — Pydantic request/response shapes; encrypted columns are
  never exposed.
- `routers/` — FastAPI routers, one per resource, with full CRUD.
- `migrations/` — Alembic.

### Frontend (`ui/react/`)

- Vite + React 18 + TypeScript + Tailwind.
- `react-router-dom` for navigation, `@tanstack/react-query` for data
  fetching, `react-hook-form` for forms, `lucide-react` for icons.
- Dev server proxies `/api` → `http://localhost:8000`, so there is no
  CORS friction in development.
- Pages: families list, family dashboard, people list + detail with photo
  upload, identity documents, sensitive identifiers, vehicles, insurance
  policies, financial accounts, documents.

## Common operations

```bash
# create a new migration after editing models
uv run alembic revision --autogenerate -m "describe your change"
uv run alembic upgrade head

# roll back one revision
uv run alembic downgrade -1

# browse the API
open http://localhost:8000/docs

# inspect the LLM schema catalog
psql -U family_assistant -d family_assistant \
  -c "SELECT table_name, column_name, column_description FROM llm_schema_catalog;"
```

## Roadmap (not yet built)

- **Avi assistant**: visual avatar, microphone + speaker, local camera
  face-recognition pipeline, wake word, voice synthesis.
- **LLM coordination layer**: Claude / local LLM tool-use that reads
  `llm_schema_catalog` and generates parameterized SQL against the private
  database, with a decrypt-by-id tool for the handful of legitimate cases
  that need plaintext sensitive values.
- **Email / research / spreadsheet automations**: delegated to Claude Code
  via tool invocations from the assistant.
- **Multi-user auth / per-person access policies** — currently the app
  assumes local single-machine trust.
