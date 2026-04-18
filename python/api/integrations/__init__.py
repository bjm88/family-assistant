"""Adapters for third-party model/providers used by the family assistant.

Each adapter lives in its own module and reads its own credentials from
``config.get_settings()``. Keep adapters thin and provider-specific — higher
level orchestration (assistant avatars, RAG pipelines, etc.) composes them
in domain modules.
"""
