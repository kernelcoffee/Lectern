// Player face avatar, served by Lectern (rendered from the Mojang skin). Falls
// back to an initial tile if the player has no skin / it can't load.

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

  if (!ok) {
    return (
      <div
        style={{ width: size, height: size, fontSize: Math.round(size / 2.4) }}
        className="flex shrink-0 items-center justify-center rounded-sm bg-slate-700 font-medium text-slate-300"
      >
        {name.slice(0, 1).toUpperCase()}
      </div>
    );
  }

  return (
    <img
      src={avatarUrl(uuid, size)}
      width={size}
      height={size}
      onError={() => setOk(false)}
      alt=""
      // Pixel skins look best crisp, not smoothed, when scaled up.
      style={{ imageRendering: "pixelated" }}
      className="shrink-0 rounded-sm"
    />
  );
}
