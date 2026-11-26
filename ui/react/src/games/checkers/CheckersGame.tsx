import { useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { Check, Copy, Eye, Flag, Play, RotateCcw, Share2, Trophy, Undo2, UserPlus, WifiOff } from "lucide-react";
import { cn } from "@/lib/cn";
import { SHIRT_COLOR_KEYS, SHIRT_COLORS } from "../Turkey";
import { canShare, copyLink, recallGuestName, shareJoinLink } from "../share";
import type { GameAction, GamePlayer, GameView, GuestGameView, MarbleStyle, ShirtColor } from "../types";
import { CheckersBoard } from "./CheckersBoard";
import { Marble } from "./Marble";
import { buildGeometry, isStep, marblesHome, movableMarbles, nextHoles, opposite } from "./rules";
import "./checkers.css";

// Chinese Checkers, on the pages behind a player's link (/play/<token>)
// and a game's shareable link (/play/join/<invite token>).
//
//   welcome → who invited you, pick a color, join
//   lobby   → who's in, links to share, "Start game"
//   playing → tap a marble, tap where it goes, hop again if you can,
//             then Confirm. Everyone watches the marble make the move.
//   round over → who filled the far point first, play again / end

export const CHECKERS_GAME = "chinese_checkers";
const GUEST_NAME_MAX_LENGTH = 24;

function meOf(game: GameView | GuestGameView): GamePlayer | undefined {
  const mine = game.you?.player_id;
  return game.players.find((p) => p.player_id === mine);
}

function PlayerMarble({ player, size, className }: { player: GamePlayer; size: number; className?: string }) {
  if (!player.avatar_color) {
    // Invited, but hasn't picked a color yet.
    return (
      <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden>
        <circle cx="12" cy="12" r="9.5" fill="rgba(128,128,128,0.12)" stroke="#9aa7a3" strokeWidth="1.4" strokeDasharray="3 2.6" />
        <text x="12" y="16.4" textAnchor="middle" fontSize="12" fontWeight="800" fill="#9aa7a3">
          ?
        </text>
      </svg>
    );
  }
  return <Marble color={player.avatar_color} size={size} className={className} title={player.name} />;
}

function Notice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="cc-rise fixed left-1/2 top-3 z-50 max-w-[92vw] -translate-x-1/2 rounded-full bg-[#b3261e] px-4 py-2 text-sm font-semibold text-white shadow-lg"
    >
      {message}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Welcome: who invited you, pick a color, join
// ---------------------------------------------------------------------------

interface WelcomeProps {
  /** With `you: null` this is a guest who has yet to say who they are. */
  game: GameView | GuestGameView;
  /** Whether joining can go through right now (connected, not already joining). */
  ready: boolean;
  notice: string | null;
  /** Why the last try at joining was turned down, shown beside the Join button. */
  problem?: string | null;
  /** `name` is what a guest typed; empty for a player who already has one. */
  onJoin: (style: MarbleStyle, color: ShirtColor, name: string) => void;
  /** Set when a player who is already in is just changing color. */
  onCancel?: () => void;
}

export function CheckersWelcome({ game, ready, notice, problem, onJoin, onCancel }: WelcomeProps) {
  const me = meOf(game);
  const you = game.you;
  const others = game.players.filter((p) => p.player_id !== you?.player_id);
  const takenBy = new Map<ShirtColor, string>();
  for (const p of others) {
    if (p.joined && p.avatar_color) takenBy.set(p.avatar_color, p.name);
  }
  const firstFree = SHIRT_COLOR_KEYS.find((c) => !takenBy.has(c)) ?? SHIRT_COLOR_KEYS[0];

  const [picked, setPicked] = useState<ShirtColor | null>(me?.avatar_color ?? null);
  const [name, setName] = useState(() => (you ? "" : recallGuestName().slice(0, GUEST_NAME_MAX_LENGTH)));
  // If someone else takes the color while this player is choosing,
  // fall back to one that is still free.
  const color = picked && !takenBy.has(picked) ? picked : firstFree;

  const guestName = name.trim();
  const needsName = !you && !guestName;
  const underWay = game.status !== "lobby";
  return (
    <div className="checkers-theme min-h-[100dvh]">
      <Notice message={notice} />
      <div className="mx-auto flex max-w-md flex-col gap-4 px-4 pb-8 pt-6">
        <header className="text-center">
          <p className="cc-soft text-sm font-semibold">
            {you?.is_host ? "You're starting a game of" : `${game.invited_by} invited you to play`}
          </p>
          <h1 className="cc-title mt-1 text-4xl leading-none">{game.game_name}</h1>
          <p className="mx-auto mt-3 max-w-xs text-sm">
            {you ? `Hi ${you.name}! ` : ""}Hop your marbles across the star. The first to fill the point on the far
            side wins.
          </p>
        </header>

        <section className="cc-card p-4">
          {!you && (
            <div className="mb-4">
              <label htmlFor="guest-name" className="mb-1.5 block text-sm font-extrabold">
                What's your name?
              </label>
              <input
                id="guest-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={GUEST_NAME_MAX_LENGTH}
                placeholder="So everyone knows who's playing"
                autoComplete="given-name"
                autoCapitalize="words"
                enterKeyHint="done"
                className="cc-input"
              />
            </div>
          )}
          <div className="flex items-center gap-3">
            <span className="cc-bob shrink-0">
              <Marble color={color} size={84} />
            </span>
            <div className="min-w-0">
              <div className="cc-soft text-xs font-bold uppercase tracking-wide">Your marbles</div>
              <div className="text-2xl font-black leading-tight">{SHIRT_COLORS[color].label}</div>
              {underWay && !you?.joined && (
                <p className="mt-1 text-xs">A round is under way. You'll watch it, then play in the next one.</p>
              )}
            </div>
          </div>

          <h2 className="mb-2 mt-4 text-sm font-extrabold">Pick your color</h2>
          <div className="grid grid-cols-4 gap-2" role="radiogroup" aria-label="Your color">
            {SHIRT_COLOR_KEYS.map((c) => {
              const owner = takenBy.get(c);
              return (
                <button
                  key={c}
                  type="button"
                  role="radio"
                  aria-checked={color === c}
                  disabled={!!owner}
                  onClick={() => setPicked(c)}
                  aria-label={owner ? `${SHIRT_COLORS[c].label}, taken by ${owner}` : SHIRT_COLORS[c].label}
                  className={cn(
                    "flex min-w-0 flex-col items-center rounded-2xl border-2 bg-white px-1 pb-1.5 pt-2 transition",
                    color === c ? "border-[#c9962a] shadow-md" : "border-transparent",
                    owner && "opacity-40"
                  )}
                >
                  <Marble color={c} size={44} />
                  <span className="mt-0.5 w-full truncate text-center text-xs font-bold">
                    {owner ?? SHIRT_COLORS[c].label}
                  </span>
                </button>
              );
            })}
          </div>

          {problem && (
            <p role="alert" className="mt-4 rounded-xl bg-[#fde3e0] px-3 py-2 text-sm font-bold text-[#a32116]">
              {problem}
            </p>
          )}
          <button
            type="button"
            className={cn("cc-btn cc-btn-primary w-full text-lg", problem ? "mt-3" : "mt-5")}
            disabled={!ready || needsName}
            onClick={() => onJoin("marble", color, guestName)}
          >
            {!ready
              ? you
                ? "Connecting…"
                : "Joining…"
              : needsName
                ? "Type your name to join"
                : you?.joined
                  ? "Use this color"
                  : "Join game"}
          </button>
          {onCancel && (
            <button type="button" className="mt-3 w-full text-sm font-semibold underline" onClick={onCancel}>
              Never mind
            </button>
          )}
        </section>

        {others.length > 0 && (
          <section className="cc-card px-4 py-3">
            <h2 className="mb-1 text-sm font-extrabold">Who's playing</h2>
            <ul className="divide-y divide-[rgba(31,45,42,0.1)]">
              {others.map((p) => (
                <li key={p.player_id} className="flex items-center gap-2 py-1.5">
                  <PlayerMarble player={p} size={30} />
                  <span className="min-w-0 flex-1 truncate font-semibold">{p.name}</span>
                  <span className="cc-soft text-xs">{p.joined ? (p.is_host ? "Started the game" : "In") : "Invited"}</span>
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
// The game: who's playing, the board, the move being built
// ---------------------------------------------------------------------------

interface CheckersGameProps {
  game: GameView;
  online: boolean;
  notice: string | null;
  act: (action: GameAction) => void;
  flash: (message: string) => void;
  onChangeColor: () => void;
}

export function CheckersGame({ game, online, notice, act, flash, onChangeColor }: CheckersGameProps) {
  const board = game.checkers;
  const state = board?.state ?? null;
  const holeCount = board?.holes.length ?? 0;
  // The board never changes, though every update carries it again.
  const geometry = useMemo(() => (board ? buildGeometry(board.holes, board.points) : null), [holeCount]);

  const myId = game.you.player_id;
  const byId = new Map(game.players.map((p) => [p.player_id, p]));
  const colors = useMemo(() => {
    const found = new Map<number, ShirtColor>();
    for (const p of game.players) if (p.avatar_color) found.set(p.player_id, p.avatar_color);
    return found;
  }, [game.players]);
  const me = byId.get(myId);
  const myShade = SHIRT_COLORS[me?.avatar_color ?? "gold"];

  const playing = game.status === "playing";
  const roundOver = game.status === "round_over";
  const order = state?.turn_order ?? [];
  const seat = order.indexOf(myId);
  const inRound = seat >= 0;
  const target = state && inRound ? opposite(state.homes[seat]) : -1;
  const turnPlayer = state?.turn_player_id != null ? byId.get(state.turn_player_id) : undefined;
  const myTurn = playing && state?.turn_player_id === myId;
  const winner = state?.winner_player_id != null ? byId.get(state.winner_player_id) : undefined;
  const iWon = winner?.player_id === myId;

  // A move is built for one position on the board; when the board
  // moves on, what was being built is dropped with it.
  const stamp = state ? `${game.status}:${state.round}:${state.moves}` : "";
  const [draft, setDraft] = useState<{ stamp: string; holes: number[] }>({ stamp: "", holes: [] });
  const path = draft.stamp === stamp ? draft.holes : [];
  const setPath = (holes: number[]) => setDraft({ stamp, holes });

  // Sent, and waiting to hear back.
  const [sent, setSent] = useState<string | null>(null);
  const sending = sent !== null && sent === stamp;
  useEffect(() => {
    if (notice) setSent(null);
  }, [notice]);
  useEffect(() => {
    if (sent === null) return;
    const timer = window.setTimeout(() => setSent(null), 3000);
    return () => window.clearTimeout(timer);
  }, [sent]);

  // A move is being played back on the board.
  const [busy, setBusy] = useState(false);
  const canPlay = myTurn && online && !busy && !sending && !!geometry && !!state;

  const marbles = state?.marbles;
  const movable = useMemo(
    () => (canPlay && geometry && marbles ? movableMarbles(geometry, marbles, myId, target) : []),
    [canPlay, geometry, marbles, myId, target]
  );
  const options = useMemo(
    () => (canPlay && geometry && marbles && path.length > 0 ? nextHoles(geometry, marbles, path, target) : []),
    [canPlay, geometry, marbles, path, target]
  );
  const stepped = !!geometry && isStep(geometry, path);
  const hops = stepped ? 0 : path.length - 1;

  const onTap = (hole: number | null) => {
    if (!canPlay) return;
    if (hole === null) {
      // A tap on nothing puts a marble back down, but never throws away hops.
      if (path.length === 1) setPath([]);
      return;
    }
    if (options.includes(hole)) {
      // After a step the other holes offered are instead of it, not after it.
      setPath(stepped ? [path[0], hole] : [...path, hole]);
      return;
    }
    const earlier = path.indexOf(hole);
    if (earlier >= 0) {
      // Back to a hole it has been in; tapping the marble before it has moved puts it down.
      setPath(path.length === 1 ? [] : path.slice(0, earlier + 1));
      return;
    }
    if (movable.includes(hole)) setPath([hole]);
  };

  const confirm = () => {
    if (!canPlay || path.length < 2) return;
    act({ type: "move", path });
    setSent(stamp);
  };

  const round = state?.round;
  useEffect(() => {
    if (myTurn) navigator.vibrate?.(35);
  }, [myTurn, round]);

  // Let the last marble land before the congratulations cover the board.
  const [celebrate, setCelebrate] = useState(false);
  const [peek, setPeek] = useState(false);
  useEffect(() => {
    setPeek(false);
  }, [roundOver, round]);
  useEffect(() => {
    if (!roundOver) {
      setCelebrate(false);
      return;
    }
    if (busy) return;
    const timer = window.setTimeout(() => setCelebrate(true), 900);
    return () => window.clearTimeout(timer);
  }, [roundOver, busy, round]);

  const [confirmEnd, setConfirmEnd] = useState(false);

  // This round's players in turn order, then anyone waiting for the next.
  const joined = game.players.filter((p) => p.joined);
  const seats = [
    ...order.map((id) => byId.get(id)).filter((p): p is GamePlayer => !!p),
    ...joined.filter((p) => !order.includes(p.player_id)),
  ];
  const homeCount = (p: GamePlayer): number | null => {
    const at = order.indexOf(p.player_id);
    if (!state || !geometry || at < 0) return null;
    return marblesHome(geometry, state.marbles, p.player_id, opposite(state.homes[at]));
  };

  // The host can bring someone else in at any point; a latecomer
  // watches the round that is going and plays in the next.
  const inviteUrl = game.is_full ? null : game.invite_url;
  const copyInvite = async () => {
    if (!inviteUrl) return;
    flash((await copyLink(inviteUrl)) ? "Invite link copied — send it to anyone you want in." : "Couldn't copy the link.");
  };

  if (!board || !geometry) {
    return <div className="checkers-theme cc-screen flex items-center justify-center font-semibold">Setting up the board…</div>;
  }

  // ---- The bar under the board: what to do now, and the buttons to do it ---

  const mover = state?.last_move ? byId.get(state.last_move.player_id) : undefined;
  let who: GamePlayer | undefined = turnPlayer;
  let title: string;
  let hint: string | null = null;
  let buttons: ReactNode = null;
  if (!online) {
    who = undefined;
    title = "Reconnecting…";
  } else if (roundOver) {
    who = winner;
    title = winner ? (iWon ? "You win!" : `${winner.name} wins!`) : "It's a draw";
    hint = winner ? "First to fill the far point" : "Nobody has a move left";
    if (peek) {
      // Two buttons leave little room on a narrow phone: the name gets both lines.
      hint = null;
      buttons = (
        <>
          <button type="button" className="cc-btn cc-btn-quiet" onClick={() => setPeek(false)}>
            Scores
          </button>
          <button type="button" className="cc-btn cc-btn-primary" onClick={() => act({ type: "play_again" })}>
            <RotateCcw className="h-5 w-5" /> Again
          </button>
        </>
      );
    }
  } else if (!playing) {
    who = undefined;
    title = game.can_start ? "Everyone ready?" : "Waiting for players";
  } else if (!inRound) {
    title = "Watching this round";
    hint = `${turnPlayer?.name ?? "Someone"}'s turn. You'll play in the next one.`;
  } else if (!myTurn) {
    title = `${turnPlayer?.name ?? "Someone"}'s turn`;
    hint = turnPlayer && !turnPlayer.connected ? "They've stepped away" : busy ? null : "Waiting for their move";
  } else if (sending) {
    title = "Sending your move…";
  } else if (busy) {
    title = "Your turn";
    hint = mover && mover.player_id !== myId ? `${mover.name} just moved` : null;
  } else if (path.length === 0) {
    title = "Your turn";
    hint = state && state.moves < order.length ? "Race them to the far point. Tap a marble to move it." : "Tap one of your marbles";
  } else if (path.length === 1) {
    title = "Where to?";
    hint = "Tap a glowing hole: one step, or a hop over a marble";
  } else {
    who = undefined;
    if (stepped) {
      title = "One step";
      hint = "Confirm, or tap another hole";
    } else {
      title = hops === 1 ? "1 hop" : `${hops} hops`;
      hint = options.length > 0 ? "Hop again, or confirm" : "That's as far as it goes";
    }
    buttons = (
      <>
        <button
          type="button"
          className="cc-btn cc-btn-quiet"
          aria-label="Undo"
          onClick={() => setPath(path.slice(0, -1))}
        >
          <Undo2 className="h-5 w-5" />
        </button>
        <button type="button" className="cc-btn cc-btn-primary" onClick={confirm}>
          <Check className="h-5 w-5" strokeWidth={3} /> Confirm
        </button>
      </>
    );
  }
  const mineNow = online && playing && myTurn;

  const columns = seats.length <= 3 ? seats.length : seats.length === 4 ? 2 : 3;
  return (
    <div className="checkers-theme cc-screen cc-table">
      <Notice message={notice} />

      <header className="cc-table-head flex items-center gap-2 px-3 pt-2">
        <h1 className="cc-title min-w-0 flex-1 truncate text-lg leading-tight">{game.game_name}</h1>
        {state && <span className="cc-pill">Round {state.round}</span>}
        {inviteUrl && game.status !== "lobby" && (
          <button
            type="button"
            onClick={() => void copyInvite()}
            aria-label="Copy the invite link"
            title="Copy the invite link"
            className="cc-pill"
          >
            <UserPlus className="h-4 w-4" />
          </button>
        )}
        <button type="button" onClick={() => setConfirmEnd(true)} className="cc-pill">
          <Flag className="h-3.5 w-3.5" /> End
        </button>
      </header>

      {seats.length > 0 && (
        <div className="cc-seats px-2 pt-2" style={{ "--cc-columns": Math.max(1, columns) } as CSSProperties}>
          {seats.map((p) => {
            const isTurn = playing && p.player_id === turnPlayer?.player_id;
            const home = homeCount(p);
            return (
              <div
                key={p.player_id}
                className={cn("cc-seat", isTurn && "cc-seat-turn", !p.connected && "cc-seat-away")}
                style={
                  isTurn && p.avatar_color ? ({ "--cc-seat": SHIRT_COLORS[p.avatar_color].main } as CSSProperties) : undefined
                }
                aria-current={isTurn ? "true" : undefined}
              >
                <PlayerMarble player={p} size={22} className="shrink-0" />
                <span className="min-w-0 flex-1 truncate text-[13px] font-extrabold">
                  {p.player_id === myId ? "You" : p.name}
                </span>
                {home !== null ? (
                  <span className="shrink-0 text-xs font-bold tabular-nums" title="Marbles in the far point">
                    {home}/{board.pieces}
                  </span>
                ) : (
                  p.wins > 0 && (
                    <span className="flex shrink-0 items-center gap-0.5 text-xs font-bold tabular-nums">
                      <Trophy className="h-3 w-3 text-[#f4c95d]" /> {p.wins}
                    </span>
                  )
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="cc-table-board relative min-h-0 min-w-0 px-1 py-1">
        <CheckersBoard
          geometry={geometry}
          state={state}
          colors={colors}
          viewer={myId}
          path={path}
          options={options}
          movable={movable}
          onTap={onTap}
          onBusy={setBusy}
        />

        {game.status === "lobby" && (
          <Overlay>
            <Lobby game={game} act={act} flash={flash} onChangeColor={onChangeColor} />
          </Overlay>
        )}

        {roundOver && celebrate && !peek && (
          <Overlay>
            <div className="cc-card cc-rise w-full max-w-sm p-5 text-center">
              {winner ? (
                <>
                  <span className="cc-bob inline-block">
                    <PlayerMarble player={winner} size={96} />
                  </span>
                  <h2 className="cc-title text-4xl leading-none">{iWon ? "You win!" : `${winner.name} wins!`}</h2>
                  <p className="mt-1 text-sm font-semibold">First to fill the far point.</p>
                </>
              ) : (
                <>
                  <h2 className="cc-title text-4xl leading-none">It's a draw!</h2>
                  <p className="mt-1 text-sm font-semibold">Nobody has a move left.</p>
                </>
              )}
              <Scores players={joined} highlight={winner?.player_id} />
              <div className="mt-4 flex flex-col gap-2">
                <button type="button" className="cc-btn cc-btn-primary text-lg" onClick={() => act({ type: "play_again" })}>
                  <RotateCcw className="h-5 w-5" /> Play again
                </button>
                <div className="flex gap-2">
                  <button type="button" className="cc-btn cc-btn-quiet flex-1 text-sm" onClick={() => setPeek(true)}>
                    <Eye className="h-4 w-4" /> See board
                  </button>
                  <button type="button" className="cc-btn cc-btn-quiet flex-1 text-sm" onClick={() => setConfirmEnd(true)}>
                    <Flag className="h-4 w-4" /> End game
                  </button>
                </div>
              </div>
            </div>
          </Overlay>
        )}
      </div>

      <div
        role="status"
        aria-live="polite"
        className={cn("cc-bar cc-table-bar", mineNow && "cc-bar-mine")}
        style={mineNow ? ({ "--cc-seat": myShade.main } as CSSProperties) : undefined}
      >
        {!online && <WifiOff className="h-6 w-6 shrink-0" />}
        {who && <PlayerMarble player={who} size={36} className="shrink-0" />}
        <div className="min-w-0 flex-1 leading-tight">
          <div className={cn("text-lg font-black", hint ? "truncate" : "line-clamp-2")}>{title}</div>
          {hint && <div className="cc-soft text-xs font-semibold">{hint}</div>}
        </div>
        {buttons && <div className="flex shrink-0 items-center gap-2">{buttons}</div>}
      </div>

      {confirmEnd && (
        <div className="cc-overlay fixed inset-0 z-40 flex items-center justify-center p-5">
          <div className="cc-card cc-rise w-full max-w-xs p-5 text-center">
            <h2 className="text-xl font-black">End the game for everyone?</h2>
            <p className="mt-1 text-sm">Wins so far stay in the family hall of fame.</p>
            <div className="mt-4 flex flex-col gap-2">
              <button
                type="button"
                className="cc-btn cc-btn-primary"
                onClick={() => {
                  setConfirmEnd(false);
                  act({ type: "end" });
                }}
              >
                End game
              </button>
              <button type="button" className="cc-btn cc-btn-quiet" onClick={() => setConfirmEnd(false)}>
                Keep playing
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Overlay({ children }: { children: ReactNode }) {
  return (
    <div className="cc-overlay absolute inset-0 z-10 flex items-center justify-center overflow-y-auto p-3">
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
            p.player_id === highlight ? "bg-[#ffe08a]" : "bg-white/80"
          )}
        >
          <PlayerMarble player={p} size={26} />
          <span className="min-w-0 flex-1 truncate font-bold">{p.name}</span>
          <span className="flex items-center gap-1 font-black tabular-nums">
            <Trophy className="h-4 w-4 text-[#c9962a]" /> {p.wins}
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
  onChangeColor,
}: {
  game: GameView;
  act: (action: GameAction) => void;
  flash: (message: string) => void;
  onChangeColor: () => void;
}) {
  const links = new Map((game.invites ?? []).map((i) => [i.player_id, i]));
  const share = async (playerId: number) => {
    const invite = links.get(playerId);
    if (!invite) return;
    const how = await shareJoinLink(invite.play_url, `${invite.name}, come play ${game.game_name} with me!`);
    if (how === "copied") flash(`Link for ${invite.name} copied — paste it in a message.`);
    if (how === "failed") flash("Couldn't copy the link.");
  };

  // The game's own link: whoever opens it types their name and is in.
  const inviteUrl = game.is_full ? null : game.invite_url;
  const [copied, setCopied] = useState(false);
  const copyInvite = async () => {
    if (!inviteUrl) return;
    if (await copyLink(inviteUrl)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } else {
      flash("Couldn't copy the link.");
    }
  };
  const shareInvite = async () => {
    if (!inviteUrl) return;
    const how = await shareJoinLink(inviteUrl, `Come play ${game.game_name} with me!`);
    if (how === "copied") flash("Invite link copied — paste it in a message.");
    if (how === "failed") flash("Couldn't share the link.");
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

  const inCount = game.players.filter((p) => p.joined).length;
  return (
    <div className="cc-card cc-rise w-full max-w-sm self-center p-4">
      <h2 className="cc-title text-center text-2xl leading-tight">
        {game.can_start ? "Ready to play!" : "Waiting for players"}
      </h2>
      <p className="cc-soft text-center text-xs font-semibold">
        {inCount} of up to {game.max_players} players in
      </p>
      <ul className="mt-3 space-y-1.5">
        {game.players.map((p) => (
          <li key={p.player_id} className="flex items-center gap-2 rounded-xl bg-white/80 px-2 py-1.5">
            <PlayerMarble player={p} size={32} />
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate font-extrabold">
                {p.name}
                {p.player_id === game.you.player_id && <span className="font-semibold"> (you)</span>}
              </div>
              <div className="cc-soft truncate text-xs">
                {p.joined ? (p.connected ? "Ready" : "Ready — stepped away") : waitingText(p)}
              </div>
            </div>
            {!p.joined && links.has(p.player_id) && (
              <button
                type="button"
                onClick={() => void share(p.player_id)}
                className="flex shrink-0 items-center gap-1 rounded-full bg-[#1f2d2a] px-3 py-1.5 text-xs font-bold text-white"
              >
                <Share2 className="h-3.5 w-3.5" /> Share link
              </button>
            )}
          </li>
        ))}
      </ul>

      {inviteUrl && (
        <div className="mt-3 rounded-xl border-2 border-dashed border-[rgba(31,45,42,0.25)] px-3 py-2.5">
          <div className="text-sm font-extrabold">Invite anyone with a link</div>
          <p className="cc-soft text-xs">Whoever opens it types their name, picks a color and is in.</p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => void copyInvite()}
              className="flex flex-1 items-center justify-center gap-1.5 rounded-full bg-[#1f2d2a] px-3 py-2 text-sm font-bold text-white"
            >
              {copied ? <Check className="h-4 w-4" strokeWidth={3} /> : <Copy className="h-4 w-4" />}
              {copied ? "Copied!" : "Copy invite link"}
            </button>
            {canShare() && (
              <button
                type="button"
                onClick={() => void shareInvite()}
                className="flex items-center justify-center gap-1.5 rounded-full border-2 border-[#1f2d2a] px-3 py-2 text-sm font-bold"
              >
                <Share2 className="h-4 w-4" /> Share
              </button>
            )}
          </div>
        </div>
      )}

      {game.can_start ? (
        <button type="button" className="cc-btn cc-btn-primary mt-4 w-full text-lg" onClick={() => act({ type: "start" })}>
          <Play className="h-5 w-5 fill-current" /> Start game
        </button>
      ) : (
        <p className="cc-dots mt-4 text-center text-sm font-semibold">
          The game can start once {game.min_players} players are in
        </p>
      )}
      <button type="button" className="mt-3 w-full text-center text-sm font-semibold underline" onClick={onChangeColor}>
        Change my color
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Game over: final scores
// ---------------------------------------------------------------------------

export function CheckersGameOver({ game }: { game: GameView | GuestGameView }) {
  const joined = game.players.filter((p) => p.joined);
  const best = Math.max(0, ...joined.map((p) => p.wins));
  const champions = best > 0 ? joined.filter((p) => p.wins === best) : [];
  return (
    <div className="checkers-theme min-h-[100dvh]">
      <div className="mx-auto flex min-h-[100dvh] max-w-sm flex-col items-center justify-center gap-3 px-5 py-8 text-center">
        <p className="cc-soft text-sm font-semibold">{game.game_name}</p>
        {champions.length === 1 ? (
          <>
            <span className="cc-bob">
              <PlayerMarble player={champions[0]} size={120} />
            </span>
            <h1 className="cc-title text-4xl leading-none">
              {champions[0].player_id === game.you?.player_id
                ? "You're the champion!"
                : `${champions[0].name} is the champion!`}
            </h1>
          </>
        ) : (
          <>
            <div className="flex justify-center gap-1">
              {(champions.length ? champions : joined).slice(0, 6).map((p) => (
                <PlayerMarble key={p.player_id} player={p} size={56} />
              ))}
            </div>
            <h1 className="cc-title text-4xl leading-none">{champions.length ? "It's a tie!" : "Game over"}</h1>
          </>
        )}
        <p className="text-sm font-semibold">
          {game.rounds_played === 0
            ? "This game ended before a round was finished."
            : `${game.rounds_played} ${game.rounds_played === 1 ? "round" : "rounds"} played. Thanks for playing!`}
        </p>
        {joined.length > 0 && (
          <div className="cc-card w-full p-3">
            <Scores players={joined} highlight={champions.length === 1 ? champions[0].player_id : undefined} />
          </div>
        )}
        <a href="/" className="cc-btn cc-btn-ghost mt-2">
          Open Family Assistant
        </a>
      </div>
    </div>
  );
}
