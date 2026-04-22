import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Users2, X } from "lucide-react";
import { api } from "@/lib/api";
import type { Person, PersonRelationship, RelationshipType } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { ProfileAvatar } from "@/components/ProfileAvatar";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";

/**
 * The family tree is stored as atomic edges in `person_relationships`:
 *   - `parent_of`   (directional, from=parent, to=child)
 *   - `spouse_of`   (symmetric, stored as two rows)
 *
 * This page picks one person as the "focus" and derives everything else:
 *   parents, children, spouses (explicit edges)
 *   siblings (any other person who shares at least one parent with focus)
 */

type TargetGroup = "parent" | "child" | "spouse";

interface AddForm {
  target: TargetGroup;
  other_person_id: number;
}

export default function RelationshipsPage() {
  const { familyId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const qc = useQueryClient();
  const toast = useToast();

  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });
  const { data: edges } = useQuery<PersonRelationship[]>({
    queryKey: ["person-relationships", familyId],
    queryFn: () =>
      api.get<PersonRelationship[]>(
        `/api/person-relationships?family_id=${familyId}`
      ),
  });

  // Optional ``?focus=<id>`` URL hint — set when arriving from a
  // family-tree node click. We make it the source of truth so
  // (a) deep links work, (b) browser back/forward restores the
  // previously-focused person, and (c) refreshing the page keeps
  // the same focus instead of snapping back to "self".
  const focusFromUrl = (() => {
    const raw = searchParams.get("focus");
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  })();

  const [focusId, setFocusIdState] = useState<number | null>(focusFromUrl);

  const setFocusId = (id: number | null) => {
    setFocusIdState(id);
    // Mirror to URL so subsequent navigations and refreshes preserve
    // the choice. ``replace: true`` keeps the back button useful
    // (otherwise every node-click click would push a new history entry).
    const next = new URLSearchParams(searchParams);
    if (id == null) next.delete("focus");
    else next.set("focus", String(id));
    setSearchParams(next, { replace: true });
  };

  useEffect(() => {
    if (!people || people.length === 0) return;
    // If the URL specifies a focus that exists in this family, honour it.
    if (focusFromUrl != null && people.some((p) => p.person_id === focusFromUrl)) {
      if (focusId !== focusFromUrl) setFocusIdState(focusFromUrl);
      return;
    }
    // Otherwise fall back to "self" (or first person) the first time.
    if (focusId === null) {
      const self = people.find(
        (p) => p.primary_family_relationship === "self"
      );
      setFocusIdState((self ?? people[0]).person_id);
    }
  }, [people, focusFromUrl, focusId]);

  const byId = useMemo(() => {
    const m = new Map<number, Person>();
    (people ?? []).forEach((p) => m.set(p.person_id, p));
    return m;
  }, [people]);

  const focus = focusId != null ? byId.get(focusId) : undefined;

  const derived = useMemo(() => {
    if (!focus || !edges) {
      return { parents: [], children: [], spouses: [], siblings: [] };
    }
    const parentEdges = edges.filter(
      (e) => e.relationship_type === "parent_of" && e.to_person_id === focus.person_id
    );
    const childEdges = edges.filter(
      (e) =>
        e.relationship_type === "parent_of" && e.from_person_id === focus.person_id
    );
    const spouseEdges = edges.filter(
      (e) =>
        e.relationship_type === "spouse_of" &&
        e.from_person_id === focus.person_id
    );

    const parentIds = new Set(parentEdges.map((e) => e.from_person_id));
    // Siblings: anyone (not focus) who is a child of any parent of focus.
    const siblingIds = new Set<number>();
    edges.forEach((e) => {
      if (
        e.relationship_type === "parent_of" &&
        parentIds.has(e.from_person_id) &&
        e.to_person_id !== focus.person_id
      ) {
        siblingIds.add(e.to_person_id);
      }
    });

    const lookup = (id: number) => byId.get(id);

    return {
      parents: parentEdges
        .map((e) => ({ edge: e, person: lookup(e.from_person_id) }))
        .filter((x): x is { edge: PersonRelationship; person: Person } => !!x.person),
      children: childEdges
        .map((e) => ({ edge: e, person: lookup(e.to_person_id) }))
        .filter((x): x is { edge: PersonRelationship; person: Person } => !!x.person),
      spouses: spouseEdges
        .map((e) => ({ edge: e, person: lookup(e.to_person_id) }))
        .filter((x): x is { edge: PersonRelationship; person: Person } => !!x.person),
      siblings: Array.from(siblingIds)
        .map((id) => lookup(id))
        .filter((p): p is Person => !!p),
    };
  }, [focus, edges, byId]);

  const [addGroup, setAddGroup] = useState<TargetGroup | null>(null);

  const addEdge = useMutation({
    mutationFn: (v: AddForm) => {
      if (!focus) throw new Error("No focus person selected.");
      let type: RelationshipType;
      let from_person_id: number;
      let to_person_id: number;
      if (v.target === "parent") {
        type = "parent_of";
        from_person_id = v.other_person_id;
        to_person_id = focus.person_id;
      } else if (v.target === "child") {
        type = "parent_of";
        from_person_id = focus.person_id;
        to_person_id = v.other_person_id;
      } else {
        type = "spouse_of";
        from_person_id = focus.person_id;
        to_person_id = v.other_person_id;
      }
      return api.post<PersonRelationship[]>("/api/person-relationships", {
        from_person_id,
        to_person_id,
        relationship_type: type,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["person-relationships", familyId] });
      setAddGroup(null);
      toast.success("Relationship added.");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const removeEdge = useMutation({
    mutationFn: (id: number) => api.del(`/api/person-relationships/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["person-relationships", familyId] });
      toast.success("Relationship removed.");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <div>
      <PageHeader
        title="Family tree"
        description="Pick a focal person to see their parents, spouse, siblings, and children. Click a name to re-center the tree, or click the face to open that person's full profile."
      />

      <div className="card mb-6">
        <div className="card-body flex flex-wrap items-center gap-3">
          <div className="text-sm text-muted-foreground">Focus on:</div>
          <select
            className="input max-w-xs"
            value={focusId ?? ""}
            onChange={(e) => setFocusId(Number(e.target.value))}
          >
            {(people ?? []).map((p) => (
              <option key={p.person_id} value={p.person_id}>
                {p.preferred_name || p.first_name} {p.last_name}
              </option>
            ))}
          </select>
          <div className="ml-auto text-xs text-muted-foreground">
            {(edges ?? []).length} edges · siblings are derived from shared parents
          </div>
        </div>
      </div>

      {!people || people.length === 0 ? (
        <div className="card">
          <div className="card-body text-sm text-muted-foreground">
            Add some people on the People page first, then come back to wire up the
            family tree.
          </div>
        </div>
      ) : focus ? (
        <div className="space-y-6">
          <TreeRow
            label="Parents"
            hint="Who brought the focus person into the family?"
            people={derived.parents.map((x) => x.person)}
            edges={derived.parents}
            familyId={familyId}
            onAdd={() => setAddGroup("parent")}
            onSelect={(id) => setFocusId(id)}
            onRemove={(edgeId) => removeEdge.mutate(edgeId)}
          />

          <div className="card border-primary/20">
            <div className="card-body flex items-center gap-4">
              <Link
                to={`/admin/families/${familyId}/people/${focus.person_id}`}
                title="Open profile"
                className="rounded-full ring-2 ring-transparent hover:ring-primary/40 transition"
              >
                <ProfileAvatar person={focus} size={64} />
              </Link>
              <div className="flex-1">
                <div className="text-xs uppercase tracking-wide text-muted-foreground">
                  Focus
                </div>
                <div className="font-semibold text-lg">
                  {focus.preferred_name || focus.first_name} {focus.last_name}
                </div>
                <div className="text-sm text-muted-foreground">
                  {focus.primary_family_relationship ?? "family member"}
                </div>
              </div>
              <div className="hidden md:flex items-center gap-6 text-xs text-muted-foreground">
                <Stat label="parents" value={derived.parents.length} />
                <Stat label="spouse" value={derived.spouses.length} />
                <Stat label="siblings" value={derived.siblings.length} />
                <Stat label="children" value={derived.children.length} />
              </div>
            </div>
          </div>

          <TreeRow
            label="Spouse"
            hint="Marriages / long-term partners. Stored symmetrically in both directions."
            people={derived.spouses.map((x) => x.person)}
            edges={derived.spouses}
            familyId={familyId}
            onAdd={() => setAddGroup("spouse")}
            onSelect={(id) => setFocusId(id)}
            onRemove={(edgeId) => removeEdge.mutate(edgeId)}
          />
          <TreeRow
            label="Siblings"
            hint="Computed from shared parents — there's nothing to add here directly."
            people={derived.siblings}
            edges={null}
            familyId={familyId}
            onSelect={(id) => setFocusId(id)}
          />
          <TreeRow
            label="Children"
            hint="Biological, adopted, step — anyone the focus person is a parent to."
            people={derived.children.map((x) => x.person)}
            edges={derived.children}
            familyId={familyId}
            onAdd={() => setAddGroup("child")}
            onSelect={(id) => setFocusId(id)}
            onRemove={(edgeId) => removeEdge.mutate(edgeId)}
          />
        </div>
      ) : null}

      <AddEdgeModal
        focus={focus ?? null}
        group={addGroup}
        people={people ?? []}
        edges={edges ?? []}
        onClose={() => setAddGroup(null)}
        onSubmit={(v) => addEdge.mutate(v)}
        submitting={addEdge.isPending}
      />
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="text-center">
      <div className="text-lg font-semibold text-foreground">{value}</div>
      <div>{label}</div>
    </div>
  );
}

function TreeRow({
  label,
  hint,
  people,
  edges,
  familyId,
  onAdd,
  onSelect,
  onRemove,
}: {
  label: string;
  hint?: string;
  people: Person[];
  edges: { edge: PersonRelationship; person: Person }[] | null;
  familyId: string | undefined;
  onAdd?: () => void;
  onSelect: (personId: number) => void;
  onRemove?: (edgeId: number) => void;
}) {
  const edgeByPerson = new Map<number, PersonRelationship>();
  (edges ?? []).forEach((e) => edgeByPerson.set(e.person.person_id, e.edge));

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">{label}</div>
          {hint && (
            <div className="text-xs text-muted-foreground mt-0.5">{hint}</div>
          )}
        </div>
        {onAdd && (
          <button className="btn-secondary" onClick={onAdd}>
            <Plus className="h-4 w-4" /> Add
          </button>
        )}
      </div>
      <div className="card-body">
        {people.length === 0 ? (
          <div className="text-sm text-muted-foreground flex items-center gap-2">
            <Users2 className="h-4 w-4" /> None on file yet.
          </div>
        ) : (
          <div className="flex flex-wrap gap-3">
            {people.map((p) => {
              const edge = edgeByPerson.get(p.person_id);
              return (
                <div
                  key={p.person_id}
                  className="flex items-center gap-3 border border-border rounded-full pl-1 pr-3 py-1 hover:border-primary/40 hover:bg-primary/5 cursor-pointer transition-colors group"
                  onClick={() => onSelect(p.person_id)}
                  title="Click name to focus the tree on this person"
                >
                  {/* Avatar drills into the person's full record. The
                      stopPropagation keeps the surrounding chip's
                      onSelect (re-focus) from also firing. */}
                  <Link
                    to={`/admin/families/${familyId}/people/${p.person_id}`}
                    onClick={(e) => e.stopPropagation()}
                    title="Open profile"
                    className="rounded-full ring-2 ring-transparent hover:ring-primary/40 transition"
                  >
                    <ProfileAvatar person={p} size={40} />
                  </Link>
                  <div className="pr-1">
                    <div className="text-sm font-medium leading-tight">
                      {p.preferred_name || p.first_name} {p.last_name}
                    </div>
                    {p.primary_family_relationship && (
                      <div className="text-[11px] text-muted-foreground leading-tight">
                        {p.primary_family_relationship}
                      </div>
                    )}
                  </div>
                  {onRemove && edge && (
                    <button
                      className="text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm("Remove this relationship?"))
                          onRemove(edge.person_relationship_id);
                      }}
                      aria-label="remove relationship"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function AddEdgeModal({
  focus,
  group,
  people,
  edges,
  onClose,
  onSubmit,
  submitting,
}: {
  focus: Person | null;
  group: TargetGroup | null;
  people: Person[];
  edges: PersonRelationship[];
  onClose: () => void;
  onSubmit: (v: AddForm) => void;
  submitting: boolean;
}) {
  const open = group !== null;
  const [otherId, setOtherId] = useState<number | "">("");

  useEffect(() => {
    if (!open) setOtherId("");
  }, [open]);

  if (!focus || !group) {
    return (
      <Modal open={open} onClose={onClose} title="Add relationship">
        <div />
      </Modal>
    );
  }

  // Filter out invalid choices (self, already-connected for this group).
  const excluded = new Set<number>([focus.person_id]);
  edges.forEach((e) => {
    if (group === "parent" && e.relationship_type === "parent_of" && e.to_person_id === focus.person_id) {
      excluded.add(e.from_person_id);
    }
    if (group === "child" && e.relationship_type === "parent_of" && e.from_person_id === focus.person_id) {
      excluded.add(e.to_person_id);
    }
    if (group === "spouse" && e.relationship_type === "spouse_of" && e.from_person_id === focus.person_id) {
      excluded.add(e.to_person_id);
    }
  });
  const selectable = people.filter((p) => !excluded.has(p.person_id));

  const titleMap: Record<TargetGroup, string> = {
    parent: "Add a parent",
    child: "Add a child",
    spouse: "Set spouse",
  };
  const helpMap: Record<TargetGroup, string> = {
    parent: `Who is a parent of ${focus.preferred_name || focus.first_name}?`,
    child: `Who is a child of ${focus.preferred_name || focus.first_name}?`,
    spouse: `Who is ${focus.preferred_name || focus.first_name}'s spouse/partner?`,
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={titleMap[group]}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={submitting || otherId === ""}
            onClick={() =>
              onSubmit({ target: group, other_person_id: Number(otherId) })
            }
          >
            {submitting ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">{helpMap[group]}</p>
        <Field label="Person" htmlFor="other_person">
          <select
            id="other_person"
            className="input"
            value={otherId}
            onChange={(e) =>
              setOtherId(e.target.value === "" ? "" : Number(e.target.value))
            }
          >
            <option value="">— Choose a person —</option>
            {selectable.map((p) => (
              <option key={p.person_id} value={p.person_id}>
                {p.preferred_name || p.first_name} {p.last_name}
                {p.primary_family_relationship
                  ? ` (${p.primary_family_relationship})`
                  : ""}
              </option>
            ))}
          </select>
        </Field>
        {selectable.length === 0 && (
          <div className="text-xs text-muted-foreground">
            Everyone in the family is already linked in this role — add more people
            first to extend the tree.
          </div>
        )}
      </div>
    </Modal>
  );
}
