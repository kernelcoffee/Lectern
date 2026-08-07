"""Proxy linking endpoints — attach Lectern servers behind a Velocity proxy.

``PUT`` rewrites velocity.toml's ``[servers]`` registry (link order = try
order, first entry is where new players land) and applies the per-backend
side effects the proxy model demands:

- every linked backend gets ``online-mode=false`` (the proxy authenticates;
  players reach backends through it) — reverted when unlinked;
- Fabric/Quilt backends get **FabricProxy-Lite** installed from Modrinth and
  its config written with the proxy's forwarding secret, so real player
  identities (Mojang UUIDs) pass through;
- vanilla / (Neo)Forge backends can't do modern forwarding out of the box —
  linking works, but the response carries a warning (offline UUIDs break
  whitelist/ops on vanilla; Forge-family needs a manual compat mod).

Backends are addressed as ``127.0.0.1:{port}`` — Lectern runs every server
process on the same host, and unpublished backend ports being unreachable
from the LAN (only via the proxy) is exactly the topology you want.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..content import manager as content
from ..content.manager import ContentError
from ..db import get_session
from ..models import Server
from ..servers import properties as props
from ..servers import velocity
from ..servers.manager import manager
from ..servers.stats_sampler import effective_port
from ..servers.types import is_proxy_type
from ..servers.velocity import VelocityConfigError
from .servers import BACKEND_PORT_BASE

router = APIRouter(prefix="/api/servers", tags=["proxy"])

# Modrinth slug of the forwarding-compat mod for Fabric-family backends.
_FORWARDING_MOD_SLUG = "fabricproxy-lite"
_FORWARDING_MOD_TYPES = {"fabric", "quilt"}

# How player-info forwarding can work per backend type (UI hint).
FORWARDING = {
    "fabric": "mod",  # FabricProxy-Lite, auto-installed
    "quilt": "mod",
    "neoforge": "manual",  # compat mods exist; not automated
    "forge": "manual",
    "vanilla": "none",  # impossible without server mods
}


def _get_proxy(server_id: str, session: Session) -> Server:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if not is_proxy_type(server.type):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a proxy server")
    if not server.path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Proxy is not installed yet")
    return server


def _payload(session: Session, proxy: Server) -> dict:
    proxy_dir = Path(proxy.path)
    links = velocity.linked_servers(proxy_dir)
    config = velocity.read_config(proxy_dir)
    try_order = [
        str(n) for n in (config.get("servers") or {}).get("try", []) if str(n) in links
    ]
    backends = [
        s
        for s in session.exec(select(Server).order_by(Server.created_at)).all()
        if s.id != proxy.id and not is_proxy_type(s.type)
    ]
    by_address = {f"127.0.0.1:{s.port}": s for s in backends}
    return {
        "links": [
            {
                "name": name,
                "address": address,
                "server_id": getattr(by_address.get(address), "id", None),
            }
            for name, address in links.items()
        ],
        "try": try_order,
        "candidates": [
            {
                "server_id": s.id,
                "name": s.name,
                "port": s.port,
                "type": s.type,
                "linked": f"127.0.0.1:{s.port}" in links.values(),
                "forwarding": FORWARDING.get(s.type, "manual"),
            }
            for s in backends
        ],
    }


@router.get("/{server_id}/proxy")
def get_proxy_links(server_id: str, session: Session = Depends(get_session)) -> dict:
    proxy = _get_proxy(server_id, session)
    try:
        return _payload(session, proxy)
    except VelocityConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


async def _prepare_backend(
    session: Session, backend: Server, secret: str, warnings: list[str]
) -> None:
    """Side effects for a newly-linked backend: offline-mode + forwarding."""
    backend_dir = Path(backend.path)
    file_props = props.read_properties(backend_dir)
    file_props["online-mode"] = "false"
    props.write_properties(backend_dir, file_props)

    if backend.type in _FORWARDING_MOD_TYPES:
        try:
            await content.install(
                session,
                backend.id,
                backend_dir,
                project_id=_FORWARDING_MOD_SLUG,
                loader=[backend.type, "fabric"],
                mc_version=backend.mc_version,
            )
            config_dir = backend_dir / "config"
            config_dir.mkdir(exist_ok=True)
            (config_dir / "FabricProxy-Lite.toml").write_text(
                velocity.toml_dumps(
                    {"hackOnlineMode": True, "hackEarlySend": False, "secret": secret}
                )
            )
        except ContentError as exc:
            warnings.append(
                f"{backend.name}: FabricProxy-Lite could not be installed ({exc}) — "
                "modern forwarding needs it; install it manually from the Mods tab."
            )
    elif FORWARDING.get(backend.type) == "manual":
        warnings.append(
            f"{backend.name}: {backend.type} needs a proxy-compat mod for modern "
            "forwarding — without one, players get offline UUIDs on this backend."
        )
    elif FORWARDING.get(backend.type) == "none":
        warnings.append(
            f"{backend.name}: vanilla servers cannot verify proxied players — "
            "they get offline UUIDs (whitelist/ops by Mojang UUID will not match)."
        )


@router.put("/{server_id}/proxy")
async def set_proxy_links(
    server_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    """Replace the proxy's linked servers. Body: ``{"server_ids": [...]}`` in
    try-order (first = default landing server). Newly-linked backends are
    prepared for proxying; unlinked ones get ``online-mode=true`` back.
    Changes apply at each server's next start."""
    proxy = _get_proxy(server_id, session)
    proxy_dir = Path(proxy.path)
    ids = body.get("server_ids") or []
    if not isinstance(ids, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "server_ids must be a list")

    previously_linked = set(velocity.linked_servers(proxy_dir).values())
    warnings: list[str] = []
    moved: list[dict] = []
    links: dict[str, str] = {}
    try_order: list[str] = []
    secret = velocity.read_secret(proxy_dir)

    # The proxy owns its (public) port — a backend configured on the same
    # port can't run alongside it, so linking moves the backend to a free
    # internal port. Ports in use = every server's effective port.
    proxy_port = velocity.get_bind_port(proxy_dir) or proxy.port
    all_servers = session.exec(select(Server)).all()
    used_ports = {effective_port(s) for s in all_servers}
    used_ports.add(proxy_port)

    def next_free_port() -> int:
        port = BACKEND_PORT_BASE
        while port in used_ports:
            port += 1
        used_ports.add(port)
        return port

    for backend_id in ids:
        backend = session.get(Server, backend_id)
        if backend is None or is_proxy_type(backend.type):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Server {backend_id} not found")
        if not backend.path:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"{backend.name} is not installed yet"
            )
        backend_port = effective_port(backend)
        if backend_port == proxy_port:
            if manager.is_running(backend.id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f'"{backend.name}" is running on port {backend_port}, which the '
                    "proxy needs. Stop it first so it can be moved to an internal port.",
                )
            new_port = next_free_port()
            backend_dir = Path(backend.path)
            file_props = props.read_properties(backend_dir)
            file_props["server-port"] = str(new_port)
            props.write_properties(backend_dir, file_props)
            backend.port = new_port
            session.add(backend)
            session.commit()
            session.refresh(backend)
            moved.append(
                {
                    "server_id": backend.id,
                    "name": backend.name,
                    "from": backend_port,
                    "to": new_port,
                }
            )
            backend_port = new_port
        name = velocity.link_name(backend.name)
        if name in links:  # sanitized names collided — disambiguate by port
            name = f"{name}-{backend.port}"
        address = f"127.0.0.1:{backend_port}"
        links[name] = address
        try_order.append(name)
        if address not in previously_linked:
            await _prepare_backend(session, backend, secret, warnings)

    # Revert online-mode on backends that were linked and no longer are.
    for s in session.exec(select(Server)).all():
        address = f"127.0.0.1:{s.port}"
        if (
            not is_proxy_type(s.type)
            and s.path
            and address in previously_linked
            and address not in links.values()
        ):
            file_props = props.read_properties(Path(s.path))
            file_props["online-mode"] = "true"
            props.write_properties(Path(s.path), file_props)

    try:
        velocity.write_links(proxy_dir, links, try_order)
    except VelocityConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return {**_payload(session, proxy), "warnings": warnings, "moved": moved}
