"""Long-lived background services that run alongside the FastAPI app.

Currently houses :mod:`email_inbox` — the poller that reads Avi's
Gmail inbox and dispatches the agent to reply. Wire-up lives in
:mod:`api.main`'s startup / shutdown hooks.
"""
