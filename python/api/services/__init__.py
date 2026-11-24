"""Long-lived background services that run alongside the FastAPI app.

Currently houses :mod:`email_inbox` — the poller that reads Avi's
Gmail inbox and dispatches the agent to reply — and
:mod:`inbound_recovery`, the catch-up pass startup and
``scripts/recover_inbound.py`` share. Wire-up lives in
:mod:`api.main`'s startup / shutdown hooks.
"""
