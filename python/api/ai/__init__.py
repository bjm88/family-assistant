"""Local AI assistant subsystem.

Exposes three independent pieces used by ``/api/aiassistant/*``:

* ``face``  — InsightFace-based enrollment + recognition.
* ``ollama`` — thin HTTP client for the local Ollama daemon.
* ``rag``   — pulls the "detailed profile" (person + goals + relationships)
              that we hand to the LLM for context-aware greetings.
"""
