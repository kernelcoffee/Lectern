// Player head avatar (Crafatar). Decorative — falls back to an initial tile if
// the image can't load (offline / blocked).

import { useState } from "react";
import { avatarUrl } from "../api/players";

export default function Avatar({
  uuid,
  name,
  size = 32,
}: {
  uuid: string;
  name: string;
  size?: number;
}) {
  const [ok, setOk] = useState(true);
  if (ok) {
    return (
      <img
        src={avatarUrl(uuid, size)}
        width={size}
        height={size}
        onError={() => setOk(false)}
        alt=""
        className="shrink-0 rounded-sm"
      />
    );
  }
  return (
    <div
      style={{ width: size, height: size }}
      className="flex shrink-0 items-center justify-center rounded-sm bg-slate-700 text-xs font-medium text-slate-300"
    >
      {name.slice(0, 1).toUpperCase()}
    </div>
  );
}
