import type { Person } from "@/lib/types";

interface ProfileAvatarProps {
  person: Pick<Person, "first_name" | "last_name" | "profile_photo_path" | "preferred_name">;
  size?: number;
}

export function ProfileAvatar({ person, size = 56 }: ProfileAvatarProps) {
  const initials =
    `${(person.preferred_name || person.first_name)?.[0] ?? ""}${
      person.last_name?.[0] ?? ""
    }`.toUpperCase();

  const sizeStyle = { width: size, height: size };

  if (person.profile_photo_path) {
    return (
      <img
        src={`/api/media/${person.profile_photo_path}`}
        alt={`${person.first_name} ${person.last_name}`}
        style={sizeStyle}
        className="rounded-full object-cover bg-muted border border-border"
      />
    );
  }
  return (
    <div
      style={sizeStyle}
      className="rounded-full bg-primary/10 text-primary flex items-center justify-center font-semibold border border-border"
    >
      {initials || "??"}
    </div>
  );
}
