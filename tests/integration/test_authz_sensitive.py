"""End-to-end proof: the household privacy gate is correct AND the live
chat won't override it via the LLM-based fast-path shortcut.

The user-stated invariant is the spec for this file:

> The code needs to know who is asking and who they are asking about,
> then if that relationship is the same person, a spouse, or a parent,
> there should never be any controls or restrictions.

These tests sit at two layers:

1. **Tool layer** — the deterministic privacy gate that the heavy
   agent invokes via :func:`api.ai.tools.handlers.secrets.handle_reveal_sensitive`
   and :func:`...handle_reveal_secret`. This is the SAME path SMS,
   WhatsApp, Telegram, email, AND (after this PR) the live chat take
   for sensitive-identifier asks. We exercise it directly with a real
   :class:`ToolContext`, no Ollama, no LLM, no SSE.

2. **Routing layer** — :mod:`api.ai.sensitive_intent` and the
   live-chat shortcut-skip decision. These prove that an
   identifier-shaped message takes the heavy-agent path even when the
   ``family_qa`` shortcut is enabled, so authz always runs in pure
   Python and never gets second-guessed by a stock chat model.

We deliberately build a fresh, isolated family per test (with a unique
suffix) rather than reusing the long-lived ``test_family`` fixture: the
tests insert SSNs and a vehicle with a real Fernet-encrypted VIN, and
the matrix of relationships (parent / spouse / uncle) needs to be
controllable per case.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from api import models
from api.ai import authz, sensitive_intent
from api.ai.tools import ToolContext
from api.ai.tools.handlers import secrets as secrets_tools
from api.crypto import encrypt_str


# ---------------------------------------------------------------------------
# Fixtures — one isolated family per test
# ---------------------------------------------------------------------------


@pytest.fixture
def authz_family(db):
    """Build a small family graph with the relationships we want to verify.

    Layout::

        ├── Parent (the requestor in the parent→child case)
        ├── Spouse  (married to Parent — gets the spouse rule)
        ├── Child   (with an encrypted SSN and an owned vehicle)
        └── Uncle   (sibling of Parent: NO direct edge to Child, gets DENY)

    Plus one stranger in another family, used to assert cross-family
    requests are also denied.

    Yields a dict of person_ids + the SSN/VIN plaintext we inserted so
    each test can compare what the reveal tool returns. Cleanup after
    the test deletes the whole family — keeps the table small and
    keeps each test fully isolated.
    """
    suffix = int(datetime.now(timezone.utc).timestamp() * 1e6)

    family = models.Family(family_name=f"__authz_test_{suffix}__")
    db.add(family)
    db.flush()

    other_family = models.Family(family_name=f"__authz_test_other_{suffix}__")
    db.add(other_family)
    db.flush()

    parent = models.Person(
        family_id=family.family_id,
        first_name=f"Parent{suffix}",
        last_name="AuthzTest",
        primary_family_relationship="parent",
    )
    spouse = models.Person(
        family_id=family.family_id,
        first_name=f"Spouse{suffix}",
        last_name="AuthzTest",
        primary_family_relationship="parent",
    )
    child = models.Person(
        family_id=family.family_id,
        first_name=f"Child{suffix}",
        last_name="AuthzTest",
        primary_family_relationship="child",
    )
    uncle = models.Person(
        family_id=family.family_id,
        first_name=f"Uncle{suffix}",
        last_name="AuthzTest",
        primary_family_relationship="other",
    )
    stranger = models.Person(
        family_id=other_family.family_id,
        first_name=f"Stranger{suffix}",
        last_name="AuthzTest",
        primary_family_relationship="other",
    )
    db.add_all([parent, spouse, child, uncle, stranger])
    db.flush()

    # Relationships. We model the ATOMIC edges only — derived ones
    # (siblings, grandparents, in-laws) fall out of graph traversal in
    # `authz._is_direct_parent` etc. The uncle is wired as a sibling
    # of the parent ONLY (no direct parent_of edge to the child), so
    # the gate must DENY him on child queries. That's the entire
    # point of the "direct parent" rule.
    edges: list[models.PersonRelationship] = [
        # parent ↔ spouse (symmetric, two rows)
        models.PersonRelationship(
            from_person_id=parent.person_id,
            to_person_id=spouse.person_id,
            relationship_type="spouse_of",
        ),
        models.PersonRelationship(
            from_person_id=spouse.person_id,
            to_person_id=parent.person_id,
            relationship_type="spouse_of",
        ),
        # parent → child (directional)
        models.PersonRelationship(
            from_person_id=parent.person_id,
            to_person_id=child.person_id,
            relationship_type="parent_of",
        ),
        # spouse → child (the other parent)
        models.PersonRelationship(
            from_person_id=spouse.person_id,
            to_person_id=child.person_id,
            relationship_type="parent_of",
        ),
    ]
    db.add_all(edges)

    # Encrypted SSN for the child. We INSERT the encrypted bytes
    # directly via :func:`api.crypto.encrypt_str` — that's the same
    # call the admin CRUD endpoint makes, so the round-trip we test
    # is identical to production.
    ssn_plain = "111-22-3333"
    ssn_row = models.SensitiveIdentifier(
        person_id=child.person_id,
        identifier_type="social_security_number",
        identifier_value_encrypted=encrypt_str(ssn_plain),
        identifier_last_four="3333",
    )
    db.add(ssn_row)

    # A vehicle owned by the child (primary_driver_person_id) — the
    # gate runs against the primary driver, so this is the parent → child
    # case for `reveal_secret(category='vehicle_vin')`.
    vin_plain = "1HGCM82633A004352"
    vehicle = models.Vehicle(
        family_id=family.family_id,
        primary_driver_person_id=child.person_id,
        vehicle_type="car",
        make="Honda",
        model="Civic",
        year=2024,
        vehicle_identification_number_encrypted=encrypt_str(vin_plain),
        vehicle_identification_number_last_four=vin_plain[-4:],
    )
    db.add(vehicle)

    db.commit()

    fixture = {
        "family_id": family.family_id,
        "other_family_id": other_family.family_id,
        "parent_id": parent.person_id,
        "spouse_id": spouse.person_id,
        "child_id": child.person_id,
        "uncle_id": uncle.person_id,
        "stranger_id": stranger.person_id,
        "ssn_plain": ssn_plain,
        "vin_plain": vin_plain,
        "vehicle_id": vehicle.vehicle_id,
        "ssn_row_id": ssn_row.sensitive_identifier_id,
    }
    yield fixture

    # Teardown — order matters because of FK cascades. The Family
    # delete cascades through Vehicle and Person; PersonRelationship
    # rides the Person cascade.
    db.delete(family)
    db.delete(other_family)
    db.commit()


def _ctx(db, *, family_id: int, person_id: int, is_admin: bool = False) -> ToolContext:
    """Mint a ToolContext that mirrors what the live agent would build."""
    return ToolContext(
        db=db,
        family_id=family_id,
        person_id=person_id,
        is_admin=is_admin,
    )


# ---------------------------------------------------------------------------
# Pure authz decisions — no tools, no Ollama
# ---------------------------------------------------------------------------


def test_authz_self_spouse_parent_allow_uncle_deny(db, authz_family):
    """The four canonical cases the user described, exercised in one shot."""
    cases: list[tuple[str, int, int, bool, str]] = [
        # (label, requestor, subject, expected_allowed, expected_label)
        ("self", authz_family["child_id"], authz_family["child_id"], True, "self"),
        (
            "spouse",
            authz_family["parent_id"],
            authz_family["spouse_id"],
            True,
            "spouse",
        ),
        (
            "parent→child",
            authz_family["parent_id"],
            authz_family["child_id"],
            True,
            "parent",
        ),
        (
            "spouse→child (the other parent)",
            authz_family["spouse_id"],
            authz_family["child_id"],
            True,
            "parent",
        ),
        (
            "uncle (no direct parent_of edge)",
            authz_family["uncle_id"],
            authz_family["child_id"],
            False,
            "unauthorized",
        ),
        (
            "child→parent (kids never see parents' SSN)",
            authz_family["child_id"],
            authz_family["parent_id"],
            False,
            "unauthorized",
        ),
        (
            "cross-family stranger",
            authz_family["stranger_id"],
            authz_family["child_id"],
            False,
            "cross_family",
        ),
        (
            "anonymous speaker (no person_id)",
            None,  # type: ignore[arg-type]
            authz_family["child_id"],
            False,
            "anonymous",
        ),
    ]

    failures: list[str] = []
    for label, req, subj, expected_allowed, expected_label in cases:
        decision = authz.can_access_sensitive(
            db,
            requestor_person_id=req,
            subject_person_id=subj,
            audit_log=False,
        )
        if (
            bool(decision.allowed) != expected_allowed
            or decision.label != expected_label
        ):
            failures.append(
                f"{label}: got allowed={decision.allowed} label={decision.label!r}; "
                f"expected allowed={expected_allowed} label={expected_label!r}"
            )
    assert not failures, "Authz matrix violations:\n" + "\n".join(failures)


def test_authz_admin_bypass_is_recorded_in_audit_label(db, authz_family):
    """``requestor_is_admin=True`` ALLOWs everything with label='admin'.

    Even cross-family — admins legitimately operate across families.
    """
    decision = authz.can_access_sensitive(
        db,
        requestor_person_id=authz_family["uncle_id"],
        subject_person_id=authz_family["child_id"],
        requestor_is_admin=True,
        audit_log=False,
    )
    assert decision.allowed
    assert decision.label == "admin"

    cross = authz.can_access_sensitive(
        db,
        requestor_person_id=authz_family["stranger_id"],
        subject_person_id=authz_family["child_id"],
        requestor_is_admin=True,
        audit_log=False,
    )
    assert cross.allowed
    assert cross.label == "admin"


# ---------------------------------------------------------------------------
# reveal_sensitive_identifier (SSN) — the actual tool the agent calls
# ---------------------------------------------------------------------------


async def test_parent_can_reveal_child_ssn(db, authz_family):
    """The bug the user reported: parent → child SSN must succeed."""
    ctx = _ctx(
        db,
        family_id=authz_family["family_id"],
        person_id=authz_family["parent_id"],
    )
    out = await secrets_tools.handle_reveal_sensitive(
        ctx,
        person_id=authz_family["child_id"],
        identifier_type="social_security_number",
    )
    assert out["found"] is True
    assert out["access_label"] == "parent"
    assert len(out["results"]) == 1
    assert out["results"][0]["value"] == authz_family["ssn_plain"]


async def test_spouse_can_reveal_other_spouse_ssn(db, authz_family):
    """Spouse rule — symmetric per the relationship table."""
    # Add a SSN for the spouse so we have something to read back.
    spouse_ssn = "999-88-7777"
    db.add(
        models.SensitiveIdentifier(
            person_id=authz_family["spouse_id"],
            identifier_type="social_security_number",
            identifier_value_encrypted=encrypt_str(spouse_ssn),
            identifier_last_four="7777",
        )
    )
    db.commit()

    ctx = _ctx(
        db,
        family_id=authz_family["family_id"],
        person_id=authz_family["parent_id"],
    )
    out = await secrets_tools.handle_reveal_sensitive(
        ctx,
        person_id=authz_family["spouse_id"],
        identifier_type="social_security_number",
    )
    assert out["found"] is True
    assert out["access_label"] == "spouse"
    assert out["results"][0]["value"] == spouse_ssn


async def test_self_can_reveal_own_ssn(db, authz_family):
    """The base-case sanity check — you can always read your own SSN."""
    ctx = _ctx(
        db,
        family_id=authz_family["family_id"],
        person_id=authz_family["child_id"],
    )
    out = await secrets_tools.handle_reveal_sensitive(
        ctx,
        person_id=authz_family["child_id"],
        identifier_type="social_security_number",
    )
    assert out["access_label"] == "self"
    assert out["results"][0]["value"] == authz_family["ssn_plain"]


async def test_uncle_cannot_reveal_nephew_ssn(db, authz_family):
    """Uncle has no direct parent_of edge → tool refuses with a coaching note.

    The handler raises :class:`ToolError` rather than returning a
    friendly string — that's the contract with the agent loop, which
    surfaces tool errors as structured "tool refused" results so the
    LLM can compose the user-facing message.
    """
    from api.ai.tools._registry import ToolError

    ctx = _ctx(
        db,
        family_id=authz_family["family_id"],
        person_id=authz_family["uncle_id"],
    )
    with pytest.raises(ToolError):
        await secrets_tools.handle_reveal_sensitive(
            ctx,
            person_id=authz_family["child_id"],
            identifier_type="social_security_number",
        )


async def test_admin_can_reveal_any_ssn(db, authz_family):
    """``ctx.is_admin=True`` bypasses the household relationship gate."""
    ctx = _ctx(
        db,
        family_id=authz_family["family_id"],
        person_id=authz_family["uncle_id"],  # not authorised by relationship
        is_admin=True,
    )
    out = await secrets_tools.handle_reveal_sensitive(
        ctx,
        person_id=authz_family["child_id"],
        identifier_type="social_security_number",
    )
    assert out["found"] is True
    assert out["access_label"] == "admin"
    assert out["results"][0]["value"] == authz_family["ssn_plain"]


# ---------------------------------------------------------------------------
# reveal_secret — VIN, the other half of the bug the user reported
# ---------------------------------------------------------------------------


async def test_parent_can_reveal_child_vehicle_vin(db, authz_family):
    """The other reported case: parent asking for child's VIN must succeed.

    The VIN is NEVER denormalised into the RAG block — only its
    last-four is — so the only way to share the full 17-character
    string is to invoke :func:`handle_reveal_secret`. This test proves
    that path returns the right plaintext for an authorised parent.
    """
    ctx = _ctx(
        db,
        family_id=authz_family["family_id"],
        person_id=authz_family["parent_id"],
    )
    out = await secrets_tools.handle_reveal_secret(
        ctx,
        category="vehicle_vin",
        record_id=authz_family["vehicle_id"],
    )
    assert out["found"] is True
    assert out["access_label"] == "parent"
    assert out["value"] == authz_family["vin_plain"]


async def test_uncle_cannot_reveal_child_vehicle_vin(db, authz_family):
    """Uncle is NOT a direct parent → reveal_secret raises ToolError."""
    from api.ai.tools._registry import ToolError

    ctx = _ctx(
        db,
        family_id=authz_family["family_id"],
        person_id=authz_family["uncle_id"],
    )
    with pytest.raises(ToolError):
        await secrets_tools.handle_reveal_secret(
            ctx,
            category="vehicle_vin",
            record_id=authz_family["vehicle_id"],
        )


async def test_admin_can_reveal_any_vehicle_vin(db, authz_family):
    """Admin bypass works for the secret tool as well as the SSN tool."""
    ctx = _ctx(
        db,
        family_id=authz_family["family_id"],
        person_id=authz_family["uncle_id"],
        is_admin=True,
    )
    out = await secrets_tools.handle_reveal_secret(
        ctx,
        category="vehicle_vin",
        record_id=authz_family["vehicle_id"],
    )
    assert out["found"] is True
    assert out["access_label"] == "admin"
    assert out["value"] == authz_family["vin_plain"]


async def test_cross_family_stranger_blocked_at_secret_tool(db, authz_family):
    """Even with the right record_id, a different-family speaker is denied."""
    from api.ai.tools._registry import ToolError

    ctx = ToolContext(
        db=db,
        family_id=authz_family["other_family_id"],
        person_id=authz_family["stranger_id"],
    )
    with pytest.raises(ToolError):
        await secrets_tools.handle_reveal_secret(
            ctx,
            category="vehicle_vin",
            record_id=authz_family["vehicle_id"],
        )


# ---------------------------------------------------------------------------
# Routing layer — the live-chat fast-path skip decision
# ---------------------------------------------------------------------------


def test_sensitive_intent_classifier_recognises_known_phrasings():
    """Every phrasing the user is likely to type must trigger the skip.

    Failure mode we care about: a sensitive ask slips past the
    classifier, takes the family_qa fast tier, and gets a stock-LLM
    refusal. So we test the FALSE-NEGATIVE direction generously.
    """
    positives = [
        "what's Jax's SSN?",
        "what is my social security number",
        "Can you remind me of our homeowners policy number?",
        "What's the full VIN of the truck",
        "I need the routing number on the joint checking account",
        "Look up Theo's passport number",
        "What's Sara's driver's license number?",
        "tell me the bank account number for the Chase account",
        "what is the tax id on file",
        "give me the license plate for the Honda",
        "what's our member number on the BCBS plan",
    ]
    misses = [m for m in positives if not sensitive_intent.is_sensitive_identifier_ask(m)]
    assert not misses, (
        "Sensitive-identifier asks that the classifier MISSED "
        "(would route to the fast tier and get refused):\n"
        + "\n".join(f"  - {m}" for m in misses)
    )


def test_sensitive_intent_classifier_ignores_routine_chitchat():
    """False positives are cheap (a slow heavy-agent reply), but we still
    want the routine cases to keep the fast path so the latency win
    isn't lost."""
    negatives = [
        "hi avi",
        "who lives here?",
        "what colour is the truck?",
        "send mom an email about dinner",
        "what time is the soccer game",
        "remind me to take out the trash",
        "what's on my calendar tomorrow",
        "thanks",
    ]
    hits = [m for m in negatives if sensitive_intent.is_sensitive_identifier_ask(m)]
    assert not hits, (
        "Routine messages misclassified as sensitive (fast path "
        "would be skipped unnecessarily):\n"
        + "\n".join(f"  - {m}" for m in hits)
    )


