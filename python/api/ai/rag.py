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
from sqlalchemy.orm import Session

from .. import models


def _age_years(dob: Optional[date]) -> Optional[int]:
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - (
        (today.month, today.day) < (dob.month, dob.day)
    )


def _person_display_name(p: models.Person) -> str:
    return p.preferred_name or p.first_name or f"Person {p.person_id}"


def build_person_context(db: Session, person: models.Person) -> str:
    """Render a person's RAG context as plain text bullets."""
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

    # Notes (truncated).
    if person.notes:
        note = person.notes.strip().replace("\n", " ")
        if len(note) > 240:
            note = note[:237] + "…"
        lines.append(f"Notes: {note}")

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


def build_family_overview(db: Session, family: models.Family) -> str:
    """High-level RAG block describing the whole household."""
    lines: List[str] = [f"Family: {family.family_name}"]
    people = family.people or []
    if people:
        roster = []
        for p in people:
            bit = _person_display_name(p)
            if p.primary_family_relationship:
                bit += f" ({p.primary_family_relationship.replace('_', ' ')})"
            roster.append(bit)
        lines.append("Members: " + ", ".join(roster))
    pets = family.pets or []
    if pets:
        lines.append(
            "Pets: "
            + ", ".join(
                f"{p.pet_name} ({p.animal_type.replace('_', ' ')})" for p in pets
            )
        )
    residences = family.residences or []
    primary = next((r for r in residences if r.is_primary_residence), None)
    if primary:
        lines.append(
            f"Home: {primary.label} at {primary.street_line_1}, "
            f"{primary.city}"
            + (f", {primary.state_or_region}" if primary.state_or_region else "")
        )
    return "\n".join(lines)


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
