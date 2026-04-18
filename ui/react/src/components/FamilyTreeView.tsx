import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Person, PersonRelationship } from "@/lib/types";
import { ProfileAvatar } from "@/components/ProfileAvatar";

/**
 * Visual family-tree renderer.
 *
 * Layout strategy
 * ---------------
 * 1. Build a directed graph from ``parent_of`` edges and an undirected
 *    index of ``spouse_of`` edges.
 * 2. Assign every person a generation index:
 *        gen(p) = 1 + max(gen(parent) for parent in parents(p)), 0 when none.
 * 3. Union spouses into the same couple group so they render side by side.
 * 4. Group couples by generation and render each generation as a flex row.
 * 5. After layout, measure card positions and draw SVG connectors:
 *       - a short horizontal line between spouses inside a couple
 *       - a rounded vertical path from each parent's bottom to each
 *         child's top (cubic bezier, shared control point per parent pair)
 */

interface Props {
  people: Person[];
  edges: PersonRelationship[];
  onSelect?: (personId: number) => void;
}

interface Couple {
  ids: number[]; // one or two person_ids; two = spouse pair
}

const CARD_W = 128;
const CARD_H = 92;
const ROW_GAP = 56;
const COUPLE_GAP = 40;
const INTER_CARD = 8;

export function FamilyTreeView({ people, edges, onSelect }: Props) {
  const { generations, parentsOf, containerWidth } = useMemo(
    () => layoutTree(people, edges),
    [people, edges]
  );

  const containerRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const [lines, setLines] = useState<LineSpec[]>([]);
  const [size, setSize] = useState({ w: 0, h: 0 });

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const update = () => {
      const rect = container.getBoundingClientRect();
      const positions = new Map<number, { cx: number; cy: number; top: number; bottom: number }>();
      cardRefs.current.forEach((el, id) => {
        const b = el.getBoundingClientRect();
        positions.set(id, {
          cx: b.left - rect.left + b.width / 2,
          cy: b.top - rect.top + b.height / 2,
          top: b.top - rect.top,
          bottom: b.bottom - rect.top,
        });
      });

      const next: LineSpec[] = [];

      // Spouse horizontal ticks.
      edges.forEach((e) => {
        if (e.relationship_type !== "spouse_of") return;
        if (e.from_person_id >= e.to_person_id) return; // dedupe symmetric rows
        const a = positions.get(e.from_person_id);
        const b = positions.get(e.to_person_id);
        if (!a || !b) return;
        next.push({
          kind: "spouse",
          d: `M ${a.cx},${a.cy} L ${b.cx},${b.cy}`,
          key: `spouse-${e.from_person_id}-${e.to_person_id}`,
        });
      });

      // Parent -> child curves.
      edges.forEach((e) => {
        if (e.relationship_type !== "parent_of") return;
        const parent = positions.get(e.from_person_id);
        const child = positions.get(e.to_person_id);
        if (!parent || !child) return;
        const x1 = parent.cx;
        const y1 = parent.bottom;
        const x2 = child.cx;
        const y2 = child.top;
        const midY = (y1 + y2) / 2;
        next.push({
          kind: "parent",
          d: `M ${x1},${y1} C ${x1},${midY} ${x2},${midY} ${x2},${y2}`,
          key: `p-${e.from_person_id}-${e.to_person_id}`,
        });
      });

      setLines(next);
      setSize({ w: rect.width, h: rect.height });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(container);
    window.addEventListener("resize", update);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [edges, generations]);

  if (people.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">
        Add people on the People page to start building the family tree.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <div
        ref={containerRef}
        className="relative mx-auto"
        style={{ minWidth: containerWidth, paddingTop: 8, paddingBottom: 8 }}
      >
        <svg
          className="absolute inset-0 pointer-events-none"
          width={size.w || containerWidth}
          height={size.h || 200}
        >
          {lines.map((ln) =>
            ln.kind === "spouse" ? (
              <path
                key={ln.key}
                d={ln.d}
                fill="none"
                stroke="#a78bfa"
                strokeWidth={2}
                strokeDasharray="4 4"
              />
            ) : (
              <path
                key={ln.key}
                d={ln.d}
                fill="none"
                stroke="#94a3b8"
                strokeWidth={1.5}
              />
            )
          )}
        </svg>

        <div className="relative flex flex-col" style={{ gap: ROW_GAP }}>
          {generations.map((row, gIdx) => (
            <div
              key={gIdx}
              className="flex items-start justify-center"
              style={{ gap: COUPLE_GAP }}
            >
              {row.map((couple, cIdx) => (
                <div
                  key={`${gIdx}-${cIdx}`}
                  className="flex items-start"
                  style={{ gap: INTER_CARD }}
                >
                  {couple.ids.map((id) => {
                    const p = people.find((x) => x.person_id === id);
                    if (!p) return null;
                    const hasParents = (parentsOf.get(id)?.length ?? 0) > 0;
                    return (
                      <div
                        key={id}
                        ref={(el) => {
                          if (el) cardRefs.current.set(id, el);
                          else cardRefs.current.delete(id);
                        }}
                        onClick={() => onSelect?.(id)}
                        className="border border-border rounded-lg bg-white shadow-sm hover:shadow-md hover:border-primary/40 transition-all cursor-pointer flex flex-col items-center justify-center gap-1 px-2 py-2"
                        style={{ width: CARD_W, height: CARD_H }}
                        title={
                          hasParents
                            ? `${p.first_name} ${p.last_name}`
                            : `${p.first_name} ${p.last_name} (root)`
                        }
                      >
                        <ProfileAvatar person={p} size={44} />
                        <div className="text-xs font-medium leading-tight text-center line-clamp-1">
                          {p.preferred_name || p.first_name}{" "}
                          {p.last_name}
                        </div>
                        {p.primary_family_relationship && (
                          <div className="text-[10px] text-muted-foreground leading-none">
                            {p.primary_family_relationship}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layout helpers
// ---------------------------------------------------------------------------

interface LineSpec {
  kind: "spouse" | "parent";
  d: string;
  key: string;
}

function layoutTree(people: Person[], edges: PersonRelationship[]) {
  const childrenOf = new Map<number, number[]>();
  const parentsOf = new Map<number, number[]>();
  const spousesOf = new Map<number, Set<number>>();

  edges.forEach((e) => {
    if (e.relationship_type === "parent_of") {
      (childrenOf.get(e.from_person_id) ?? childrenOf.set(e.from_person_id, []).get(e.from_person_id)!).push(
        e.to_person_id
      );
      (parentsOf.get(e.to_person_id) ?? parentsOf.set(e.to_person_id, []).get(e.to_person_id)!).push(
        e.from_person_id
      );
    } else if (e.relationship_type === "spouse_of") {
      const s = spousesOf.get(e.from_person_id) ?? new Set<number>();
      s.add(e.to_person_id);
      spousesOf.set(e.from_person_id, s);
    }
  });

  // Generation via memoized DFS.
  const gen = new Map<number, number>();
  const visiting = new Set<number>();
  const computeGen = (id: number): number => {
    if (gen.has(id)) return gen.get(id)!;
    if (visiting.has(id)) return 0; // cycle safety; shouldn't happen
    visiting.add(id);
    const parents = parentsOf.get(id) ?? [];
    const g = parents.length === 0 ? 0 : Math.max(...parents.map((p) => computeGen(p))) + 1;
    visiting.delete(id);
    gen.set(id, g);
    return g;
  };
  people.forEach((p) => computeGen(p.person_id));

  // Align spouses to the same generation (the max of the two).
  people.forEach((p) => {
    const partners = spousesOf.get(p.person_id);
    if (!partners) return;
    partners.forEach((q) => {
      const gp = gen.get(p.person_id) ?? 0;
      const gq = gen.get(q) ?? 0;
      const target = Math.max(gp, gq);
      gen.set(p.person_id, target);
      gen.set(q, target);
    });
  });

  // Couples: union-find on spouse pairs so each person is in exactly one couple.
  const couple = new Map<number, number>(); // person_id -> couple index
  let coupleIdx = 0;
  const couples: number[][] = [];
  const assignCouple = (ids: number[]) => {
    const idx = coupleIdx++;
    couples[idx] = ids;
    ids.forEach((id) => couple.set(id, idx));
  };
  people.forEach((p) => {
    if (couple.has(p.person_id)) return;
    const partners = Array.from(spousesOf.get(p.person_id) ?? []);
    if (partners.length > 0) {
      const partner = partners.find((q) => !couple.has(q));
      if (partner !== undefined) {
        assignCouple([p.person_id, partner]);
        return;
      }
    }
    assignCouple([p.person_id]);
  });

  // Bucket couples by generation, keeping a stable order.
  const byGen = new Map<number, Couple[]>();
  couples.forEach((ids) => {
    const g = gen.get(ids[0]) ?? 0;
    const bucket = byGen.get(g) ?? [];
    bucket.push({ ids });
    byGen.set(g, bucket);
  });

  const minG = Math.min(...Array.from(byGen.keys()), 0);
  const maxG = Math.max(...Array.from(byGen.keys()), 0);
  const generations: Couple[][] = [];
  for (let g = minG; g <= maxG; g++) {
    generations.push(byGen.get(g) ?? []);
  }

  // Sort each generation so siblings (shared parents) stay adjacent.
  generations.forEach((row, idx) => {
    if (idx === 0) return;
    row.sort((a, b) => {
      const ap = parentsOf.get(a.ids[0]) ?? [];
      const bp = parentsOf.get(b.ids[0]) ?? [];
      const ak = ap.slice().sort().join(",");
      const bk = bp.slice().sort().join(",");
      return ak.localeCompare(bk);
    });
  });

  const widest = generations.reduce((acc, row) => {
    const w =
      row.reduce(
        (s, c) => s + c.ids.length * CARD_W + (c.ids.length - 1) * INTER_CARD,
        0
      ) +
      Math.max(0, row.length - 1) * COUPLE_GAP;
    return Math.max(acc, w);
  }, 0);

  return {
    generations,
    parentsOf,
    containerWidth: Math.max(widest + 32, 320),
  };
}
