import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { cn } from "@/lib/cn";
import { SHIRT_COLORS, Turkey } from "./Turkey";
import type { GameBoard, GamePlayer } from "./types";

const ROWS = 10;
const COLS = 5;
const GAP = 4;
const PAD = 7;
// Squares may be a little wider than tall so the tall board uses more
// of a phone's width; never taller than wide.
const MAX_ASPECT = 1.3;
const MAX_CELL = 88;

interface TurkeyBoardProps {
  board: GameBoard | null;
  players: GamePlayer[];
  /** The viewer; their turkey previews on the square they've picked. */
  me: GamePlayer | undefined;
  /** True when the viewer may place a turkey right now. */
  canPlay: boolean;
  selected: number | null;
  onCellTap: (cell: number) => void;
}

function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setSize({ width: el.clientWidth, height: el.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, size] as const;
}

/**
 * The 5 x 10 board, sized to fill whatever space it is given without
 * scrolling. Tapping an open square is reported to the parent, which
 * decides whether that picks the square or places the turkey.
 */
export function TurkeyBoard({ board, players, me, canPlay, selected, onCellTap }: TurkeyBoardProps) {
  const [ref, size] = useSize<HTMLDivElement>();
  const rows = board?.rows ?? ROWS;
  const cols = board?.cols ?? COLS;

  const fitHeight = Math.floor((size.height - 2 * PAD - (rows - 1) * GAP) / rows);
  const fitWidth = Math.floor((size.width - 2 * PAD - (cols - 1) * GAP) / cols);
  const cellHeight = Math.max(0, Math.min(fitHeight, fitWidth, MAX_CELL));
  const cellWidth = Math.max(0, Math.min(fitWidth, Math.round(cellHeight * MAX_ASPECT)));
  const turkeySize = Math.round(Math.min(cellWidth, cellHeight * 1.12));

  const byId = new Map(players.map((p) => [p.player_id, p]));
  const cells = board?.cells ?? Array<number | null>(rows * cols).fill(null);
  const winning = new Set(board?.winning_cells ?? []);
  const gridWidth = cols * cellWidth + (cols - 1) * GAP;
  const gridHeight = rows * cellHeight + (rows - 1) * GAP;

  const center = (cell: number) => ({
    x: (cell % cols) * (cellWidth + GAP) + cellWidth / 2,
    y: Math.floor(cell / cols) * (cellHeight + GAP) + cellHeight / 2,
  });

  let line: { x1: number; y1: number; x2: number; y2: number } | null = null;
  if (board && board.winning_cells.length >= 2) {
    const a = center(board.winning_cells[0]);
    const b = center(board.winning_cells[board.winning_cells.length - 1]);
    // Run the line a little past the end turkeys.
    const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    const over = Math.min(cellWidth, cellHeight) * 0.38;
    const ux = ((b.x - a.x) / length) * over;
    const uy = ((b.y - a.y) / length) * over;
    line = { x1: a.x - ux, y1: a.y - uy, x2: b.x + ux, y2: b.y + uy };
  }
  const winner = board?.winner_player_id != null ? byId.get(board.winner_player_id) : undefined;
  const lineColor = winner?.avatar_color ? SHIRT_COLORS[winner.avatar_color].main : "#e03131";

  return (
    <div ref={ref} className="h-full w-full flex items-center justify-center">
      {cellHeight > 0 && (
        <div className="tg-board relative" style={{ padding: PAD }}>
          <div
            className="grid"
            style={{
              gridTemplateColumns: `repeat(${cols}, ${cellWidth}px)`,
              gridAutoRows: `${cellHeight}px`,
              gap: GAP,
            }}
          >
            {cells.map((owner, cell) => {
              const player = owner != null ? byId.get(owner) : undefined;
              const open = owner == null && canPlay;
              const picked = open && selected === cell;
              const won = winning.has(cell);
              const row = Math.floor(cell / cols);
              const col = cell % cols;
              const tint = player?.avatar_color && !won ? SHIRT_COLORS[player.avatar_color].pale : undefined;
              return (
                <button
                  key={cell}
                  type="button"
                  disabled={!open}
                  onClick={() => onCellTap(cell)}
                  aria-label={
                    player
                      ? `Row ${row + 1}, column ${col + 1}: ${player.name}`
                      : `Row ${row + 1}, column ${col + 1}: empty${picked ? ", tap again to place" : ""}`
                  }
                  className={cn(
                    "tg-cell",
                    (row + col) % 2 === 1 && "tg-cell-alt",
                    open && "tg-cell-open",
                    picked && "tg-cell-selected",
                    won && "tg-cell-win",
                    !won && board?.last_move === cell && "tg-cell-last"
                  )}
                  style={{
                    backgroundColor: tint,
                    ...(picked && me?.avatar_color
                      ? ({ "--tg-pick": SHIRT_COLORS[me.avatar_color].main } as CSSProperties)
                      : null),
                  }}
                >
                  {player?.avatar_style && player.avatar_color && (
                    <span className={cn("block", won ? "tg-win-turkey" : "tg-placed")}>
                      <Turkey
                        style={player.avatar_style}
                        color={player.avatar_color}
                        size={turkeySize}
                        title={player.name}
                      />
                    </span>
                  )}
                  {picked && me?.avatar_style && me.avatar_color && (
                    <span className="tg-ghost block">
                      <Turkey style={me.avatar_style} color={me.avatar_color} size={turkeySize} />
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          {line && (
            <svg
              className="absolute pointer-events-none"
              style={{ left: PAD, top: PAD }}
              width={gridWidth}
              height={gridHeight}
              viewBox={`0 0 ${gridWidth} ${gridHeight}`}
              aria-hidden
            >
              <line {...line} pathLength={1} className="tg-win-line" stroke="#fff" strokeWidth={10} strokeLinecap="round" />
              <line {...line} pathLength={1} className="tg-win-line" stroke={lineColor} strokeWidth={5} strokeLinecap="round" />
            </svg>
          )}
        </div>
      )}
    </div>
  );
}
