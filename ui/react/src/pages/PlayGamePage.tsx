import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useParams } from "react-router-dom";
import { Check, Eye, Flag, Play, RotateCcw, Share2, Trophy, WifiOff } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { SHIRT_COLOR_KEYS, SHIRT_COLORS, Turkey, TURKEY_STYLES } from "@/games/Turkey";
import { TurkeyBoard } from "@/games/TurkeyBoard";
import { shareJoinLink } from "@/games/share";
import { useGameSocket } from "@/games/useGameSocket";
import type { GameAction, GamePlayer, GameView, ShirtColor, TurkeyStyle } from "@/games/types";
import "@/games/games.css";

// The page behind a player's join link, /play/<token>. It needs no
// sign-in: the token says who this is and which game they're in.
//
//   welcome  → who invited you, pick a turkey and a shirt, join
//   lobby    → the board, with a "Start game" card once two are in
//   playing  → tap a square to pick it, tap it again to place
//   round over → the winning line, congratulations, play again / end

export default function PlayGamePage() {
  const { token } = useParams();
  const [game, setGame] = useState<GameView | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const noticeTimer = useRef<number>();

  const flash = useCallback((message: string) => {
    setNotice(message);
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 3500);
  }, []);
  useEffect(() => () => window.clearTimeout(noticeTimer.current), []);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      setGame(await api.get<GameView>(`/api/games/play/${encodeURIComponent(token)}`));
    } catch (err) {
      // A bad or expired link is final. Anything else (the network, a
      // restart) is left to the socket, which keeps retrying.
      if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
        setFatal(err.message);
      }
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const { status: link, send } = useGameSocket(token, fatal === null, {
    onState: setGame,
    onError: flash,
    onDropped: load,
  });

  const act = useCallback(
    (action: GameAction) => {
      if (!send(action)) flash("Reconnecting — try that again in a moment.");
    },
    [send, flash]
  );

  useEffect(() => {
    const before = document.title;
    document.title = game?.game_name ?? "Family game";
    return () => {
      document.title = before;
    };
  }, [game?.game_name]);

  if (fatal) return <Sorry message={fatal} />;
  if (!game) return <Loading />;
  if (game.status === "ended") return <GameOver game={game} />;
  if (!game.you.joined || picking) {
    return (
      <Welcome
        game={game}
        online={link === "open"}
        notice={notice}
        onJoin={(style, color) => {
          act({ type: "join", style, color });
          setPicking(false);
        }}
        onCancel={picking ? () => setPicking(false) : undefined}
      />
    );
  }
  return (
    <GameScreen
      game={game}
      online={link === "open"}
      notice={notice}
      act={act}
      flash={flash}
      onChangeTurkey={() => setPicking(true)}
    />
  );
}

// ---------------------------------------------------------------------------
// Small shared pieces
// ---------------------------------------------------------------------------

function meOf(game: GameView): GamePlayer | undefined {
  return game.players.find((p) => p.player_id === game.you.player_id);
}

function PlayerTurkey({ player, size, className }: { player: GamePlayer; size: number; className?: string }) {
  if (!player.avatar_style || !player.avatar_color) return <NotHereYet size={size} />;
  return <Turkey style={player.avatar_style} color={player.avatar_color} size={size} className={className} title={player.name} />;
}

/** Stand-in for someone who was invited but hasn't picked a turkey: an egg. */
function NotHereYet({ size }: { size: number }) {
  return (
    <svg viewBox="0 0 120 120" width={size} height={size} aria-hidden>
      <ellipse cx="60" cy="66" rx="30" ry="38" fill="#fffaf0" stroke="#c9b79c" strokeWidth="3" strokeDasharray="7 6" />
      <text x="60" y="80" textAnchor="middle" fontSize="40" fontWeight="800" fill="#c9b79c">
        ?
      </text>
    </svg>
  );
}

const LEAF_COLORS = ["#e8590c", "#f59f00", "#c92a2a", "#d9480f", "#f5b800", "#a0522d"];

