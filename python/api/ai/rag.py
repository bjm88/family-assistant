"""Pull a "detailed profile" block for a person from Postgres.

Used by the AI assistant to build context-rich prompts for the LLM. We
deliberately keep the format as short, human-readable bullet lines — the
local Gemma models handle that format well and it keeps token counts
low on the CPU/GPU path.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models
from . import authz


def _age_years(dob: Optional[date]) -> Optional[int]:
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - (
        (today.month, today.day) < (dob.month, dob.day)
    )


def _person_display_name(p: models.Person) -> str:
    return p.preferred_name or p.first_name or f"Person {p.person_id}"


def build_person_context(
    db: Session,
    person: models.Person,
    *,
    requestor_person_id: Optional[int] = None,
) -> str:
    """Render a person's RAG context as plain text bullets.

    When ``requestor_person_id`` is supplied and the requestor lacks
    relationship-based access to ``person`` (see :mod:`ai.authz`),
    sensitive details (free-text notes, medical conditions /
    medications / physicians, identity-document counts) are redacted
    from the rendered block. Public details (name, age, role,
    interests, goals, relationships) are kept so the LLM can still
    reference the person naturally in conversation.

    When ``requestor_person_id`` is ``None`` we behave as before — the
    callers that don't care about redaction (greet, followup, where the
    speaker IS the subject) keep their existing behaviour unchanged.
    """
    if requestor_person_id is None:
        access_allowed = True
    else:
        access_allowed = authz.can_access_sensitive(
            db,
            requestor_person_id=requestor_person_id,
            subject_person_id=person.person_id,
            family_id=person.family_id,
        ).allowed

    lines: List[str] = []
    name = _person_display_name(person)
    full = " ".join(
        x for x in [person.first_name, person.middle_name, person.last_name] if x
    ).strip()
    lines.append(f"Name: {name}" + (f" (full: {full})" if full and full != name else ""))
    if person.primary_family_relationship:
        lines.append(
            f"Role in family: {person.primary_family_relationship.replace('_', ' ')}"
        )
    if person.gender:
        lines.append(f"Gender: {person.gender}")
    age = _age_years(person.date_of_birth)
    if age is not None:
        lines.append(f"Age: {age}")

    # Email handles double as Google Calendar ids — surface them so
    # the LLM can correctly pick a calendar to query and so it can
    # email the right address. Email addresses themselves are not
    # treated as sensitive (they're already on lots of business
    # cards); access to the calendar BEHIND the address is gated
    # separately by ai.authz.can_see_calendar_details.
    email_bits: List[str] = []
    if person.email_address:
        email_bits.append(f"personal {person.email_address}")
    if person.work_email:
        email_bits.append(f"work {person.work_email}")
    if email_bits:
        lines.append("Email: " + ", ".join(email_bits))

    # Goals (ordered by priority).
    priority_rank = {"urgent": 0, "semi_urgent": 1, "normal": 2, "low": 3}
    goals = sorted(
        person.goals or [],
        key=lambda g: (priority_rank.get(g.priority, 9), g.goal_name or ""),
    )
    if goals:
        lines.append("Current goals:")
        for g in goals[:6]:
            extra = []
            if g.start_date:
                extra.append(f"starts {g.start_date.isoformat()}")
            extra.append(f"priority {g.priority.replace('_', '-')}")
            line = f"  - {g.goal_name}"
            if g.description:
                line += f" — {g.description}"
            line += f" ({', '.join(extra)})"
            lines.append(line)

    # Interests / hobbies — free-form line the user maintains in the
    # admin console specifically so Avi has something concrete to riff
    # on in conversation. Without this the followup-question generator
    # falls back to a generic "how was your day" because goals alone
    # often aren't enough material.
    if person.interests_and_activities:
        interests = person.interests_and_activities.strip().replace("\n", " ")
        if len(interests) > 320:
            interests = interests[:317] + "…"
        lines.append(f"Interests / hobbies: {interests}")

    # Notes (truncated). Personal notes are gated by relationship —
    # children and others should not see private notes a parent stored
    # about themselves or about a sibling.
    if person.notes and access_allowed:
        note = person.notes.strip().replace("\n", " ")
        if len(note) > 240:
            note = note[:237] + "…"
        lines.append(f"Notes: {note}")
    elif person.notes and not access_allowed:
        lines.append(f"Notes: {authz.REDACTED_PLACEHOLDER}")

    # Relationships — sibling / parent / spouse count.
    rels = db.execute(
        select(models.PersonRelationship).where(
            (models.PersonRelationship.from_person_id == person.person_id)
            | (models.PersonRelationship.to_person_id == person.person_id)
        )
    ).scalars().all()
    # Count parents / children / spouses using relationship direction.
    parent_ids: set[int] = set()
    child_ids: set[int] = set()
    spouse_ids: set[int] = set()
    for r in rels:
        if r.relationship_type == "parent_of":
            if r.from_person_id == person.person_id:
                child_ids.add(r.to_person_id)
            else:
                parent_ids.add(r.from_person_id)
        elif r.relationship_type == "spouse_of":
            other = (
                r.to_person_id
                if r.from_person_id == person.person_id
                else r.from_person_id
            )
            spouse_ids.add(other)
    # Siblings = people who share at least one parent with me.
    sibling_ids: set[int] = set()
    if parent_ids:
        sib_rows = db.execute(
            select(models.PersonRelationship).where(
                (models.PersonRelationship.relationship_type == "parent_of")
                & (models.PersonRelationship.from_person_id.in_(parent_ids))
            )
        ).scalars().all()
        for sr in sib_rows:
            if sr.to_person_id != person.person_id:
                sibling_ids.add(sr.to_person_id)

    def names_for(ids: set[int]) -> List[str]:
        if not ids:
            return []
        people = db.execute(
            select(models.Person).where(models.Person.person_id.in_(ids))
        ).scalars().all()
        return [_person_display_name(p) for p in people]

    parents = names_for(parent_ids)
    spouses = names_for(spouse_ids)
    children = names_for(child_ids)
    siblings = names_for(sibling_ids)
    if parents:
        lines.append(f"Parents: {', '.join(parents)}")
    if spouses:
        lines.append(f"Spouse: {', '.join(spouses)}")
    if children:
        lines.append(f"Children: {', '.join(children)}")
    if siblings:
        lines.append(f"Siblings: {', '.join(siblings)}")

    return "\n".join(lines)


def build_family_overview(
    db: Session,
    family: models.Family,
    *,
    requestor_person_id: Optional[int] = None,
) -> str:
    """High-level RAG block describing the whole household.

    The output is intentionally dense, sectioned, and bulletised so a
    small local model (Gemma 4) can scan it for a specific entity in
    the conversation. Sensitive identifiers (full account numbers,
    VINs, plate numbers, SSNs) are *never* surfaced — only their
    last-four helpers, which is what humans actually quote anyway.

    When ``requestor_person_id`` is supplied, per-person sensitive
    blocks (medical conditions, medications, physicians, identity-doc
    counts, financial-account last-fours) are also redacted to match
    the speaker's relationship-based access window. See :mod:`ai.authz`.
    Without a requestor we fall back to the original "trusted reader"
    behaviour for backwards compatibility.
    """
    # Pre-compute who the speaker is allowed to see in detail. When
    # requestor is None this stays empty and the access lookup short-
    # circuits below.
    accessible_subject_ids: set[int] = set()
    if requestor_person_id is not None and family.people:
        for p in family.people:
            decision = authz.can_access_sensitive(
                db,
                requestor_person_id=requestor_person_id,
                subject_person_id=p.person_id,
                family_id=family.family_id,
            )
            if decision.allowed:
                accessible_subject_ids.add(p.person_id)

    def _can_see(subject_person_id: Optional[int]) -> bool:
        if requestor_person_id is None:
            return True
        if subject_person_id is None:
            return False
        return subject_person_id in accessible_subject_ids

    # Pre-load the per-person collections we render below in a single
    # round trip per relationship, so the overview stays N+1-free even
    # for big households. Without this we'd issue one ``SELECT FROM
    # goals`` and one ``SELECT FROM identity_documents`` per Person
    # instance — fine on six rows, but unnecessary every chat turn.
    if family.people:
        person_ids = [p.person_id for p in family.people]
        db.execute(
            select(models.Person)
            .where(models.Person.person_id.in_(person_ids))
            .options(
                selectinload(models.Person.goals),
                selectinload(models.Person.identity_documents),
                selectinload(models.Person.medical_conditions),
                selectinload(models.Person.medications),
                selectinload(models.Person.physicians),
            )
        ).all()

    lines: List[str] = []
    lines.append(f"Family: {family.family_name} (family_id={family.family_id})")
    if family.assistant:
        a = family.assistant
        lines.append(
            f"Assistant: {a.assistant_name}"
            + (f" ({a.gender})" if a.gender else "")
        )
    if family.head_of_household_notes:
        lines.append("Household notes: " + _short(family.head_of_household_notes))

    # ---- People ------------------------------------------------------
    people = family.people or []
    if people:
        lines.append("")
        lines.append(f"## People ({len(people)})")
        for p in people:
            bits = [f"{_person_display_name(p)}"]
            if p.primary_family_relationship:
                bits.append(p.primary_family_relationship.replace("_", " "))
            age = _age_years(p.date_of_birth)
            if age is not None:
                bits.append(f"age {age}")
            elif p.date_of_birth:
                bits.append(f"DOB {p.date_of_birth.isoformat()}")
            if p.gender:
                bits.append(p.gender)
            line = "- " + " · ".join(bits) + f" [person_id={p.person_id}]"
            lines.append(line)
            if p.interests_and_activities:
                lines.append(
                    "    interests: " + _short(p.interests_and_activities, 200)
                )
            goals = p.goals or []
            if goals:
                priority_rank = {"urgent": 0, "semi_urgent": 1, "normal": 2, "low": 3}
                top = sorted(
                    goals,
                    key=lambda g: (
                        priority_rank.get(g.priority, 9),
                        g.goal_name or "",
                    ),
                )[:3]
                lines.append(
                    "    goals: "
                    + "; ".join(
                        f"{g.goal_name} ({g.priority.replace('_', '-')})"
                        for g in top
                    )
                )
            # Active medical conditions / meds inline so the LLM sees
            # them right next to the person without having to crawl a
            # second section. Closed conditions (with end_date) are
            # left out of the per-person summary; they're available
            # via SQL for follow-up questions about history.
            #
            # ALL of these blocks are relationship-gated: only the
            # speaker themselves, their spouse, and their direct
            # parents may see another person's medical / physician
            # data. Anyone else gets a single redacted line so the
            # LLM still knows the data exists but cannot quote it.
            person_visible = _can_see(p.person_id)
            active_conditions = [
                c for c in (p.medical_conditions or []) if c.end_date is None
            ]
            active_meds = [
                m for m in (p.medications or []) if m.end_date is None
            ]
            has_any_medical = bool(
                active_conditions or active_meds or p.physicians
            )
            if has_any_medical and not person_visible:
                lines.append(
                    f"    medical: {authz.REDACTED_PLACEHOLDER}"
                )
            else:
                if active_conditions:
                    lines.append(
                        "    conditions: "
                        + "; ".join(
                            c.condition_name
                            + (f" ({c.icd10_code})" if c.icd10_code else "")
                            for c in active_conditions[:6]
                        )
                    )
                if active_meds:
                    lines.append(
                        "    medications: "
                        + "; ".join(
                            (m.brand_name or m.generic_name or m.ndc_number or "?")
                            + (f" {m.dosage}" if m.dosage else "")
                            for m in active_meds[:6]
                        )
                    )
                if p.physicians:
                    lines.append(
                        "    physicians: "
                        + "; ".join(
                            doc.physician_name
                            + (f" ({doc.specialty})" if doc.specialty else "")
                            for doc in p.physicians[:5]
                        )
                    )

    # ---- Pets --------------------------------------------------------
    pets = family.pets or []
    if pets:
        lines.append("")
        lines.append(f"## Pets ({len(pets)})")
        for pet in pets:
            bits = [f"{pet.pet_name}", pet.animal_type.replace("_", " ")]
            if pet.breed:
                bits.append(pet.breed)
            if pet.color:
                bits.append(pet.color)
            age = _age_years(pet.date_of_birth)
            if age is not None:
                bits.append(f"age {age}")
            lines.append("- " + " · ".join(bits) + f" [pet_id={pet.pet_id}]")

    # ---- Residences --------------------------------------------------
    residences = family.residences or []
    if residences:
        lines.append("")
        lines.append(f"## Residences ({len(residences)})")
        for r in residences:
            tag = " (PRIMARY)" if r.is_primary_residence else ""
            location = ", ".join(
                x for x in [r.street_line_1, r.city, r.state_or_region] if x
            )
            lines.append(
                f"- {r.label}{tag}: {location} [residence_id={r.residence_id}]"
            )

    # ---- Vehicles ----------------------------------------------------
    vehicles = family.vehicles or []
    if vehicles:
        lines.append("")
        lines.append(f"## Vehicles ({len(vehicles)})")
        # Build a quick driver lookup so we can show names instead of ids.
        people_by_id = {p.person_id: _person_display_name(p) for p in people}
        residence_label_by_id = {r.residence_id: r.label for r in residences}
        for v in vehicles:
            head_bits: List[str] = [v.vehicle_type or "vehicle"]
            if v.year:
                head_bits.append(str(v.year))
            head_bits.append(v.make)
            head_bits.append(v.model)
            if v.trim:
                head_bits.append(v.trim)
            line = "- " + " ".join(head_bits)
            if v.nickname:
                line += f' — "{v.nickname}"'
            line += f" [vehicle_id={v.vehicle_id}]"
            lines.append(line)
            extras: List[str] = []
            if v.color:
                extras.append(f"color {v.color}")
            if v.fuel_type:
                extras.append(f"fuel {v.fuel_type}")
            if v.license_plate_number_last_four:
                extras.append(
                    f"plate ****{v.license_plate_number_last_four}"
                    + (
                        f" ({v.license_plate_state_or_region})"
                        if v.license_plate_state_or_region
                        else ""
                    )
                )
            if v.vehicle_identification_number_last_four:
                extras.append(
                    f"VIN ****{v.vehicle_identification_number_last_four}"
                )
            if v.registration_expiration_date:
                extras.append(
                    f"registration expires {v.registration_expiration_date.isoformat()}"
                )
            if v.primary_driver_person_id in people_by_id:
                extras.append(
                    f"primary driver {people_by_id[v.primary_driver_person_id]}"
                )
            if v.residence_id in residence_label_by_id:
                extras.append(f"parked at {residence_label_by_id[v.residence_id]}")
            if extras:
                lines.append("    " + " · ".join(extras))

    # ---- Insurance ---------------------------------------------------
    policies = family.insurance_policies or []
    if policies:
        lines.append("")
        lines.append(f"## Insurance policies ({len(policies)})")
        for pol in policies:
            head = (
                f"- {pol.policy_type} · {pol.carrier_name}"
                + (f" — {pol.plan_name}" if pol.plan_name else "")
                + f" [insurance_policy_id={pol.insurance_policy_id}]"
            )
            lines.append(head)
            extras: List[str] = []
            if pol.policy_number_last_four:
                extras.append(f"policy ****{pol.policy_number_last_four}")
            if pol.premium_amount_usd:
                freq = (
                    f"/{pol.premium_billing_frequency}"
                    if pol.premium_billing_frequency
                    else ""
                )
                extras.append(f"premium ${pol.premium_amount_usd:.2f}{freq}")
            if pol.deductible_amount_usd:
                extras.append(f"deductible ${pol.deductible_amount_usd:.2f}")
            if pol.expiration_date:
                extras.append(f"expires {pol.expiration_date.isoformat()}")
            if pol.agent_name:
                extras.append(f"agent {pol.agent_name}")
            if extras:
                lines.append("    " + " · ".join(extras))

    # ---- Financial accounts -----------------------------------------
    # Each account is gated by its primary holder. If the speaker
    # cannot access the holder, we still mention "an account exists"
    # (so the model knows not to claim there are none) but drop the
    # institution / nickname / last-four.
    accounts = family.financial_accounts or []
    if accounts:
        lines.append("")
        lines.append(f"## Financial accounts ({len(accounts)})")
        for fa in accounts:
            holder_id = getattr(fa, "primary_holder_person_id", None)
            holder_visible = _can_see(holder_id)
            if not holder_visible:
                lines.append(
                    f"- {authz.REDACTED_PLACEHOLDER} "
                    f"[financial_account_id={fa.financial_account_id}]"
                )
                continue
            bits = [
                f"{fa.account_type}",
                fa.institution_name,
            ]
            if fa.account_nickname:
                bits.append(f'"{fa.account_nickname}"')
            if fa.account_number_last_four:
                bits.append(f"****{fa.account_number_last_four}")
            lines.append(
                "- " + " · ".join(bits)
                + f" [financial_account_id={fa.financial_account_id}]"
            )

    # ---- Identity documents (counts only — content is sensitive) -----
    # Even the *count* is gated: knowing "Sarah has 3 ID documents on
    # file" gives a stranger a reconnaissance signal, so we suppress
    # the line entirely for unauthorized speakers.
    id_doc_count_by_person: dict[int, int] = {}
    for p in people:
        n = len(p.identity_documents or [])
        if n and _can_see(p.person_id):
            id_doc_count_by_person[p.person_id] = n
    if id_doc_count_by_person:
        lines.append("")
        lines.append("## Identity documents on file")
        names = {p.person_id: _person_display_name(p) for p in people}
        for pid, n in id_doc_count_by_person.items():
            lines.append(f"- {names.get(pid, f'Person {pid}')}: {n} document(s)")

    return "\n".join(lines).strip()


def _short(s: str, limit: int = 240) -> str:
    """Collapse and truncate a free-text blob for prompt embedding."""
    flat = " ".join(s.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def pick_goal_for_question(person: models.Person) -> Optional[models.Goal]:
    """Choose the most salient goal for an opening question."""
    goals = person.goals or []
    if not goals:
        return None
    priority_rank = {"urgent": 0, "semi_urgent": 1, "normal": 2, "low": 3}
    goals_sorted = sorted(
        goals,
        key=lambda g: (priority_rank.get(g.priority, 9), g.goal_name or ""),
    )
    return goals_sorted[0]
