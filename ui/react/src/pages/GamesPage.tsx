import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Crown, Gamepad2, Link2, Mail, MessageSquare, Play, Share2, Trophy, UserPlus } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { PageHeader } from "@/components/PageHeader";
import { Modal } from "@/components/Modal";
import { useConfirm } from "@/components/ConfirmDialog";
import { useToast } from "@/components/Toast";
import { Avatar } from "@/games/Avatar";
import { Turkey } from "@/games/Turkey";
import { Marble } from "@/games/checkers/Marble";
import { Survivor } from "@/games/zombies/Survivor";
import { copyLink, shareJoinLink } from "@/games/share";
import type {
  AiLevel,
  GameCatalogEntry,
  GamePerson,
  GamesOverview,
  GameSummary,
  GameSummaryPlayer,
} from "@/games/types";
import "@/games/games.css";
import "@/games/zombies/zombies.css";
import "@/games/checkers/checkers.css";

// The family's Games page: start a game and invite people, hop back
// into one that's going, and see who has won the most.

const STATUS_LABEL: Record<GameSummary["status"], string> = {
  lobby: "Waiting for players",
  playing: "Playing now",
  round_over: "Between rounds",
  ended: "Finished",
};

const AI_LEVEL_BLURB: Record<AiLevel, string> = {
  easy: "Misses a lot. Good for young players.",
  medium: "Plays to win and blocks most of the time, but slips up now and then.",
  hard: "Doesn't miss a thing.",
};

type Dialog =
  | { kind: "new"; game: GameCatalogEntry }
  | { kind: "invite"; game: GameSummary };

