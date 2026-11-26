import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Check, ChevronDown, ChevronUp, ChevronsLeftRight, Copy, Flag, Play, Share2, Skull, Trophy, WifiOff } from "lucide-react";
import { cn } from "@/lib/cn";
import { SHIRT_COLOR_KEYS, SHIRT_COLORS } from "../Turkey";
import { canShare, copyLink, recallGuestName, shareJoinLink } from "../share";
import type {
  GameAction,
  GamePlayer,
  GameView,
  GuestGameView,
  ShirtColor,
  SurvivorStyle,
  ZombieView,
  ZombieWeapon,
} from "../types";
import type { LiveMessage } from "../useGameSocket";
import { ZombieEngine } from "./engine";
import type { Hud, Phase } from "./engine";
import { WeaponIcon, ZombieIcon } from "./icons";
import { Survivor } from "./Survivor";
import { isSurvivorStyle, SURVIVOR_BY_KEY, SURVIVORS } from "./survivors";
import "./zombies.css";

// Zombie Apocalypse, as a player sees it on /play/<token>:
//
//   welcome  → who invited you, pick a character and a shirt, join
//   setup    → the team, your weapons, how long to hold out, "Start level N"
//   level    → the map (engine.ts) with the clock above it and the
//              controls below; then back to setup for the next one
//
// Everything the thumbs do is under the map, so nothing covers it: the
// left thumb taps a weapon to fire it straight out; the right one
// slides the player along the wall and flips them to the other wall.

export const ZOMBIE_GAME = "zombie_apocalypse";
const GUEST_NAME_MAX_LENGTH = 24;
/** How long the map stays up after a level ends, so the ending can be seen. */
const LINGER_MS = 2400;

/**
 * Holds what the server says about a level until the map is on screen
 * to hear it. The level's first message can arrive a moment before the
 * page has switched to the map.
 */
export class LiveFeed {
  private held: { message: LiveMessage; at: number }[] = [];
  private listener: ((message: LiveMessage, at: number) => void) | null = null;

  push(message: LiveMessage) {
    const at = performance.now();
    if (this.listener) this.listener(message, at);
    else if (message.type === "zs") this.held = [{ message, at }];
    else if (this.held.length > 0 && this.held.length < 600) this.held.push({ message, at });
  }

  attach(listener: (message: LiveMessage, at: number) => void) {
    this.listener = listener;
    const held = this.held;
    this.held = [];
    for (const { message, at } of held) listener(message, at);
  }

  detach() {
    this.listener = null;
  }

  clear() {
    this.held = [];
  }
}

function meOf(game: GameView | GuestGameView): GamePlayer | undefined {
  const mine = game.you?.player_id;
  return game.players.find((p) => p.player_id === mine);
}