function FallingLeaves({ count = 22 }: { count?: number }) {
  const leaves = useMemo(
    () =>
      Array.from({ length: count }, (_, i) => {
        // Cheap repeatable scatter; no need for real randomness.
        const r = (n: number) => ((Math.sin((i + 1) * n) + 1) / 2) % 1;
        return {
          left: `${Math.round(r(12.9898) * 100)}%`,
          size: 14 + Math.round(r(78.233) * 14),
          color: LEAF_COLORS[i % LEAF_COLORS.length],
          duration: `${3.2 + r(37.719) * 3}s`,
          delay: `${-r(4.581) * 6}s`,
          drift: `${Math.round((r(91.17) - 0.5) * 120)}px`,
          spin: `${Math.round(360 + r(53.3) * 540) * (i % 2 ? 1 : -1)}deg`,
        };
      }),
    [count]
  );
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden>
      {leaves.map((leaf, i) => (
        <svg
          key={i}
          className="tg-leaf"
          viewBox="-10 -12 20 26"
          width={leaf.size}
          height={leaf.size * 1.3}
          style={
            {
              left: leaf.left,
              animationDuration: leaf.duration,
              animationDelay: leaf.delay,
              "--tg-drift": leaf.drift,
              "--tg-spin": leaf.spin,
            } as CSSProperties
          }
        >
          <path d="M0 -11 C8 -8 9 5 0 10 C-9 5 -8 -8 0 -11 Z" fill={leaf.color} />
          <path d="M0 -8 V13" stroke="#6f3d14" strokeWidth="1.2" strokeLinecap="round" fill="none" />
        </svg>
      ))}
    </div>
  );
}

function Notice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="fixed left-1/2 top-3 z-50 -translate-x-1/2 max-w-[92vw] rounded-full bg-[#b3261e] px-4 py-2 text-sm font-semibold text-white shadow-lg tg-rise"
    >
      {message}
    </div>
  );
}

function Loading() {
  return (
    <div className="turkey-theme turkey-screen flex flex-col items-center justify-center gap-3">
      <span className="tg-bob">
        <Turkey style="happy" color="orange" size={120} />
      </span>
      <p className="font-semibold tg-dots">Setting the table</p>
    </div>
  );
}