export default function GamesPage() {
  const { familyId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const [dialog, setDialog] = useState<Dialog | null>(null);

  const queryKey = ["games", familyId];
  const { data, isLoading, error } = useQuery<GamesOverview>({
    queryKey,
    queryFn: () => api.get<GamesOverview>(`/api/games?family_id=${familyId}`),
    enabled: !!familyId,
    // Other people join and finish games; keep the list fresh.
    refetchInterval: 5000,
  });

  const end = useMutation({
    mutationFn: (gameId: number) => api.post<GameSummary>(`/api/games/${gameId}/end`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey }),
    onError: (err: Error) => toast.error(err.message),
  });

  if (isLoading) return <div className="text-sm text-muted-foreground">Loading games…</div>;
  if (error || !data) {
    return <div className="text-sm text-destructive">{(error as Error)?.message ?? "Couldn't load games."}</div>;
  }

  // Zombie Apocalypse is played in levels, not rounds.
  const inLevels = new Set(data.catalog.filter((g) => g.realtime).map((g) => g.key));
  const active = data.games.filter((g) => g.status !== "ended");
  const finished = data.games.filter((g) => g.status === "ended" && g.rounds_played > 0).slice(0, 5);

  const share = async (game: GameSummary, player: GameSummaryPlayer) => {
    if (!player.play_url) return;
    const how = await shareJoinLink(player.play_url, `${player.name}, come play ${game.game_name} with me!`);
    if (how === "copied") toast.success(`Link for ${player.name} copied`);
    if (how === "failed") toast.error("Couldn't copy the link");
  };

  const copyInvite = async (game: GameSummary) => {
    if (!game.invite_url) return;
    if (await copyLink(game.invite_url)) {
      toast.success("Invite link copied. Anyone who opens it types their name and joins.");
    } else {
      toast.error("Couldn't copy the link");
    }
  };

  return (
    <div>
      <PageHeader title="Games" description="Quick games to play together, each on your own phone." />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          {data.catalog.map((game) =>
            game.realtime ? (
              <section key={game.key} className="zombie-theme overflow-hidden rounded-xl border border-[#2f4a34] shadow-sm">
                <div className="flex flex-col items-center gap-4 p-5 sm:flex-row sm:p-6">
                  <div className="flex shrink-0 items-end -space-x-5">
                    <Survivor style="grandma" color="purple" size={84} />
                    <Survivor style="army_guy" color="green" size={108} className="relative z-10" />
                    <Survivor style="uncle" color="orange" size={84} />
                  </div>
                  <div className="min-w-0 flex-1 text-center sm:text-left">
                    <h2 className="za-title text-3xl leading-none">{game.name}</h2>
                    <p className="mt-2 text-sm font-semibold">{game.tagline}</p>
                    <p className="za-soft mt-1 text-xs">
                      {playerCount(game)} on one team · played live, a couple of minutes a level · every level is
                      harder · invite anyone with a link, no sign-in needed
                    </p>
                    <button
                      type="button"
                      className="za-btn za-btn-primary mt-4"
                      onClick={() => setDialog({ kind: "new", game })}
                    >
                      <Play className="h-5 w-5 fill-current" /> Start a game
                    </button>
                  </div>
                </div>
              </section>
            ) : game.key === "chinese_checkers" ? (
              <section key={game.key} className="checkers-theme overflow-hidden rounded-xl border border-[#1d5a51] shadow-sm">
                <div className="flex flex-col items-center gap-4 p-5 sm:flex-row sm:p-6">
                  <div className="flex shrink-0 items-end -space-x-3">
                    <Marble color="blue" size={64} />
                    <Marble color="red" size={88} className="relative z-10" />
                    <Marble color="gold" size={64} />
                  </div>
                  <div className="min-w-0 flex-1 text-center sm:text-left">
                    <h2 className="cc-title text-3xl leading-none">{game.name}</h2>
                    <p className="mt-2 text-sm font-semibold">{game.tagline}</p>
                    <p className="cc-soft mt-1 text-xs">
                      {playerCount(game)}, each with a color · chain hops for big moves, and everyone watches them
                      play out · invite anyone with a link, no sign-in needed
                    </p>
                    <button
                      type="button"
                      className="cc-btn cc-btn-primary mt-4"
                      onClick={() => setDialog({ kind: "new", game })}
                    >
                      <Play className="h-5 w-5 fill-current" /> Start a game
                    </button>
                  </div>
                </div>
              </section>
            ) : (
              <section key={game.key} className="turkey-theme overflow-hidden rounded-xl border border-[#e9b872] shadow-sm">
                <div className="flex flex-col items-center gap-4 p-5 sm:flex-row sm:p-6">
                  <div className="flex shrink-0 items-end -space-x-6">
                    <Turkey style="cheerleader" color="purple" size={84} />
                    <Turkey style="happy" color="blue" size={108} className="relative z-10" />
                    <Turkey style="beer" color="green" size={84} />
                  </div>
                  <div className="min-w-0 flex-1 text-center sm:text-left">
                    <h2 className="tg-title text-3xl leading-none">{game.name}</h2>
                    <p className="mt-2 text-sm font-semibold">{game.tagline}</p>
                    <p className="mt-1 text-xs text-[color:var(--tg-ink-soft)]">
                      {playerCount(game)} · a few minutes a round · everyone gets a link by text or email, no sign-in
                      needed
                      {game.can_play_ai && ` · or play ${data.ai_name} on your own`}
                    </p>
                    <button
                      type="button"
                      className="tg-btn tg-btn-primary mt-4"
                      onClick={() => setDialog({ kind: "new", game })}
                    >
                      <Play className="h-5 w-5 fill-current" /> Start a game
                    </button>
                  </div>
                </div>
              </section>
            )
          )}

          <section className="card">
            <div className="card-header">
              <div className="card-title flex items-center gap-2">
                <Gamepad2 className="h-4 w-4 text-primary" /> Games going on
              </div>
            </div>
            {active.length === 0 ? (
              <div className="card-body text-sm text-muted-foreground">
                Nobody is playing right now. Start a game and invite someone.
              </div>
            ) : (
              <ul className="divide-y divide-border">
                {active.map((game) => (
                  <li key={game.game_id} className="px-5 py-4">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-semibold">{game.game_name}</span>
                          <span
                            className={cn(
                              "badge whitespace-nowrap",
                              game.status === "playing" && "border-green-200 bg-green-50 text-green-700"
                            )}
                          >
                            {game.status === "round_over" && inLevels.has(game.game_type)
                              ? "Between levels"
                              : STATUS_LABEL[game.status]}
                          </span>
                        </div>
                        <div className="mt-0.5 text-xs text-muted-foreground">
                          Started by {game.host_name} {timeAgo(game.created_at)}
                          {game.rounds_played > 0 &&
                            ` · ${count(game.rounds_played, inLevels.has(game.game_type) ? "level" : "round")} played`}
                        </div>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        {game.my_play_path && (
                          <button
                            type="button"
                            className="btn-primary"
                            onClick={() => navigate(game.my_play_path!)}
                          >
                            <Play className="h-4 w-4" />
                            {mySeat(game)?.joined ? "Back to game" : "Join"}
                          </button>
                        )}
                        {game.can_manage && game.players.length < game.max_players && (
                          <button
                            type="button"
                            className="btn-secondary"
                            onClick={() => setDialog({ kind: "invite", game })}
                          >
                            <UserPlus className="h-4 w-4" /> Invite
                          </button>
                        )}
                        {game.invite_url && (
                          <button
                            type="button"
                            className="btn-secondary"
                            title="Anyone who opens this link types their name and joins"
                            onClick={() => void copyInvite(game)}
                          >
                            <Link2 className="h-4 w-4" /> Copy invite link
                          </button>
                        )}
                        {(game.can_manage || game.my_play_path) && (
                          <button
                            type="button"
                            className="btn-secondary"
                            disabled={end.isPending}
                            onClick={async () => {
                              const ok = await confirm({
                                title: "End this game?",
                                message: "It ends for everyone playing. Wins so far are kept.",
                                confirmLabel: "End game",
                                destructive: true,
                              });
                              if (ok) end.mutate(game.game_id);
                            }}
                          >
                            End
                          </button>
                        )}
                      </div>
                    </div>

                    <ul className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                      {game.players.map((p) => (
                        <li key={p.player_id} className="flex items-center gap-2 rounded-lg bg-muted/60 px-2 py-1.5">
                          {p.avatar_style && p.avatar_color ? (
                            <Avatar gameType={game.game_type} style={p.avatar_style} color={p.avatar_color} size={36} />
                          ) : (
                            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-dashed border-border bg-white text-sm font-semibold text-muted-foreground">
                              {p.name.slice(0, 1).toUpperCase()}
                            </span>
                          )}
                          <div className="min-w-0 flex-1 leading-tight">
                            <div className="truncate text-sm font-medium">
                              {p.name}
                              {p.kind === "ai" && <Bot className="ml-1 inline h-3.5 w-3.5 text-muted-foreground" />}
                              {p.wins > 0 && (
                                <span className="ml-1.5 inline-flex items-center gap-0.5 text-xs text-amber-600">
                                  <Trophy className="h-3 w-3" /> {p.wins}
                                </span>
                              )}
                            </div>
                            <div className="truncate text-xs text-muted-foreground" title={p.invite_detail ?? undefined}>
                              {playerStatus(p)}
                            </div>
                          </div>
                          {!p.joined && p.play_url && game.can_manage && p !== mySeat(game) && (
                            <button
                              type="button"
                              className="shrink-0 rounded-md border border-border bg-white px-2 py-1 text-xs font-medium hover:bg-accent"
                              onClick={() => void share(game, p)}
                            >
                              <Share2 className="mr-1 inline h-3 w-3" />
                              Share link
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {finished.length > 0 && (
            <section className="card">
              <div className="card-header">
                <div className="card-title">Recent games</div>
              </div>
              <ul className="divide-y divide-border">
                {finished.map((game) => (
                  <li key={game.game_id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3 text-sm">
                    <span className="font-medium">{game.game_name}</span>
                    <span className="text-xs text-muted-foreground">{timeAgo(game.created_at)}</span>
                    <span className="ml-auto flex flex-wrap items-center gap-3">
                      {game.players
                        .filter((p) => p.joined)
                        .sort((a, b) => b.wins - a.wins)
                        .map((p) => (
                          <span key={p.player_id} className="inline-flex items-center gap-1">
                            <Avatar gameType={game.game_type} style={p.avatar_style} color={p.avatar_color} size={24} />
                            {p.name} <span className="font-semibold tabular-nums">{p.wins}</span>
                          </span>
                        ))}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <aside className="space-y-6">
          {data.catalog.map((game) => {
            const board = data.leaderboard[game.key] ?? [];
            return (
              <section key={game.key} className="card">
                <div className="card-header">
                  <div className="card-title flex items-center gap-2">
                    <Trophy className="h-4 w-4 text-amber-500" /> Hall of fame
                  </div>
                  <span className="text-xs text-muted-foreground">{game.name}</span>
                </div>
                {board.length === 0 ? (
                  <div className="card-body text-sm text-muted-foreground">
                    {game.realtime
                      ? "No levels survived yet. Hold the house once and you're on top."
                      : "No rounds won yet. The first win puts you on top."}
                  </div>
                ) : (
                  <ol className="divide-y divide-border">
                    {board.map((entry, i) => (
                      <li
                        key={`${entry.kind}-${entry.person_id ?? entry.name}`}
                        className="flex items-center gap-3 px-5 py-2.5"
                      >
                        <span className="w-5 text-center text-sm font-semibold text-muted-foreground">
                          {i === 0 && entry.wins > 0 ? <Crown className="inline h-4 w-4 text-amber-500" /> : i + 1}
                        </span>
                        <span className="min-w-0 flex-1 truncate font-medium">
                          {entry.name}
                          {entry.kind === "ai" && <Bot className="ml-1 inline h-3.5 w-3.5 text-muted-foreground" />}
                          {entry.kind === "guest" && (
                            <span className="ml-1.5 text-xs font-normal text-muted-foreground">guest</span>
                          )}
                        </span>
                        <span className="text-right leading-tight">
                          <span className="font-semibold tabular-nums">
                            {game.realtime ? `${count(entry.wins, "level")} survived` : count(entry.wins, "win")}
                          </span>
                          <span className="block text-[11px] text-muted-foreground">
                            {game.realtime && entry.best ? `best: level ${entry.best} · ` : ""}
                            {count(entry.games, "game")}
                          </span>
                        </span>
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            );
          })}
        </aside>
      </div>

      {dialog && familyId && (
        <InviteDialog
          key={dialog.kind === "new" ? `new-${dialog.game.key}` : `invite-${dialog.game.game_id}`}
          dialog={dialog}
          familyId={Number(familyId)}
          overview={data}
          onClose={() => setDialog(null)}
          onDone={(game) => {
            setDialog(null);
            void queryClient.invalidateQueries({ queryKey });
            if (dialog.kind === "new" && game.my_play_path) navigate(game.my_play_path);
            else toast.success(dialog.kind === "new" ? "Game started" : "Invite sent");
          }}
        />
      )}
    </div>
  );
}

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

function playerCount(game: GameCatalogEntry): string {
  return game.min_players === game.max_players
    ? count(game.min_players, "player")
    : `${game.min_players}–${game.max_players} players`;
}

/** The viewer's own seat in a game, if they have one. */
function mySeat(game: GameSummary): GameSummaryPlayer | undefined {
  const path = game.my_play_path;
  return path ? game.players.find((p) => p.play_url?.endsWith(path)) : undefined;
}

function playerStatus(p: GameSummaryPlayer): string {
  if (p.kind === "ai") return "AI player";
  if (p.joined) return p.is_host ? "Started the game" : p.kind === "guest" ? "Guest — in the game" : "In the game";
  if (p.invite_status === "pending") return "Sending the invite…";
  if (p.invite_status === "sent") {
    const via = p.invite_channels.map((c) => (c === "sms" ? "text" : "email")).join(" and ");
    return via ? `Invited by ${via}` : "Invited";
  }
  if (p.invite_status === "failed") return "Couldn't send an invite";
  return p.is_host ? "Hasn't joined yet" : "Needs the link";
}

function timeAgo(iso: string): string {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const days = Math.round(hours / 24);
  return `${days} ${days === 1 ? "day" : "days"} ago`;
}

// ---------------------------------------------------------------------------
// Start a game / invite more people
// ---------------------------------------------------------------------------

interface InviteDialogProps {
  dialog: Dialog;
  familyId: number;
  overview: GamesOverview;
  onClose: () => void;
  onDone: (game: GameSummary) => void;
}

function InviteDialog({ dialog, familyId, overview, onClose, onDone }: InviteDialogProps) {
  const toast = useToast();
  const isNew = dialog.kind === "new";
  const [hostId, setHostId] = useState<number | null>(overview.me_person_id);
  const [invitees, setInvitees] = useState<number[]>([]);
  const [sendInvites, setSendInvites] = useState(true);
  const canPlayAi = isNew && dialog.game.can_play_ai;
  const [vsAi, setVsAi] = useState(false);
  const [aiLevel, setAiLevel] = useState<AiLevel>(overview.ai_default_level);

  const alreadyIn = isNew ? [] : dialog.game.players.map((p) => p.person_id);
  const choices = overview.people.filter(
    (p) => !alreadyIn.includes(p.person_id) && !(isNew && p.person_id === hostId)
  );
  // Seats left for family members: the host has one, and so does the AI.
  const room = isNew
    ? dialog.game.max_players - 1 - (vsAi ? 1 : 0)
    : dialog.game.max_players - dialog.game.players.length;

  // Changing who the host is can make them one of the ticked invitees,
  // and giving the AI a seat can leave one too few for them.
  useEffect(() => {
    setInvitees((ids) => ids.filter((id) => id !== hostId).slice(0, room));
  }, [hostId, room]);

  const submit = useMutation({
    mutationFn: () =>
      isNew
        ? api.post<GameSummary>("/api/games", {
            family_id: familyId,
            game_type: dialog.game.key,
            host_person_id: hostId,
            invite_person_ids: invitees,
            send_invites: sendInvites,
            vs_ai: vsAi,
            ai_level: aiLevel,
          })
        : api.post<GameSummary>(`/api/games/${dialog.game.game_id}/invites`, {
            person_ids: invitees,
            send_invites: sendInvites,
          }),
    onSuccess: onDone,
    onError: (err: Error) => toast.error(err.message),
  });

  const toggle = (personId: number) =>
    setInvitees((ids) => (ids.includes(personId) ? ids.filter((id) => id !== personId) : [...ids, personId]));

  const needsHost = isNew && hostId === null;
  const anyUnreachable = overview.people.some((p) => invitees.includes(p.person_id) && p.notify.length === 0);

  return (
    <Modal
      open
      onClose={onClose}
      title={isNew ? `Start ${dialog.game.name}` : `Invite to ${dialog.game.game_name}`}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={submit.isPending}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            // A new game needs nobody picked: the host can play the AI,
            // or pass the invite link around once it has started.
            disabled={(!isNew && invitees.length === 0) || needsHost || submit.isPending}
            onClick={() => submit.mutate()}
          >
            {isNew ? (submit.isPending ? "Starting…" : "Start game") : submit.isPending ? "Inviting…" : "Invite"}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        {isNew && overview.can_pick_host && (
          <div>
            <label className="label" htmlFor="game-host">
              You're playing as
            </label>
            <select
              id="game-host"
              className="input"
              value={hostId ?? ""}
              onChange={(e) => setHostId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Choose…</option>
              {overview.people.map((p) => (
                <option key={p.person_id} value={p.person_id}>
                  {fullName(p)}
                </option>
              ))}
            </select>
          </div>
        )}

        {canPlayAi && (
          <div className="rounded-md border border-border">
            <label className="flex cursor-pointer items-center gap-3 px-3 py-2.5">
              <input
                type="checkbox"
                className="h-4 w-4"
                checked={vsAi}
                onChange={(e) => setVsAi(e.target.checked)}
              />
              <Bot className="h-4 w-4 shrink-0 text-primary" />
              <span className="min-w-0 flex-1 text-sm">
                <span className="font-medium">Play against {overview.ai_name}</span>
                <span className="block text-xs text-muted-foreground">
                  Ready straight away — no waiting for anyone to join.
                </span>
              </span>
            </label>
            {vsAi && (
              <div className="flex items-center gap-2 border-t border-border px-3 py-2.5">
                <span className="text-xs font-medium text-muted-foreground">How good?</span>
                <div className="flex flex-1 gap-1" role="radiogroup" aria-label={`How well ${overview.ai_name} plays`}>
                  {overview.ai_levels.map((level) => (
                    <button
                      key={level}
                      type="button"
                      role="radio"
                      aria-checked={aiLevel === level}
                      onClick={() => setAiLevel(level)}
                      className={cn(
                        "flex-1 rounded-md border px-2 py-1.5 text-sm font-medium capitalize",
                        aiLevel === level
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-border bg-white hover:bg-accent"
                      )}
                    >
                      {level}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {vsAi && <p className="px-3 pb-2.5 text-xs text-muted-foreground">{AI_LEVEL_BLURB[aiLevel]}</p>}
          </div>
        )}

        <div>
          <div className="label">
            {isNew ? "Invite family" : "Who do you want to play with?"}{" "}
            <span className="normal-case">({isNew ? "optional, " : ""}up to {room})</span>
          </div>
          {choices.length === 0 ? (
            <p className="text-sm text-muted-foreground">There's nobody else in the family to invite yet.</p>
          ) : (
            <ul className="divide-y divide-border rounded-md border border-border">
              {choices.map((p) => {
                const checked = invitees.includes(p.person_id);
                const full = !checked && invitees.length >= room;
                return (
                  <li key={p.person_id}>
                    <label
                      className={cn(
                        "flex cursor-pointer items-center gap-3 px-3 py-2.5",
                        full && "cursor-not-allowed opacity-50"
                      )}
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4"
                        checked={checked}
                        disabled={full}
                        onChange={() => toggle(p.person_id)}
                      />
                      <span className="min-w-0 flex-1 truncate text-sm font-medium">{fullName(p)}</span>
                      <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
                        {p.notify.includes("sms") && (
                          <span className="badge gap-1">
                            <MessageSquare className="h-3 w-3" /> Text
                          </span>
                        )}
                        {p.notify.includes("email") && (
                          <span className="badge gap-1">
                            <Mail className="h-3 w-3" /> Email
                          </span>
                        )}
                        {p.notify.length === 0 && "Share the link yourself"}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {invitees.length > 0 && (
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4"
              checked={sendInvites}
              onChange={(e) => setSendInvites(e.target.checked)}
            />
            <span>
              Send each person their link by text and email
              <span className="block text-xs text-muted-foreground">
                Texts only go to people who have opted in to them.
              </span>
            </span>
          </label>
        )}

        {invitees.length > 0 && (anyUnreachable || !sendInvites) && (
          <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
            {sendInvites
              ? "Someone you picked has no phone or email we can use. "
              : "No invites will be sent. "}
            You'll get a Share link button for each person on the next screen.
          </p>
        )}

        {isNew && (
          <p className="flex items-start gap-2 rounded-md bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
            <Link2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              Want someone else in? Once the game is open you can copy an invite link and send it to anyone. They
              type their name and join — no account needed.
            </span>
          </p>
        )}
      </div>
    </Modal>
  );
}

function fullName(p: GamePerson): string {
  return p.last_name ? `${p.name} ${p.last_name}` : p.name;
}