function clock(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function minutes(seconds: number): string {
  const count = seconds / 60;
  return `${count} ${count === 1 ? "minute" : "minutes"}`;
}

/** Shots a weapon comes with for a level: `ammo_for` on the server. */
function ammoFor(weapon: ZombieWeapon, duration: number): number | null {
  if (weapon.ammo === null || weapon.given) return weapon.ammo;
  return Math.ceil((weapon.ammo * Math.max(1, duration)) / 120);
}

export function Portrait({
  player,
  size,
  className,
}: {
  player: Pick<GamePlayer, "avatar_style" | "avatar_color" | "name">;
  size: number;
  className?: string;
}) {
  if (!isSurvivorStyle(player.avatar_style) || !player.avatar_color) {
    return (
      <span
        className="flex shrink-0 items-center justify-center rounded-full border-2 border-dashed border-white/25 font-black text-white/40"
        style={{ width: size, height: size, fontSize: size * 0.45 }}
        aria-hidden
      >
        ?
      </span>
    );
  }
  return (
    <Survivor
      style={player.avatar_style}
      color={player.avatar_color}
      size={size}
      className={cn("shrink-0", className)}
      title={player.name}
    />
  );
}

function Notice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="za-rise fixed left-1/2 top-3 z-50 max-w-[92vw] -translate-x-1/2 rounded-full bg-[#c92a2a] px-4 py-2 text-sm font-semibold text-white shadow-lg"
    >
      {message}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Welcome: who invited you, pick a character, join
// ---------------------------------------------------------------------------

interface WelcomeProps {
  /** With `you: null` this is a guest who has yet to say who they are. */
  game: GameView | GuestGameView;
  ready: boolean;
  notice: string | null;
  problem?: string | null;
  /** `name` is what a guest typed; empty for a player who already has one. */
  onJoin: (style: SurvivorStyle, color: ShirtColor, name: string) => void;
  /** Set when a player who is already in is just changing characters. */
  onCancel?: () => void;
}

export function ZombieWelcome({ game, ready, notice, problem, onJoin, onCancel }: WelcomeProps) {
  const me = meOf(game);
  const you = game.you;
  const others = game.players.filter((p) => p.player_id !== you?.player_id);
  const takenBy = new Map<ShirtColor, string>();
  for (const p of others) if (p.joined && p.avatar_color) takenBy.set(p.avatar_color, p.name);
  const firstFree = SHIRT_COLOR_KEYS.find((c) => !takenBy.has(c)) ?? SHIRT_COLOR_KEYS[0];

  const [style, setStyle] = useState<SurvivorStyle>(isSurvivorStyle(me?.avatar_style) ? me.avatar_style : "dad");
  const [picked, setPicked] = useState<ShirtColor | null>(me?.avatar_color ?? null);
  const [name, setName] = useState(() => (you ? "" : recallGuestName().slice(0, GUEST_NAME_MAX_LENGTH)));
  // If someone else takes the colour while this player is choosing,
  // fall back to one that is still free.
  const color = picked && !takenBy.has(picked) ? picked : firstFree;

  const guestName = name.trim();
  const needsName = !you && !guestName;
  const underWay = game.status === "playing";
  return (
    <div className="zombie-theme min-h-[100dvh]">
      <Notice message={notice} />
      <div className="mx-auto flex max-w-md flex-col gap-4 px-4 pb-8 pt-6">
        <header className="text-center">
          <p className="za-soft text-sm font-semibold">
            {you?.is_host ? "You're starting a game of" : `${game.invited_by} needs your help in`}
          </p>
          <h1 className="za-title mt-1 text-4xl leading-none">{game.game_name}</h1>
          <p className="mx-auto mt-3 max-w-xs text-sm">
            {you ? `Hi ${you.name}! ` : ""}Zombies are coming for the house. Hold them off together until the clock
            runs out.
          </p>
        </header>

        <section className="za-card p-4">
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
                placeholder="So everyone knows who's fighting"
                autoComplete="given-name"
                autoCapitalize="words"
                enterKeyHint="done"
                className="za-input"
              />
            </div>
          )}
          <div className="flex items-center gap-3">
            <span className="za-sway shrink-0">
              <Survivor style={style} color={color} size={104} />
            </span>
            <div className="min-w-0">
              <div className="za-soft text-xs font-bold uppercase tracking-wide">Your character</div>
              <div className="text-xl font-black leading-tight">
                {SURVIVOR_BY_KEY[style].label} in {SHIRT_COLORS[color].label.toLowerCase()}
              </div>
              {underWay && !you?.joined && (
                <p className="mt-1 text-xs">A level is being played. You'll watch it, then fight in the next one.</p>
              )}
            </div>
          </div>

          <h2 className="mb-2 mt-4 text-sm font-extrabold">Pick your character</h2>
          <div className="grid grid-cols-4 gap-2">
            {SURVIVORS.map((s) => (
              <button
                key={s.key}
                type="button"
                onClick={() => setStyle(s.key)}
                aria-pressed={style === s.key}
                className="za-pick flex flex-col items-center px-0.5 pb-1.5 pt-1"
              >
                <Survivor style={s.key} color={color} size={62} />
                <span className="text-center text-[11px] font-bold leading-tight">{s.label}</span>
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
                    "relative flex aspect-square items-center justify-center rounded-full border-2 border-white/80 shadow",
                    color === c && "ring-2 ring-[#9ae66e] ring-offset-2 ring-offset-[#131d17]",
                    owner && "opacity-30 shadow-none"
                  )}
                  style={{ backgroundColor: SHIRT_COLORS[c].main }}
                >
                  {color === c && <Check className="h-4 w-4 text-white" strokeWidth={4} />}
                  {owner && <span className="absolute h-0.5 w-[115%] rotate-45 rounded bg-white" />}
                </button>
              );
            })}
          </div>
          {takenBy.size > 0 && (
            <p className="za-soft mt-2 text-xs">
              Taken: {[...takenBy].map(([c, who]) => `${SHIRT_COLORS[c].label.toLowerCase()} (${who})`).join(", ")}
            </p>
          )}

          {problem && (
            <p role="alert" className="mt-4 rounded-xl bg-[#c92a2a]/25 px-3 py-2 text-sm font-bold text-[#ffc9c9]">
              {problem}
            </p>
          )}
          <button
            type="button"
            className={cn("za-btn za-btn-primary w-full text-lg", problem ? "mt-3" : "mt-5")}
            disabled={!ready || needsName}
            onClick={() => onJoin(style, color, guestName)}
          >
            {!ready
              ? you
                ? "Connecting…"
                : "Joining…"
              : needsName
                ? "Type your name to join"
                : you?.joined
                  ? "Use this character"
                  : "Join the fight"}
          </button>
          {onCancel && (
            <button type="button" className="mt-3 w-full text-sm font-semibold underline" onClick={onCancel}>
              Never mind
            </button>
          )}
        </section>

        {others.length > 0 && (
          <section className="za-card px-4 py-3">
            <h2 className="mb-1 text-sm font-extrabold">Who's in</h2>
            <ul className="divide-y divide-white/10">
              {others.map((p) => (
                <li key={p.player_id} className="flex items-center gap-2 py-1.5">
                  <Portrait player={p} size={36} />
                  <span className="min-w-0 flex-1 truncate font-semibold">{p.name}</span>
                  <span className="za-soft text-xs">
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
// The game for someone who has joined: setup between levels, or the level
// ---------------------------------------------------------------------------

interface ZombieGameProps {
  game: GameView;
  online: boolean;
  notice: string | null;
  act: (action: GameAction) => void;
  flash: (message: string) => void;
  /** Sends a message about the level being played; false if it couldn't go. */
  send: (message: LiveMessage) => boolean;
  feed: LiveFeed;
  onChangeCharacter: () => void;
}

export function ZombieGame({ game, online, notice, act, flash, send, feed, onChangeCharacter }: ZombieGameProps) {
  const zombie = game.zombie;
  const playing = game.status === "playing";
  const [confirmEnd, setConfirmEnd] = useState(false);

  // When a level ends the game goes straight back to "between levels".
  // The map stays up a moment longer so the ending is seen; it must not
  // be taken down and put back in between, or the ending would be gone.
  const [onMap, setOnMap] = useState(playing);
  if (playing && !onMap) setOnMap(true);
  const phase = useRef<Phase>("waiting");
  useEffect(() => {
    if (playing || !onMap) return;
    const ended = phase.current === "won" || phase.current === "lost";
    const timer = window.setTimeout(() => setOnMap(false), ended ? LINGER_MS : 0);
    return () => window.clearTimeout(timer);
  }, [playing, onMap]);

  if (!zombie) return null;
  return (
    <>
      <Notice message={notice} />
      {playing || onMap ? (
        <Battle
          game={game}
          zombie={zombie}
          online={online}
          send={send}
          feed={feed}
          onPhase={(next) => (phase.current = next)}
          onEnd={() => setConfirmEnd(true)}
        />
      ) : (
        <Setup
          game={game}
          zombie={zombie}
          online={online}
          act={act}
          flash={flash}
          onChangeCharacter={onChangeCharacter}
          onEnd={() => setConfirmEnd(true)}
        />
      )}
      {confirmEnd && (
        <div className="zombie-theme za-overlay fixed inset-0 z-40 flex items-center justify-center p-5">
          <div className="za-card za-rise w-full max-w-xs p-5 text-center">
            <h2 className="text-xl font-black">End the game for everyone?</h2>
            <p className="za-soft mt-1 text-sm">Levels survived so far stay in the family hall of fame.</p>
            <div className="mt-4 flex flex-col gap-2">
              <button
                type="button"
                className="za-btn za-btn-danger"
                onClick={() => {
                  setConfirmEnd(false);
                  act({ type: "end" });
                }}
              >
                End game
              </button>
              <button type="button" className="za-btn za-btn-quiet" onClick={() => setConfirmEnd(false)}>
                Keep playing
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// The level
// ---------------------------------------------------------------------------

interface BattleProps {
  game: GameView;
  zombie: ZombieView;
  online: boolean;
  send: (message: LiveMessage) => boolean;
  feed: LiveFeed;
  onPhase: (phase: Phase) => void;
  onEnd: () => void;
}

function Battle({ game, zombie, online, send, feed, onPhase, onEnd }: BattleProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const backdropRef = useRef<HTMLCanvasElement>(null);
  const slideRef = useRef<HTMLDivElement>(null);
  const sliding = useRef<number | null>(null);
  const engineRef = useRef<ZombieEngine | null>(null);
  // The weapon whose reload is showing, and the shot that started it.
  const [view, setView] = useState<{ hud: Hud | null; reload: { index: number; shot: number } | null }>({
    hud: null,
    reload: null,
  });
  const meId = game.you.player_id;

  // The rules never change during a game; hold on to the first copy so
  // a fresh one with every update doesn't rebuild the map.
  const rules = useRef(zombie.rules).current;
  const players = useRef(game.players);
  players.current = game.players;
  const report = useRef(onPhase);
  report.current = onPhase;

  useEffect(() => {
    const canvas = canvasRef.current;
    const backdrop = backdropRef.current;
    if (!canvas || !backdrop) return;
    const engine = new ZombieEngine({
      canvas,
      backdrop,
      rules,
      me: meId,
      send,
      onHud: (hud) => {
        report.current(hud.phase);
        setView((before) => {
          const last = before.hud;
          let reload = before.reload;
          if (last && hud.shot !== last.shot) reload = { index: hud.weapon, shot: hud.shot };
          else if (last && hud.weapon !== last.weapon) reload = null;
          return { hud, reload };
        });
      },
      // The knob follows the player sixty times a second: not through React.
      onWhere: (fraction) => slideRef.current?.style.setProperty("--za-at", fraction.toFixed(4)),
    });
    engine.setPlayers(players.current);
    engineRef.current = engine;
    feed.attach((message, at) => engine.receive(message, at));
    return () => {
      feed.detach();
      engine.destroy();
      engineRef.current = null;
    };
  }, [feed, send, rules, meId]);

  useEffect(() => {
    engineRef.current?.setPlayers(game.players);
  }, [game.players]);

  const { hud, reload } = view;
  const weapons = useMemo(() => new Map(rules.weapons.map((w) => [w.key, w])), [rules]);
  const fighters = game.players.filter((p) => hud && p.player_id in hud.kills);
  const teamKills = hud ? Object.values(hud.kills).reduce((sum, n) => sum + n, 0) : 0;
  const over = hud?.phase === "won" || hud?.phase === "lost";
  const hurry = !!hud && hud.phase === "playing" && hud.left <= 10;
  const facingTop = (hud?.side ?? -1) < 0;

  /** The thumb on the slide: where it is along the bar is where the player goes along the wall. */
  const slideTo = (event: React.PointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    // Half the knob (zombies.css), so the knob comes to rest under the thumb.
    const margin = Math.min(26, box.width * 0.18);
    engineRef.current?.steer((event.clientX - box.left - margin) / (box.width - margin * 2));
  };

  return (
    <div className="zombie-theme zombie-screen flex flex-col">
      <header className="flex items-center gap-2 px-2.5 py-1.5">
        <span className="za-chip">Level {hud?.level ?? zombie.level}</span>
        <div
          className={cn(
            "min-w-0 flex-1 text-center text-2xl font-black tabular-nums leading-none",
            hurry && "text-[color:var(--za-amber)]"
          )}
          role="timer"
          aria-label="Time left"
        >
          {hud ? clock(hud.left) : clock(zombie.duration)}
        </div>
        <span className="za-chip" title="Zombies killed this level" aria-label={`${teamKills} zombies killed this level`}>
          <Skull className="h-3.5 w-3.5" /> {teamKills}
        </span>
        <button type="button" onClick={onEnd} className="za-chip" aria-label="End the game">
          <Flag className="h-3.5 w-3.5" /> End
        </button>
      </header>

      {fighters.length > 1 && (
        <div className="flex gap-1.5 px-2 pb-1.5">
          {fighters.map((p) => (
            <div
              key={p.player_id}
              className={cn(
                "flex min-w-0 flex-1 items-center gap-1 rounded-full bg-white/[0.07] py-0.5 pl-0.5 pr-2",
                p.player_id === meId && "ring-1 ring-inset ring-[color:var(--za-green)]",
                !p.connected && "opacity-45"
              )}
              title={p.name}
            >
              <Portrait player={p} size={24} />
              <span className="min-w-0 flex-1 truncate text-xs font-bold">
                {p.player_id === meId ? "You" : p.name}
              </span>
              <span className="text-xs font-black tabular-nums">{hud?.kills[p.player_id] ?? 0}</span>
            </div>
          ))}
        </div>
      )}

      <div className="za-stage">
        <canvas ref={backdropRef} aria-hidden />
        <canvas ref={canvasRef} aria-label="The house and the grounds around it" />
        {!online && (
          <div className="absolute left-1/2 top-2 z-20 flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-[#ffd43b] px-3 py-1 text-xs font-extrabold text-[#3d2f00]">
            <WifiOff className="h-3.5 w-3.5" /> Reconnecting…
          </div>
        )}
        {over && hud && (
          <div className={cn("za-outcome", hud.phase === "won" ? "za-won" : "za-lost")} role="status">
            <h2>{hud.phase === "won" ? "You survived!" : "The house fell!"}</h2>
            <p className="text-base font-bold">
              {hud.phase === "won"
                ? `Level ${hud.level} held with ${hud.house} of ${hud.houseMax} left`
                : `The zombies broke through on level ${hud.level}`}
            </p>
          </div>
        )}
      </div>

      {hud && !hud.fighting && hud.phase !== "waiting" ? (
        <div className="px-4 py-5 text-center text-sm font-semibold">
          You're watching this level. You'll be in the next one.
        </div>
      ) : !hud?.weapons.length ? (
        <div className="za-controls">
          <div className="za-soft za-dots col-span-3 flex items-center justify-center text-sm font-semibold">
            Loading the level
          </div>
        </div>
      ) : (
        <div className="za-controls">
          <div className="za-weapons" role="group" aria-label="Weapons: tap one to fire it">
            {hud.weapons.map((key, index) => {
              const weapon = weapons.get(key);
              const left = hud.ammo[index] ?? null;
              const inHand = hud.weapon === index;
              const empty = left !== null && left <= 0;
              return (
                <button
                  key={key}
                  type="button"
                  className={cn("za-weapon", inHand && "za-weapon-held", empty && "za-weapon-empty")}
                  aria-pressed={inHand}
                  aria-label={`${weapon?.name ?? key}, ${left === null ? "never runs out" : `${left} left`}`}
                  onPointerDown={(event) => {
                    event.preventDefault();
                    try {
                      event.currentTarget.setPointerCapture(event.pointerId);
                    } catch {
                      /* the pointer is already gone */
                    }
                    engineRef.current?.press(index);
                  }}
                  onPointerUp={() => engineRef.current?.release(index)}
                  onPointerCancel={() => engineRef.current?.release(index)}
                  onContextMenu={(event) => event.preventDefault()}
                >
                  {inHand && reload?.index === index && weapon && weapon.reload >= 0.25 && !empty && (
                    <span key={reload.shot} className="za-reload" style={{ animationDuration: `${weapon.reload}s` }} />
                  )}
                  <span className="za-weapon-key">{index + 1}</span>
                  <span className="za-weapon-ammo">{left === null ? "∞" : left}</span>
                  <WeaponIcon weapon={key} />
                  <span className="za-weapon-name">{BUTTON_NAMES[key] ?? weapon?.name ?? key}</span>
                </button>
              );
            })}
          </div>

          <div
            ref={slideRef}
            className="za-slide"
            role="slider"
            aria-label="Slide to move along the wall"
            aria-orientation="horizontal"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={50}
            onPointerDown={(event) => {
              event.preventDefault();
              if (sliding.current !== null) return;
              sliding.current = event.pointerId;
              try {
                event.currentTarget.setPointerCapture(event.pointerId);
              } catch {
                /* the pointer is already gone */
              }
              slideTo(event);
            }}
            onPointerMove={(event) => {
              if (event.pointerId === sliding.current) slideTo(event);
            }}
            onPointerUp={(event) => {
              if (event.pointerId === sliding.current) sliding.current = null;
            }}
            onPointerCancel={(event) => {
              if (event.pointerId === sliding.current) sliding.current = null;
            }}
            onContextMenu={(event) => event.preventDefault()}
          >
            <span className="za-slide-rail" />
            <span className="za-slide-knob">
              <ChevronsLeftRight className="h-6 w-6" strokeWidth={3} />
            </span>
            <span className="za-slide-hint">Slide to move</span>
          </div>

          <button
            type="button"
            className="za-flip"
            aria-label={facingTop ? "Facing the top wall. Tap to face the bottom." : "Facing the bottom wall. Tap to face the top."}
            onPointerDown={(event) => {
              event.preventDefault();
              engineRef.current?.flip();
            }}
            onContextMenu={(event) => event.preventDefault()}
          >
            <span className={cn("za-flip-way", facingTop && "za-flip-on")}>
              <ChevronUp className="h-7 w-7" strokeWidth={3.5} />
            </span>
            <span className="za-flip-label">{facingTop ? "Top" : "Bottom"}</span>
            <span className={cn("za-flip-way", !facingTop && "za-flip-on")}>
              <ChevronDown className="h-7 w-7" strokeWidth={3.5} />
            </span>
          </button>
        </div>
      )}
    </div>
  );
}

/** Names too long for one line of a weapon's button, with the place to break them. */
const BUTTON_NAMES: Record<string, string> = { flamethrower: "Flame\u00adthrower" };

// ---------------------------------------------------------------------------
// Between levels: the team, weapons, how long, start
// ---------------------------------------------------------------------------

interface SetupProps {
  game: GameView;
  zombie: ZombieView;
  online: boolean;
  act: (action: GameAction) => void;
  flash: (message: string) => void;
  onChangeCharacter: () => void;
  onEnd: () => void;
}

const KIND_LABEL: Record<string, string> = {
  shot: "One zombie a shot",
  cone: "Sprays a crowd",
  lob: "Blast",
  roll: "Rolls through",
};

function Setup({ game, zombie, online, act, flash, onChangeCharacter, onEnd }: SetupProps) {
  const meId = game.you.player_id;
  const rules = zombie.rules;
  const weapons = useMemo(() => new Map(rules.weapons.map((w) => [w.key, w])), [rules]);
  const choices = rules.weapons.filter((w) => !w.given && w.key !== rules.default_weapon);
  const given = rules.weapons.filter((w) => w.given);
  const joined = game.players.filter((p) => p.joined);
  const waiting = game.players.filter((p) => !p.joined);
  const last = zombie.last;

  // What this player has picked. The server's word follows a moment
  // after each tap; don't let an older answer undo a newer tap.
  const mine = zombie.loadouts[String(meId)] ?? rules.default_loadout;
  const [picks, setPicks] = useState<string[]>(mine);
  const touched = useRef(0);
  const mineKey = mine.join(",");
  useEffect(() => {
    if (Date.now() - touched.current > 1500) setPicks(mineKey.split(","));
  }, [mineKey]);

  const pick = (key: string) => {
    if (picks.includes(key) || !online) return;
    // Taking a third puts down whichever was picked longest ago.
    const next = [...picks.slice(1), key];
    touched.current = Date.now();
    setPicks(next);
    act({ type: "loadout", weapons: next });
  };

  const links = new Map((game.invites ?? []).map((i) => [i.player_id, i]));
  const share = async (playerId: number) => {
    const invite = links.get(playerId);
    if (!invite) return;
    const how = await shareJoinLink(invite.play_url, `${invite.name}, the zombies are coming — help me hold the house!`);
    if (how === "copied") flash(`Link for ${invite.name} copied — paste it in a message.`);
    if (how === "failed") flash("Couldn't copy the link.");
  };
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
    const how = await shareJoinLink(inviteUrl, `Come help me hold off the zombies in ${game.game_name}!`);
    if (how === "copied") flash("Invite link copied — paste it in a message.");
    if (how === "failed") flash("Couldn't share the link.");
  };

  const startLabel = !last
    ? `Start level ${zombie.level}`
    : last.won
      ? `On to level ${zombie.level}`
      : `Try level ${zombie.level} again`;

  return (
    <div className="zombie-theme min-h-[100dvh]">
      <div className="mx-auto flex max-w-md flex-col gap-3 px-3 pb-28 pt-4">
        <header className="flex items-center gap-2">
          <h1 className="za-title min-w-0 flex-1 text-xl leading-tight">{game.game_name}</h1>
          {zombie.best_level > 0 && (
            <span
              className="za-chip"
              title="The highest level survived in this game"
              aria-label={`Best so far: level ${zombie.best_level}`}
            >
              <Trophy className="h-3.5 w-3.5 text-[color:var(--za-amber)]" /> Level {zombie.best_level}
            </span>
          )}
          <button type="button" onClick={onEnd} className="za-chip">
            <Flag className="h-3.5 w-3.5" /> End
          </button>
        </header>

        {!online && (
          <div className="flex items-center justify-center gap-2 rounded-full bg-[#ffd43b] px-3 py-1.5 text-sm font-extrabold text-[#3d2f00]">
            <WifiOff className="h-4 w-4" /> Reconnecting…
          </div>
        )}

        {last && (
          <section className="za-card za-rise p-4 text-center">
            <h2 className={cn("text-2xl font-black leading-tight", last.won ? "text-[color:var(--za-green)]" : "text-[#ff8787]")}>
              {last.won ? `Level ${last.level} survived!` : `The house fell on level ${last.level}`}
            </h2>
            <p className="za-soft mt-1 text-sm font-semibold">
              {last.won
                ? `The house held with ${last.house} of ${rules.house.hp} left.`
                : `It stood for ${clock(last.seconds)} of ${clock(last.duration)}.`}
            </p>
            <ul className="mx-auto mt-3 max-w-[17rem] space-y-1 text-left">
              {joined
                .filter((p) => String(p.player_id) in last.kills)
                .sort((a, b) => (last.kills[String(b.player_id)] ?? 0) - (last.kills[String(a.player_id)] ?? 0))
                .map((p) => (
                  <li key={p.player_id} className="flex items-center gap-2 rounded-xl bg-white/[0.07] px-2 py-1">
                    <Portrait player={p} size={30} />
                    <span className="min-w-0 flex-1 truncate font-bold">
                      {p.player_id === meId ? "You" : p.name}
                    </span>
                    <span className="flex items-center gap-1 font-black tabular-nums">
                      <Skull className="h-4 w-4 text-[color:var(--za-green)]" /> {last.kills[String(p.player_id)] ?? 0}
                    </span>
                  </li>
                ))}
            </ul>
          </section>
        )}

        <section className="za-card p-4">
          <div className="flex items-baseline justify-between gap-2">
            <h2 className="text-lg font-black">Level {zombie.level}</h2>
            <span className="za-soft text-xs font-bold">Keep the house standing for {minutes(zombie.duration)}</span>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="za-soft text-xs font-bold uppercase tracking-wide">Coming</span>
            {zombie.coming.map((key) => (
              <span key={key} className="flex items-center gap-1 text-sm font-bold">
                <ZombieIcon kind={key} size={30} />
                {rules.zombies.find((z) => z.key === key)?.name ?? key}
              </span>
            ))}
          </div>
          {game.you.is_host && (
            <div className="mt-3 flex items-center gap-1.5" role="radiogroup" aria-label="How long a level lasts">
              {zombie.durations.map((seconds) => (
                <button
                  key={seconds}
                  type="button"
                  role="radio"
                  aria-checked={zombie.duration === seconds}
                  disabled={!online}
                  onClick={() => act({ type: "settings", duration: seconds })}
                  className="za-pick flex-1 px-1 py-1.5 text-sm font-extrabold"
                >
                  {seconds / 60} min
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="za-card p-4">
          <h2 className="text-lg font-black">Your weapons</h2>
          <p className="za-soft text-xs font-semibold">
            Everyone has the crossbow and two boulders. Pick {rules.loadout_picks} more — you can change them before
            every level.
          </p>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <WeaponCard weapon={weapons.get(rules.default_weapon)} duration={zombie.duration} note="Always with you" />
            {given.map((w) => (
              <WeaponCard key={w.key} weapon={w} duration={zombie.duration} note="Always with you" />
            ))}
            {choices.map((w) => (
              <WeaponCard
                key={w.key}
                weapon={w}
                duration={zombie.duration}
                picked={picks.includes(w.key)}
                onPick={() => pick(w.key)}
              />
            ))}
          </div>
        </section>

        <section className="za-card p-4">
          <h2 className="text-lg font-black">The team</h2>
          <ul className="mt-2 space-y-1.5">
            {joined.map((p) => (
              <li key={p.player_id} className="flex items-center gap-2 rounded-xl bg-white/[0.07] px-2 py-1.5">
                <Portrait player={p} size={40} />
                <div className="min-w-0 flex-1 leading-tight">
                  <div className="truncate font-extrabold">
                    {p.name}
                    {p.player_id === meId && <span className="font-semibold"> (you)</span>}
                  </div>
                  <div className="za-soft truncate text-xs">
                    {p.connected ? "Ready" : "Stepped away"}
                    {(zombie.kills[String(p.player_id)] ?? 0) > 0 && ` · ${zombie.kills[String(p.player_id)]} zombies so far`}
                  </div>
                </div>
                <div className="flex shrink-0 items-center">
                  {(p.player_id === meId ? picks : zombie.loadouts[String(p.player_id)] ?? []).map((key) => (
                    <WeaponIcon key={key} weapon={key} className="h-6 w-9" />
                  ))}
                </div>
              </li>
            ))}
            {waiting.map((p) => (
              <li key={p.player_id} className="flex items-center gap-2 rounded-xl bg-white/[0.04] px-2 py-1.5">
                <Portrait player={p} size={40} />
                <div className="min-w-0 flex-1 leading-tight">
                  <div className="truncate font-extrabold">{p.name}</div>
                  <div className="za-soft truncate text-xs">
                    {p.invite_status === "sent" ? "Invited" : p.invite_status === "pending" ? "Sending the invite…" : "Not here yet"}
                  </div>
                </div>
                {links.has(p.player_id) && (
                  <button
                    type="button"
                    onClick={() => void share(p.player_id)}
                    className="za-chip shrink-0 bg-[color:var(--za-green)] text-[#0d1a10]"
                  >
                    <Share2 className="h-3.5 w-3.5" /> Share link
                  </button>
                )}
              </li>
            ))}
          </ul>

          {inviteUrl && (
            <div className="mt-3 rounded-xl border-2 border-dashed border-white/20 px-3 py-2.5">
              <div className="text-sm font-extrabold">Bring in help with a link</div>
              <p className="za-soft text-xs">
                Up to {game.max_players} can defend the house. Whoever opens the link types their name, picks a
                character and is in.
              </p>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  onClick={() => void copyInvite()}
                  className="za-btn za-btn-quiet flex-1 px-3 py-2 text-sm"
                >
                  {copied ? <Check className="h-4 w-4" strokeWidth={3} /> : <Copy className="h-4 w-4" />}
                  {copied ? "Copied!" : "Copy invite link"}
                </button>
                {canShare() && (
                  <button type="button" onClick={() => void shareInvite()} className="za-btn za-btn-quiet px-3 py-2 text-sm">
                    <Share2 className="h-4 w-4" /> Share
                  </button>
                )}
              </div>
            </div>
          )}
        </section>

        <section className="za-card px-4 py-3 text-sm">
          <h2 className="font-black">How to play</h2>
          <ul className="za-soft mt-1 list-disc space-y-0.5 pl-5 font-semibold">
            <li>Slide a thumb across the map to move along the house. Touch above the house to defend the top wall, below it for the bottom.</li>
            <li>Tap a weapon to fire it straight ahead. Hold the fast ones down.</li>
            <li>The team wins the level if the house is still standing when the clock runs out.</li>
          </ul>
        </section>

        {game.status === "lobby" && (
          <button type="button" className="text-center text-sm font-semibold underline" onClick={onChangeCharacter}>
            Change my character
          </button>
        )}
      </div>

      <div className="fixed inset-x-0 bottom-0 z-20 bg-gradient-to-t from-[#0e1712] via-[#0e1712]/95 to-transparent px-4 pb-[calc(0.9rem+env(safe-area-inset-bottom))] pt-6">
        <button
          type="button"
          className="za-btn za-btn-primary mx-auto flex w-full max-w-md text-lg"
          disabled={!game.can_start || !online}
          onClick={() => act({ type: last ? "play_again" : "start" })}
        >
          <Play className="h-5 w-5 fill-current" /> {startLabel}
        </button>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="za-soft w-11 shrink-0 text-[10px] font-bold uppercase leading-none">{label}</span>
      <span className="za-bar flex-1">
        <span style={{ width: `${Math.round(Math.max(0.04, Math.min(1, value)) * 100)}%` }} />
      </span>
    </div>
  );
}

function WeaponCard({
  weapon,
  duration,
  note,
  picked,
  onPick,
}: {
  weapon: ZombieWeapon | undefined;
  duration: number;
  /** For a weapon that isn't a choice: why it's there. */
  note?: string;
  picked?: boolean;
  onPick?: () => void;
}) {
  if (!weapon) return null;
  const shots = ammoFor(weapon, duration);
  const power = weapon.freeze ? 0 : Math.log(1 + Math.min(weapon.damage, 400)) / Math.log(401);
  const reach = weapon.range / 340;
  const rate = 1 - Math.log(weapon.reload / 0.08) / Math.log(1.5 / 0.08);
  const body: ReactNode = (
    <>
      <div className="flex items-center gap-2">
        <WeaponIcon weapon={weapon.key} className="h-8 w-12 shrink-0" />
        <div className="min-w-0 flex-1 leading-tight">
          <div className="text-sm font-black">{weapon.name}</div>
          <div className="za-soft text-[11px] font-bold">
            {shots === null ? "Never runs out" : `${shots} ${shots === 1 ? "shot" : "shots"}`}
          </div>
        </div>
        {picked && <Check className="h-4 w-4 shrink-0 text-[color:var(--za-green)]" strokeWidth={4} />}
      </div>
      <div className="mt-2 space-y-1">
        <Stat label={weapon.freeze ? "Freeze" : "Power"} value={weapon.freeze ? weapon.freeze / 8 : power} />
        <Stat label="Reach" value={reach} />
        <Stat label="Speed" value={rate} />
      </div>
      <p className="za-soft mt-1.5 text-[11px] font-semibold leading-snug">
        <span className="text-[color:var(--za-ink)]">{note ?? KIND_LABEL[weapon.kind]}.</span> {weapon.blurb}
      </p>
    </>
  );
  if (!onPick) {
    return <div className="rounded-[14px] border-2 border-dashed border-white/15 p-2 text-left">{body}</div>;
  }
  return (
    <button type="button" aria-pressed={!!picked} onClick={onPick} className="za-pick p-2 text-left">
      {body}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Game over
// ---------------------------------------------------------------------------

export function ZombieGameOver({ game }: { game: GameView | GuestGameView }) {
  const zombie = game.zombie;
  const joined = game.players.filter((p) => p.joined);
  const kills = (p: GamePlayer) => zombie?.kills[String(p.player_id)] ?? 0;
  const ranked = [...joined].sort((a, b) => kills(b) - kills(a));
  const best = zombie?.best_level ?? 0;
  return (
    <div className="zombie-theme min-h-[100dvh]">
      <div className="mx-auto flex min-h-[100dvh] max-w-sm flex-col items-center justify-center gap-3 px-5 py-8 text-center">
        <p className="za-soft text-sm font-semibold">{game.game_name}</p>
        <div className="flex justify-center -space-x-3">
          {ranked.slice(0, 4).map((p) => (
            <Portrait key={p.player_id} player={p} size={84} />
          ))}
        </div>
        <h1 className="za-title text-4xl leading-none">
          {best > 0 ? `Survived to level ${best}!` : "Game over"}
        </h1>
        <p className="text-sm font-semibold">
          {game.rounds_played === 0
            ? "This game ended before a level was finished."
            : `${game.rounds_played} ${game.rounds_played === 1 ? "level" : "levels"} played. Thanks for defending the house!`}
        </p>
        {ranked.length > 0 && game.rounds_played > 0 && (
          <ul className="za-card w-full space-y-1 p-3 text-left">
            {ranked.map((p) => (
              <li key={p.player_id} className="flex items-center gap-2 rounded-xl bg-white/[0.07] px-2 py-1">
                <Portrait player={p} size={30} />
                <span className="min-w-0 flex-1 truncate font-bold">{p.name}</span>
                <span className="flex items-center gap-1 font-black tabular-nums">
                  <Skull className="h-4 w-4 text-[color:var(--za-green)]" /> {kills(p)}
                </span>
              </li>
            ))}
          </ul>
        )}
        <a href="/" className="za-btn za-btn-quiet mt-2">
          Open Family Assistant
        </a>
      </div>
    </div>
  );
}
