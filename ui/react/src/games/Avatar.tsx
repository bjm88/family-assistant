import { Turkey, turkeyStyle } from "./Turkey";
import type { AvatarStyle, ShirtColor } from "./types";
import { Survivor } from "./zombies/Survivor";
import { isSurvivorStyle } from "./zombies/survivors";

interface AvatarProps {
  /** Which game the player is in: it decides what their style means. */
  gameType: string;
  style: AvatarStyle | null;
  color: ShirtColor | null;
  size: number;
  className?: string;
}

/**
 * A player as they appear in their game: a turkey in Turkey TicTacToe,
 * a character in Zombie Apocalypse. Renders nothing for someone who
 * hasn't picked one yet.
 */
export function Avatar({ gameType, style, color, size, className }: AvatarProps) {
  if (!style || !color) return null;
  if (gameType === "zombie_apocalypse") {
    return isSurvivorStyle(style) ? <Survivor style={style} color={color} size={size} className={className} /> : null;
  }
  const turkey = turkeyStyle(style);
  return turkey ? <Turkey style={turkey} color={color} size={size} className={className} /> : null;
}
