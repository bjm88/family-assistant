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

/** Who a player is in Zombie Apocalypse. */
export type SurvivorStyle =
  | "army_guy"
  | "army_girl"
  | "dad"
  | "mom"
  | "girl"
  | "boy"
  | "uncle"
  | "grandma";

/** A turkey in Turkey TicTacToe, a character in Zombie Apocalypse. */
export type AvatarStyle = TurkeyStyle | SurvivorStyle;

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
  avatar_style: AvatarStyle | null;
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

// ---- Zombie Apocalypse (see python/api/games/zombie_apocalypse.py) ------

export type WeaponKind = "shot" | "cone" | "lob" | "roll";

export interface ZombieWeapon {
  key: string;
  name: string;
  /** shot: one bolt; cone: a spray; lob: thrown, bursts where it lands; roll: a boulder. */
  kind: WeaponKind;
  area: boolean;
  damage: number;
  /** How far it reaches, in map units. */
  range: number;
  /** Map units a second. */
  speed: number;
  /** Seconds between shots. */
  reload: number;
  /** Shots for a two-minute level; null = never runs out. */
  ammo: number | null;
  spread: number;
  radius: number;
  falloff: number;
  /** Seconds the zombies it catches stand frozen. */
  freeze: number;
  /** Everyone carries it; it isn't one of the weapons a player picks. */
  given: boolean;
  blurb: string;
}

export interface ZombieKind {
  key: string;
  name: string;
  hp: number;
  speed: number;
  radius: number;
  flying: boolean;
  from_level: number;
}

/** A dog a player can pick to help guard the house. */
export interface GuardDog {
  key: string;
  name: string;
  breed: string;
  /** How much of a mauling it takes before it runs home. */
  hp: number;
  /** The damage one bite does. */
  bite: number;
  /** Map units a second. */
  speed: number;
  blurb: string;
}

export interface ZombieRules {
  world: { w: number; h: number };
  house: { x: number; y: number; w: number; h: number; hp: number };
  /** Where players stand: between left and right, on the top or the bottom wall. */
  track: { left: number; right: number; top: number; bottom: number };
  move_speed: number;
  cross_seconds: number;
  shot_width: number;
  pile_depth: number;
  countdown: number;
  default_weapon: string;
  loadout_picks: number;
  default_loadout: string[];
  weapons: ZombieWeapon[];
  zombies: ZombieKind[];
  bat_weave: number;
  bat_weave_rate: number;
  guard_dogs: GuardDog[];
  default_guard_dog: string;
  /** A dog comes home after this many kills... */
  guard_max_kills: number;
  /** ...and rests between this many seconds and this many. */
  guard_rest: [number, number];
}

export interface ZombieLevelResult {
  level: number;
  won: boolean;
  house: number;
  seconds: number;
  duration: number;
  zombies: number;
  /** By player id. */
  kills: Record<string, number>;
  shots: Record<string, number>;
  /** Of each player's kills, the ones their dog made. */
  dog_kills?: Record<string, number>;
}

export interface ZombieView {
  /** The level that is being played, or will be next. */
  level: number;
  /** Seconds a level lasts. */
  duration: number;
  durations: number[];
  /** By player id: the two weapons each player picked. */
  loadouts: Record<string, string[]>;
  /** By player id: the guard dog each player picked. */
  guard_dogs: Record<string, string>;
  /** Who is in the level being played. */
  fighting: number[];
  last: ZombieLevelResult | null;
  best_level: number;
  kills: Record<string, number>;
  /** The kinds of zombie the next level has. */
  coming: string[];
  rules: ZombieRules;
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
  /** Only for Zombie Apocalypse. */
  zombie?: ZombieView;
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
  | { type: "join"; style: AvatarStyle; color: ShirtColor }
  | { type: "start" }
  | { type: "loadout"; weapons: string[] }
  | { type: "guard_dog"; dog: string }
  | { type: "settings"; duration: number }
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
  /** Played live rather than in turns. */
  realtime: boolean;
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
  avatar_style: AvatarStyle | null;
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
  /** Zombie Apocalypse only: the highest level survived. */
  best?: number;
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
