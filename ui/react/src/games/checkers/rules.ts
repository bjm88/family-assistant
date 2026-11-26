// The Chinese Checkers board as the phone needs it: where the holes
// are, which are next to which, and where a marble can go next while a
// player is building a move. The server has the rules that count
// (python/api/games/chinese_checkers.py); this only decides what to
// light up, and it must agree with them.

/** The six ways from a hole to the next one, as [dx, dz]. Opposites are paired. */
const DIRECTIONS: [number, number][] = [
  [1, 0],
  [-1, 0],
  [0, 1],
  [0, -1],
  [1, -1],
  [-1, 1],
];

const ROW_HEIGHT = Math.sqrt(3) / 2;

export interface Geometry {
  /** Where each hole is drawn, one hole apart, the top point up. */
  xy: [number, number][];
  /** For each hole, the hole next to it in each direction, or -1. */
  neighbours: number[][];
  /** For each hole, the point (0-5, clockwise from the top) it is in, or -1. */
  pointOf: number[];
  points: number[][];
}

export function buildGeometry(holes: [number, number][], points: number[][]): Geometry {
  const index = new Map<string, number>();
  holes.forEach(([x, z], i) => index.set(`${x},${z}`, i));
  const pointOf = holes.map(() => -1);
  points.forEach((members, point) => members.forEach((hole) => (pointOf[hole] = point)));
  return {
    xy: holes.map(([x, z]) => [x + z / 2, z * ROW_HEIGHT]),
    neighbours: holes.map(([x, z]) => DIRECTIONS.map(([dx, dz]) => index.get(`${x + dx},${z + dz}`) ?? -1)),
    pointOf,
    points,
  };
}

/** The point across the board from `point`. */
export function opposite(point: number): number {
  return (point + 3) % 6;
}

/**
 * Hole positions turned so that `home` is at the bottom of the screen:
 * every player sees their own marbles start nearest to them.
 */
export function turnedTowards(geometry: Geometry, home: number | null): [number, number][] {
  if (home === null) return geometry.xy;
  const angle = ((3 - home) * Math.PI) / 3;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  return geometry.xy.map(([x, y]) => [x * cos - y * sin, x * sin + y * cos]);
}

type Marbles = (number | null)[];

/** Whether a move that began with `path` began with a single step. */
export function isStep(geometry: Geometry, path: number[]): boolean {
  return path.length === 2 && geometry.neighbours[path[0]].includes(path[1]);
}

function firstMoves(geometry: Geometry, marbles: Marbles, from: number, allowed: (hole: number) => boolean) {
  const found: number[] = [];
  geometry.neighbours[from].forEach((next, direction) => {
    if (next < 0) return;
    if (marbles[next] === null) {
      if (allowed(next)) found.push(next);
      return;
    }
    const landing = geometry.neighbours[next][direction];
    if (landing >= 0 && marbles[landing] === null && allowed(landing)) found.push(landing);
  });
  return found;
}

/**
 * Where the marble being moved can go next.
 *
 * `path` is the move so far: the hole the marble started in, then
 * every hole it has landed in. With only the start in it, that is one
 * step to an empty hole next door, or a hop over one marble. After a
 * hop it is the hops that carry on from there. After a step the turn's
 * move is complete, so what is offered is the other places it could
 * have gone instead.
 *
 * `target` is the point the player is filling: a marble that began the
 * move inside it has to stay inside it.
 */
export function nextHoles(geometry: Geometry, marbles: Marbles, path: number[], target: number): number[] {
  if (path.length === 0) return [];
  const start = path[0];
  const locked = geometry.pointOf[start] === target;
  const allowed = (hole: number) => !locked || geometry.pointOf[hole] === target;
  if (path.length === 1) return firstMoves(geometry, marbles, start, allowed);
  if (isStep(geometry, path)) {
    return firstMoves(geometry, marbles, start, allowed).filter((hole) => hole !== path[1]);
  }
  // Hops, with the marble lifted off the board so it can't hop itself.
  const at = path[path.length - 1];
  const found: number[] = [];
  geometry.neighbours[at].forEach((over, direction) => {
    if (over < 0 || over === start || marbles[over] === null) return;
    const landing = geometry.neighbours[over][direction];
    // `path` holds the start too: a marble never lands in a hole twice.
    if (landing < 0 || path.includes(landing) || marbles[landing] !== null || !allowed(landing)) return;
    found.push(landing);
  });
  return found;
}

/** The holes holding one of `player`'s marbles that has somewhere to go. */
export function movableMarbles(geometry: Geometry, marbles: Marbles, player: number, target: number): number[] {
  const found: number[] = [];
  marbles.forEach((owner, hole) => {
    if (owner === player && nextHoles(geometry, marbles, [hole], target).length > 0) found.push(hole);
  });
  return found;
}

/** How many of `player`'s marbles are in the point they are filling. */
export function marblesHome(geometry: Geometry, marbles: Marbles, player: number, target: number): number {
  return geometry.points[target].reduce((n, hole) => n + (marbles[hole] === player ? 1 : 0), 0);
}
