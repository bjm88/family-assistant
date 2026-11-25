// Shapes returned by /api/games (see python/api/games/service.py).

export type GameStatus = "lobby" | "playing" | "round_over" | "ended";
export type InviteStatus = "not_sent" | "pending" | "sent" | "failed";
export type InviteChannel = "sms" | "email";

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

export interface GameView {
  game_id: number;
  game_type: string;
  game_name: string;
  status: GameStatus;
  invited_by: string;
  min_players: number;
  max_players: number;
  rounds_played: number;
  can_start: boolean;
  players: GamePlayer[];
  board: GameBoard | null;
  you: { player_id: number; name: string; is_host: boolean; joined: boolean };
  /** Host only: links for the people who haven't joined yet. */
  invites?: GameInviteLink[];
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
  person_id: number;
  name: string;
  is_host: boolean;
  joined: boolean;
  avatar_style: TurkeyStyle | null;
  avatar_color: ShirtColor | null;
  wins: number;
  invite_status: InviteStatus;
  invite_channels: InviteChannel[];
  invite_detail: string | null;
  /** Only for the viewer's own seat, or every seat for the host / an admin. */
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
  players: GameSummaryPlayer[];
}

export interface LeaderboardEntry {
  person_id: number;
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
}
