"""Domain-grouped tool handlers.

Each module here exposes a small set of ``_handle_*`` coroutines plus
their JSON-Schema payload constants. The registry assembly lives in
:mod:`api.ai.tools._default_registry`; this package is just where the
actual work happens.

Modules
-------

* :mod:`api.ai.tools.handlers.sql`       — ``sql_query`` + ``lookup_person``
* :mod:`api.ai.tools.handlers.secrets`   — ``reveal_sensitive_identifier`` + ``reveal_secret``
* :mod:`api.ai.tools.handlers.calendar`  — every ``calendar_*`` tool
* :mod:`api.ai.tools.handlers.tasks`     — every ``task_*`` tool
* :mod:`api.ai.tools.handlers.messaging` — ``gmail_send`` + ``telegram_invite``
* :mod:`api.ai.tools.handlers.web`       — ``web_search``

Adding a new domain: drop a new module here, register its tools in
``_default_registry.build_default_registry``, and (if it defines new
capability flags) update ``_default_registry.detect_capabilities``.
"""