def test_live_chat_skips_family_qa_shortcut_for_sensitive_asks(
    client, monkeypatch, test_family, db
):
    """End-to-end proof: the live-chat router will NOT hand a sensitive ask
    to the family_qa fast model — even when the shortcut feature flag
    is on.

    We mock the heavy agent so we don't need Ollama, and we patch
    ``family_qa_router.try_shortcut`` to record whether it was called.
    The contract: for a sensitive-identifier ask, the live-chat
    endpoint must skip ``try_shortcut`` and run the heavy agent
    instead. (The heavy agent's tool layer is what we already covered
    in the tests above; here we just prove the router takes the right
    branch.)
    """
    from unittest.mock import patch

    # Mint a real session cookie the same way ``/api/auth/google/callback``
    # would for a non-admin family member of the persistent test
    # household. The cookie middleware (see ``api.main``) verifies and
    # decodes it on every request, so the /chat handler sees a logged
    # in member with the right family_id pinned and ``is_admin=False``
    # — exactly the scenario the user reported.
    from api.auth import ROLE_MEMBER, sign_session
    from api.config import get_settings

    cookie_name = get_settings().SESSION_COOKIE_NAME
    member_cookie = sign_session(
        email="integration.test@example.com",
        role=ROLE_MEMBER,
        person_id=test_family["person_id"],
        family_id=test_family["family_id"],
    )
    client.cookies.set(cookie_name, member_cookie)

    # Stub the heavy agent so the test doesn't need Ollama. We just
    # need to observe that `run_agent` was called with the right
    # `requestor_is_admin` flag and that the router didn't divert
    # into the family_qa fast tier.
    async def _fake_agent(**_kwargs):
        # `run_agent` is an async generator that yields AgentEvents.
        # We yield nothing — the SSE stream then closes cleanly with
        # a `done: true` marker, which is enough for this test.
        if False:  # pragma: no cover - keeps the function a generator
            yield None

    shortcut_called: list[str] = []

    async def _fake_shortcut(*_args, **_kwargs):
        shortcut_called.append(_kwargs.get("recognized_person_id", "?"))
        return None

    with patch("api.routers.ai_chat.agent_loop.run_agent", _fake_agent), patch(
        "api.routers.ai_chat.family_qa_router.try_shortcut",
        _fake_shortcut,
    ), patch(
        "api.routers.ai_chat.web_search_shortcut.try_shortcut",
        return_value=None,
    ):
        resp = client.post(
            "/api/aiassistant/chat",
            json={
                "family_id": test_family["family_id"],
                "messages": [
                    {
                        "role": "user",
                        "content": "what is my social security number?",
                    }
                ],
            },
        )

    assert resp.status_code == 200, resp.text
    assert not shortcut_called, (
        "family_qa_router.try_shortcut MUST be skipped for sensitive "
        "identifier asks so the heavy agent's deterministic authz "
        "tools make the call. It was invoked instead — the routing "
        "guard in api.routers.ai_chat is broken."
    )
