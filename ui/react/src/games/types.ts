// Shapes returned by /api/games (see python/api/games/service.py).

export type GameStatus = "lobby" | "playing" | "round_over" | "ended";
export type InviteStatus = "not_sent" | "pending" | "sent" | "failed";
export type InviteChannel = "sms" | "email";
/** A family member, someone who joined through the shareable link, or the AI. */
export type PlayerKind = "family" | "guest" | "ai";
export type AiLevel = "easy" | "medium" | "hard";

export type TurkeyStyle =
  | "happy"
  | "strong"
  | "girl"
  | "cheerleader"
  | "hippie"
  | "beer";

export type ShirtColor =
  | "red"
  | "orange"
  | "gold"
  | "green"
  | "teal"
  | "blue"
  | "purple"
  | "pink";

// ---- What a player sees on /play/<token> ------------------------------

export interface GamePlayer {
  player_id: number;
  name: string;
  kind: PlayerKind;
  is_host: boolean;
  joined: boolean;
  avatar_style: TurkeyStyle | null;
  avatar_color: ShirtColor | null;
  wins: number;
  connected: boolean;
  invite_status: InviteStatus;
}

export interface GameBoard {
  rows: number;
  cols: number;
  win_length: number;
  /** rows * cols entries, row by row; the player_id on each square or null. */
  cells: (number | null)[];
  turn_order: number[];
  round: number;
  winner_player_id: number | null;
  /** In order from one end of the winning line to the other. */
  winning_cells: number[];
  is_draw: boolean;
  last_move: number | null;
  /** Whose turn it is; null when no round is being played. */
  turn_player_id: number | null;
}

export interface GameInviteLink {
  player_id: number;
  name: string;
  play_url: string;
  invite_status: InviteStatus;
  invite_channels: InviteChannel[];
  invite_detail: string | null;
}

/** The game as anyone may see it. */
export interface GameBase {
  game_id: number;
  game_type: string;
  game_name: string;
  status: GameStatus;
  invited_by: string;
  min_players: number;
  max_players: number;
  rounds_played: number;
  can_start: boolean;
  /** Every seat is taken; nobody else can join. */
  is_full: boolean;
  players: GamePlayer[];
  board: GameBoard | null;
}

/** What a player sees on /play/<token>. */
export interface GameView extends GameBase {
  you: { player_id: number; name: string; is_host: boolean; joined: boolean };
  /** Host only: the shareable link. Anyone who opens it joins as a guest. */
  invite_url?: string | null;
  /** Host only: links for the people who haven't joined yet. */
  invites?: GameInviteLink[];
}

/** What someone sees on /play/join/<invite token> before they have joined. */
export interface GuestGameView extends GameBase {
  you: null;
}

/** Returned when a guest joins: their own link from here on. */
export interface GuestSeat {
  play_path: string;
  player_id: number;
  name: string;
}

export type GameAction =
  | { type: "join"; style: TurkeyStyle; color: ShirtColor }
  | { type: "start" }
  | { type: "move"; cell: number }
  | { type: "play_again" }
  | { type: "end" };

// ---- The Games page ----------------------------------------------------

export interface GameCatalogEntry {
  key: string;
  name: string;
  tagline: string;
  min_players: number;
  max_players: number;
  can_play_ai: boolean;
}

export interface GamePerson {
  person_id: number;
  name: string;
  last_name: string | null;
  /** How an invite would reach them; empty means share the link by hand. */
  notify: InviteChannel[];
}

export interface GameSummaryPlayer {
  player_id: number;
  /** Null for a guest and for the AI. */
  person_id: number | null;
  name: string;
  kind: PlayerKind;
  is_host: boolean;
  joined: boolean;
  avatar_style: TurkeyStyle | null;
  avatar_color: ShirtColor | null;
  wins: number;
  invite_status: InviteStatus;
  invite_channels: InviteChannel[];
  invite_detail: string | null;
  /** Only for the viewer's own seat, or every seat for the host / an admin. Never for the AI. */
  play_url: string | null;
}

export interface GameSummary {
  game_id: number;
  game_type: string;
  game_name: string;
  status: GameStatus;
  rounds_played: number;
  max_players: number;
  created_at: string;
  host_name: string;
  can_manage: boolean;
  my_play_path: string | null;
  /** The shareable link, for the host / an admin while the game is going. */
  invite_url: string | null;
  players: GameSummaryPlayer[];
}

export interface LeaderboardEntry {
  person_id: number | null;
  kind: PlayerKind;
  name: string;
  wins: number;
  games: number;
}

export interface GamesOverview {
  me_person_id: number | null;
  can_pick_host: boolean;
  catalog: GameCatalogEntry[];
  people: GamePerson[];
  games: GameSummary[];
  leaderboard: Record<string, LeaderboardEntry[]>;
  /** What the AI opponent is called, e.g. "Avi AI". */
  ai_name: string;
  ai_levels: AiLevel[];
  ai_default_level: AiLevel;
}
