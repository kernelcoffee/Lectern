"""App settings endpoints — the runtime-editable tunables (Settings UI).

``GET`` returns each tunable with its current value + metadata (label, help,
bounds, unit); ``PATCH`` persists overrides into the ``Setting`` table (values
outside a tunable's bounds → 422, nothing written).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from .. import app_settings
from ..db import get_session
from ..models import SettingRead

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _read_all(session: Session) -> list[SettingRead]:
    values = app_settings.all_values(session)
    return [
        SettingRead(
            key=t.key,
            label=t.label,
            help=t.help,
            unit=t.unit,
            value=values[t.key],
            min=t.minimum,
            max=t.maximum,
            category=t.category,
        )
        for t in app_settings.TUNABLES.values()
    ]


@router.get("", response_model=list[SettingRead])
def list_settings(session: Session = Depends(get_session)) -> list[SettingRead]:
    return _read_all(session)


@router.patch("", response_model=list[SettingRead])
def update_settings(
    updates: dict[str, int], session: Session = Depends(get_session)
) -> list[SettingRead]:
    try:
        app_settings.set_values(session, updates)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return _read_all(session)