function Sorry({ message }: { message: string }) {
  return (
    <div className="turkey-theme turkey-screen flex flex-col items-center justify-center gap-3 px-6 text-center">
      <Turkey style="hippie" color="green" size={130} />
      <h1 className="tg-title text-3xl">{message}</h1>
      <p className="max-w-xs text-sm">
        Game links work for a day. Ask whoever invited you to start a new game and send you a fresh link.
      </p>
      <a href="/" className="tg-btn tg-btn-quiet mt-2">
        Open Family Assistant
      </a>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Welcome: who invited you, pick a turkey, join
// ---------------------------------------------------------------------------

interface WelcomeProps {
  game: GameView;
  online: boolean;
  notice: string | null;
  onJoin: (style: TurkeyStyle, color: ShirtColor) => void;
  /** Set when a player who is already in is just changing turkeys. */
  onCancel?: () => void;
}

function Welcome({ game, online, notice, onJoin, onCancel }: WelcomeProps) {
  const me = meOf(game);
  const others = game.players.filter((p) => p.player_id !== game.you.player_id);
  const takenBy = new Map<ShirtColor, string>();
  for (const p of others) {
    if (p.joined && p.avatar_color) takenBy.set(p.avatar_color, p.name);
  }
  const firstFree = SHIRT_COLOR_KEYS.find((c) => !takenBy.has(c)) ?? SHIRT_COLOR_KEYS[0];

  const [style, setStyle] = useState<TurkeyStyle>(me?.avatar_style ?? "happy");
  const [picked, setPicked] = useState<ShirtColor | null>(me?.avatar_color ?? null);
  // If someone else takes the colour while this player is choosing,
  // fall back to one that is still free.
  const color = picked && !takenBy.has(picked) ? picked : firstFree;

  const underWay = game.status !== "lobby";
  return (
    <div className="turkey-theme min-h-[100dvh]">
      <Notice message={notice} />
      <div className="mx-auto flex max-w-md flex-col gap-4 px-4 pb-8 pt-6">
        <header className="text-center">
          <p className="text-sm font-semibold text-[color:var(--tg-ink-soft)]">
            {game.you.is_host ? "You're starting a game of" : `${game.invited_by} invited you to play`}
          </p>
          <h1 className="tg-title mt-1 text-4xl leading-none">{game.game_name}</h1>
          <p className="mx-auto mt-3 max-w-xs text-sm">
            Hi {game.you.name}! Get four of your turkeys in a row — across, down or diagonally — before anyone
            else does.
          </p>
        </header>

        <section className="tg-card p-4">
          <div className="flex items-center gap-3">
            <span className="tg-cheer shrink-0">
              <Turkey style={style} color={color} size={104} />
            </span>
            <div className="min-w-0">
              <div className="text-xs font-bold uppercase tracking-wide text-[color:var(--tg-ink-soft)]">
                Your turkey
              </div>
              <div className="text-xl font-black leading-tight">
                {TURKEY_STYLES.find((s) => s.key === style)?.label} in {SHIRT_COLORS[color].label.toLowerCase()}
              </div>
              {underWay && !game.you.joined && (
                <p className="mt-1 text-xs">A round is under way. You'll watch it, then play in the next one.</p>
              )}
            </div>
          </div>

          <h2 className="mb-2 mt-4 text-sm font-extrabold">Pick your turkey</h2>
          <div className="grid grid-cols-3 gap-2">
            {TURKEY_STYLES.map((s) => (
              <button
                key={s.key}
                type="button"
                onClick={() => setStyle(s.key)}
                aria-pressed={style === s.key}
                className={cn(
                  "flex flex-col items-center rounded-2xl border-2 bg-white px-1 pb-1.5 pt-1 transition",
                  style === s.key ? "border-[#e8590c] shadow-md" : "border-transparent opacity-80"
                )}
              >
                <Turkey style={s.key} color={color} size={76} />
                <span className="text-xs font-bold">{s.label}</span>
              </button>
            ))}
          </div>

          <h2 className="mb-2 mt-4 text-sm font-extrabold">Pick your shirt</h2>
          <div className="grid grid-cols-8 gap-1.5">
            {SHIRT_COLOR_KEYS.map((c) => {
              const owner = takenBy.get(c);
              return (
                <button
                  key={c}
                  type="button"
                  disabled={!!owner}
                  onClick={() => setPicked(c)}
                  aria-pressed={color === c}
                  aria-label={owner ? `${SHIRT_COLORS[c].label}, taken by ${owner}` : SHIRT_COLORS[c].label}
                  title={owner ? `${owner} has ${SHIRT_COLORS[c].label.toLowerCase()}` : SHIRT_COLORS[c].label}
                  className={cn(
                    "relative flex aspect-square items-center justify-center rounded-full border-2 border-white shadow",
                    color === c && "ring-2 ring-[#4a2c14] ring-offset-2 ring-offset-[#fffaee]",
                    owner && "opacity-30 shadow-none"
                  )}
                  style={{ backgroundColor: SHIRT_COLORS[c].main }}
                >
                  {color === c && <Check className="h-4 w-4 text-white" strokeWidth={4} />}
                  {owner && <span className="absolute h-0.5 w-[115%] rotate-45 rounded bg-[#4a2c14]" />}
                </button>
              );
            })}
          </div>
          {takenBy.size > 0 && (
            <p className="mt-2 text-xs text-[color:var(--tg-ink-soft)]">
              Taken:{" "}
              {[...takenBy].map(([c, name]) => `${SHIRT_COLORS[c].label.toLowerCase()} (${name})`).join(", ")}
            </p>
          )}

          <button
            type="button"
            className="tg-btn tg-btn-primary mt-5 w-full text-lg"
            disabled={!online}
            onClick={() => onJoin(style, color)}
          >
            {!online ? "Connecting…" : game.you.joined ? "Use this turkey" : "Join game"}
          </button>
          {onCancel && (
            <button type="button" className="mt-3 w-full text-sm font-semibold underline" onClick={onCancel}>
              Never mind
            </button>
          )}
        </section>

        {others.length > 0 && (
          <section className="tg-card px-4 py-3">
            <h2 className="mb-1 text-sm font-extrabold">Who's playing</h2>
            <ul className="divide-y divide-[rgba(74,44,20,0.1)]">
              {others.map((p) => (
                <li key={p.player_id} className="flex items-center gap-2 py-1.5">
                  <PlayerTurkey player={p} size={36} />
                  <span className="min-w-0 flex-1 truncate font-semibold">{p.name}</span>
                  <span className="text-xs text-[color:var(--tg-ink-soft)]">
                    {p.joined ? (p.is_host ? "Started the game" : "In") : "Invited"}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The game: scoreboard, whose turn, board, overlays
// ---------------------------------------------------------------------------

interface GameScreenProps {
  game: GameView;
  online: boolean;
  notice: string | null;
  act: (action: GameAction) => void;
  flash: (message: string) => void;
  onChangeTurkey: () => void;
}

function GameScreen({ game, online, notice, act, flash, onChangeTurkey }: GameScreenProps) {
  const board = game.board;
  const me = meOf(game);
  const byId = new Map(game.players.map((p) => [p.player_id, p]));
  const [selected, setSelected] = useState<number | null>(null);
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [celebrate, setCelebrate] = useState(false);
  const [peek, setPeek] = useState(false);

  const playing = game.status === "playing";
  const roundOver = game.status === "round_over";
  const turnPlayer = board?.turn_player_id != null ? byId.get(board.turn_player_id) : undefined;
  const myTurn = playing && turnPlayer?.player_id === game.you.player_id;
  const inRound = !!board && board.turn_order.includes(game.you.player_id);
  const winner = board?.winner_player_id != null ? byId.get(board.winner_player_id) : undefined;
  const iWon = winner?.player_id === game.you.player_id;

  // A pick only lasts for the turn it was made in.
  useEffect(() => {
    setSelected(null);
  }, [board?.turn_player_id, board?.round, game.status]);

  useEffect(() => {
    if (myTurn) navigator.vibrate?.(35);
  }, [myTurn, board?.round]);

  // Let the line finish drawing through the four turkeys before the
  // congratulations cover the board.
  const hasWinner = winner !== undefined;
  useEffect(() => {
    setPeek(false);
    if (!roundOver) {
      setCelebrate(false);
      return;
    }
    const timer = window.setTimeout(() => setCelebrate(true), hasWinner ? 1300 : 300);
    return () => window.clearTimeout(timer);
  }, [roundOver, hasWinner, board?.round]);

  const onCellTap = (cell: number) => {
    if (!myTurn || !online) return;
    if (selected === cell) {
      act({ type: "move", cell });
      setSelected(null);
    } else {
      setSelected(cell);
    }
  };

  // Scoreboard: this round's turn order first, then anyone waiting for the next.
  const joined = game.players.filter((p) => p.joined);
  const order = board ? board.turn_order : [];
  const seats = [
    ...order.map((id) => byId.get(id)).filter((p): p is GamePlayer => !!p),
    ...joined.filter((p) => !order.includes(p.player_id)),
  ];

  let banner: string;
  if (!online) banner = "Reconnecting…";
  else if (game.status === "lobby") banner = game.can_start ? "Everyone ready?" : "Waiting for players";
  else if (roundOver) banner = winner ? (iWon ? "You got four in a row!" : `${winner.name} got four in a row!`) : "It's a draw!";
  else if (!inRound) banner = `Watching this round — ${turnPlayer?.name ?? "someone"}'s turn`;
  else if (myTurn) banner = selected != null ? "Tap it again to place your turkey" : "Your turn — tap a square";
  else banner = `${turnPlayer?.name ?? "Someone"}'s turn`;

  const turnColor = turnPlayer?.avatar_color ? SHIRT_COLORS[turnPlayer.avatar_color].main : "#e8590c";

  return (
    <div className="turkey-theme turkey-screen flex flex-col">
      <Notice message={notice} />

      <header className="flex items-center gap-2 px-3 pt-2">
        <h1 className="tg-title min-w-0 flex-1 truncate text-xl leading-tight">{game.game_name}</h1>
        {board && (
          <span className="rounded-full bg-white/60 px-2 py-0.5 text-xs font-bold">Round {board.round}</span>
        )}
        <button
          type="button"
          onClick={() => setConfirmEnd(true)}
          className="flex items-center gap-1 rounded-full bg-white/60 px-2.5 py-1 text-xs font-bold hover:bg-white"
        >
          <Flag className="h-3.5 w-3.5" /> End
        </button>
      </header>

      <div className="flex gap-1.5 px-2 pt-2">
        {seats.map((p) => {
          const isTurn = playing && p.player_id === turnPlayer?.player_id;
          return (
            <div
              key={p.player_id}
              className={cn(
                "flex min-w-0 flex-1 items-center gap-1 rounded-2xl bg-white/55 px-1.5 py-1 transition",
                isTurn && "tg-turn-chip",
                !p.connected && "opacity-50"
              )}
              style={
                isTurn && p.avatar_color
                  ? ({ "--tg-chip": SHIRT_COLORS[p.avatar_color].main } as CSSProperties)
                  : undefined
              }
            >
              <span className={cn("shrink-0", isTurn && "tg-bob")}>
                <PlayerTurkey player={p} size={seats.length > 3 ? 30 : 38} />
              </span>
              <div className="min-w-0 leading-tight">
                <div className="truncate text-[13px] font-extrabold">{p.name}</div>
                <div className="flex items-center gap-1 text-[11px] font-semibold text-[color:var(--tg-ink-soft)]">
                  <Trophy className="h-3 w-3 shrink-0 text-[#d48806]" />
                  <span>{p.wins}</span>
                  <span className="truncate">
                    {!p.connected ? "· away" : p.player_id === game.you.player_id ? "· you" : ""}
                  </span>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div
        role="status"
        aria-live="polite"
        className={cn(
          "mx-2 mt-2 flex items-center justify-center gap-2 rounded-full px-3 py-1.5 text-center text-sm font-extrabold",
          !online ? "bg-[#fff3bf]" : myTurn ? "text-white" : "bg-white/60"
        )}
        style={online && myTurn ? { backgroundColor: turnColor } : undefined}
      >
        {!online && <WifiOff className="h-4 w-4" />}
        <span className="truncate">{banner}</span>
      </div>

      <div className="relative min-h-0 flex-1 p-2">
        <TurkeyBoard
          board={board}
          players={game.players}
          me={me}
          canPlay={myTurn && online}
          selected={selected}
          onCellTap={onCellTap}
        />

        {game.status === "lobby" && (
          <Overlay>
            <Lobby game={game} act={act} flash={flash} onChangeTurkey={onChangeTurkey} />
          </Overlay>
        )}

        {roundOver && celebrate && !peek && (
          <Overlay leaves={!!winner}>
            <div className="tg-card tg-rise w-full max-w-sm p-5 text-center">
              {winner ? (
                <>
                  <span className="tg-cheer inline-block">
                    <PlayerTurkey player={winner} size={128} />
                  </span>
                  <h2 className="tg-title text-4xl leading-none">{iWon ? "You win!" : `${winner.name} wins!`}</h2>
                  <p className="mt-1 text-sm font-semibold">Four in a row. Gobble gobble!</p>
                </>
              ) : (
                <>
                  <div className="flex justify-center">
                    {seats.slice(0, 2).map((p) => (
                      <PlayerTurkey key={p.player_id} player={p} size={84} />
                    ))}
                  </div>
                  <h2 className="tg-title text-4xl leading-none">It's a draw!</h2>
                  <p className="mt-1 text-sm font-semibold">The board is full and nobody got four in a row.</p>
                </>
              )}
              <Scores players={joined} highlight={winner?.player_id} />
              <div className="mt-4 flex flex-col gap-2">
                <button type="button" className="tg-btn tg-btn-primary text-lg" onClick={() => act({ type: "play_again" })}>
                  <RotateCcw className="h-5 w-5" /> Play again
                </button>
                <div className="flex gap-2">
                  <button type="button" className="tg-btn tg-btn-quiet flex-1 text-sm" onClick={() => setPeek(true)}>
                    <Eye className="h-4 w-4" /> See board
                  </button>
                  <button type="button" className="tg-btn tg-btn-quiet flex-1 text-sm" onClick={() => setConfirmEnd(true)}>
                    <Flag className="h-4 w-4" /> End game
                  </button>
                </div>
              </div>
            </div>
          </Overlay>
        )}
      </div>

      {roundOver && peek && (
        <div className="flex gap-2 px-3 pb-3">
          <button type="button" className="tg-btn tg-btn-primary flex-1" onClick={() => act({ type: "play_again" })}>
            <RotateCcw className="h-5 w-5" /> Play again
          </button>
          <button type="button" className="tg-btn tg-btn-quiet" onClick={() => setPeek(false)}>
            Scores
          </button>
        </div>
      )}

      {confirmEnd && (
        <div className="tg-overlay fixed inset-0 z-40 flex items-center justify-center p-5">
          <div className="tg-card tg-rise w-full max-w-xs p-5 text-center">
            <h2 className="text-xl font-black">End the game for everyone?</h2>
            <p className="mt-1 text-sm">Wins so far stay in the family hall of fame.</p>
            <div className="mt-4 flex flex-col gap-2">
              <button
                type="button"
                className="tg-btn tg-btn-primary"
                onClick={() => {
                  setConfirmEnd(false);
                  act({ type: "end" });
                }}
              >
                End game
              </button>
              <button type="button" className="tg-btn tg-btn-quiet" onClick={() => setConfirmEnd(false)}>
                Keep playing
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Overlay({ children, leaves }: { children: ReactNode; leaves?: boolean }) {
  return (
    <div className="tg-overlay absolute inset-0 z-10 flex items-center justify-center overflow-y-auto p-3">
      {leaves && <FallingLeaves />}
      <div className="relative flex max-h-full w-full justify-center">{children}</div>
    </div>
  );
}

function Scores({ players, highlight }: { players: GamePlayer[]; highlight?: number }) {
  const ranked = [...players].sort((a, b) => b.wins - a.wins);
  return (
    <ul className="mx-auto mt-3 max-w-[16rem] space-y-1 text-left">
      {ranked.map((p) => (
        <li
          key={p.player_id}
          className={cn(
            "flex items-center gap-2 rounded-xl px-2 py-1",
            p.player_id === highlight ? "bg-[#ffe066]" : "bg-white/70"
          )}
        >
          <PlayerTurkey player={p} size={30} />
          <span className="min-w-0 flex-1 truncate font-bold">{p.name}</span>
          <span className="flex items-center gap-1 font-black tabular-nums">
            <Trophy className="h-4 w-4 text-[#d48806]" /> {p.wins}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Lobby card: who's in, share links, start
// ---------------------------------------------------------------------------

function Lobby({
  game,
  act,
  flash,
  onChangeTurkey,
}: {
  game: GameView;
  act: (action: GameAction) => void;
  flash: (message: string) => void;
  onChangeTurkey: () => void;
}) {
  const links = new Map((game.invites ?? []).map((i) => [i.player_id, i]));
  const share = async (playerId: number) => {
    const invite = links.get(playerId);
    if (!invite) return;
    const how = await shareJoinLink(invite.play_url, `${invite.name}, come play ${game.game_name} with me!`);
    if (how === "copied") flash(`Link for ${invite.name} copied — paste it in a message.`);
    if (how === "failed") flash("Couldn't copy the link.");
  };

  const waitingText = (p: GamePlayer): string => {
    const invite = links.get(p.player_id);
    if (p.invite_status === "pending") return "Sending the invite…";
    if (p.invite_status === "sent") {
      const via = invite?.invite_channels.map((c) => (c === "sms" ? "text" : "email")).join(" and ");
      return via ? `Invited by ${via}` : "Invited";
    }
    // Only the host can do something about an invite that didn't go.
    if (!invite) return "Not here yet";
    return p.invite_status === "failed" ? "Couldn't send an invite" : "Needs the link";
  };

  return (
    <div className="tg-card tg-rise w-full max-w-sm self-center p-4">
      <h2 className="tg-title text-center text-2xl leading-tight">
        {game.can_start ? "Ready to play!" : "Waiting for players"}
      </h2>
      <ul className="mt-3 space-y-1.5">
        {game.players.map((p) => (
          <li key={p.player_id} className="flex items-center gap-2 rounded-xl bg-white/70 px-2 py-1.5">
            <PlayerTurkey player={p} size={40} />
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate font-extrabold">
                {p.name}
                {p.player_id === game.you.player_id && <span className="font-semibold"> (you)</span>}
              </div>
              <div className="truncate text-xs text-[color:var(--tg-ink-soft)]">
                {p.joined ? (p.connected ? "Ready" : "Ready — stepped away") : waitingText(p)}
              </div>
            </div>
            {!p.joined && links.has(p.player_id) && (
              <button
                type="button"
                onClick={() => void share(p.player_id)}
                className="flex shrink-0 items-center gap-1 rounded-full bg-[#4a2c14] px-3 py-1.5 text-xs font-bold text-white"
              >
                <Share2 className="h-3.5 w-3.5" /> Share link
              </button>
            )}
          </li>
        ))}
      </ul>

      {game.can_start ? (
        <button type="button" className="tg-btn tg-btn-primary mt-4 w-full text-lg" onClick={() => act({ type: "start" })}>
          <Play className="h-5 w-5 fill-current" /> Start game
        </button>
      ) : (
        <p className="tg-dots mt-4 text-center text-sm font-semibold">
          The game can start once {game.min_players} players are in
        </p>
      )}
      <button type="button" className="mt-3 w-full text-center text-sm font-semibold underline" onClick={onChangeTurkey}>
        Change my turkey
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Game over: final scores
// ---------------------------------------------------------------------------

function GameOver({ game }: { game: GameView }) {
  const joined = game.players.filter((p) => p.joined);
  const best = Math.max(0, ...joined.map((p) => p.wins));
  const champions = best > 0 ? joined.filter((p) => p.wins === best) : [];
  return (
    <div className="turkey-theme relative min-h-[100dvh]">
      {champions.length > 0 && <FallingLeaves count={16} />}
      <div className="relative mx-auto flex min-h-[100dvh] max-w-sm flex-col items-center justify-center gap-3 px-5 py-8 text-center">
        <p className="text-sm font-semibold text-[color:var(--tg-ink-soft)]">{game.game_name}</p>
        {champions.length === 1 ? (
          <>
            <span className="tg-cheer">
              <PlayerTurkey player={champions[0]} size={140} />
            </span>
            <h1 className="tg-title text-4xl leading-none">
              {champions[0].player_id === game.you.player_id ? "You're the champion!" : `${champions[0].name} is the champion!`}
            </h1>
          </>
        ) : (
          <>
            <div className="flex justify-center">
              {(champions.length ? champions : joined).slice(0, 4).map((p) => (
                <PlayerTurkey key={p.player_id} player={p} size={84} />
              ))}
            </div>
            <h1 className="tg-title text-4xl leading-none">{champions.length ? "It's a tie!" : "Game over"}</h1>
          </>
        )}
        <p className="text-sm font-semibold">
          {game.rounds_played === 0
            ? "This game ended before a round was played."
            : `${game.rounds_played} ${game.rounds_played === 1 ? "round" : "rounds"} played. Thanks for playing!`}
        </p>
        {joined.length > 0 && (
          <div className="tg-card w-full p-3">
            <Scores players={joined} highlight={champions.length === 1 ? champions[0].player_id : undefined} />
          </div>
        )}
        <a href="/" className="tg-btn tg-btn-quiet mt-2">
          Open Family Assistant
        </a>
      </div>
    </div>
  );
}
