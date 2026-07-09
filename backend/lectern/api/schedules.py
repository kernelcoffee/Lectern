"""Schedule endpoints (M10): per-server cron CRUD.

Every write validates the cron expression (422 with the parser's message) and
the command-action invariant (a ``command`` schedule needs a command), then
mutates the row and re-syncs the scheduler job. ``next_run`` in responses is
computed from the trigger, so it's accurate even before the scheduler ticks.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..db import get_session
from ..models import (
    Schedule,
    ScheduleAction,
    ScheduleCreate,
    ScheduleRead,
    ScheduleUpdate,
    Server,
)
from ..scheduler import next_run_time, scheduler_service, validate_cron

router = APIRouter(prefix="/api/servers/{server_id}/schedules", tags=["schedules"])


def _get_server(server_id: str, session: Session) -> Server:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    return server


def _get_schedule(server_id: str, schedule_id: str, session: Session) -> Schedule:
    schedule = session.get(Schedule, schedule_id)
    if schedule is None or schedule.server_id != server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    return schedule


def _validate(action: str, cron: str, command: str | None) -> None:
    try:
        validate_cron(cron)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid cron expression: {exc}"
        ) from exc
    if action == ScheduleAction.command.value and not (command or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            'A "command" schedule needs a console command',
        )


def _read(schedule: Schedule) -> ScheduleRead:
    return ScheduleRead(
        **schedule.model_dump(), next_run=next_run_time(schedule)
    )


@router.get("", response_model=list[ScheduleRead])
def list_(server_id: str, session: Session = Depends(get_session)) -> list[ScheduleRead]:
    _get_server(server_id, session)
    rows = session.exec(
        select(Schedule).where(Schedule.server_id == server_id)
    ).all()
    return [_read(row) for row in rows]


@router.post("", response_model=ScheduleRead, status_code=status.HTTP_201_CREATED)
def create(
    server_id: str,
    payload: ScheduleCreate,
    session: Session = Depends(get_session),
) -> ScheduleRead:
    _get_server(server_id, session)
    _validate(payload.action.value, payload.cron, payload.command)
    schedule = Schedule(
        server_id=server_id,
        action=payload.action.value,
        cron=payload.cron,
        command=payload.command,
        one_time=payload.one_time,
        enabled=payload.enabled,
    )
    session.add(schedule)
    session.commit()
    session.refresh(schedule)
    scheduler_service.sync(schedule)
    return _read(schedule)


@router.patch("/{schedule_id}", response_model=ScheduleRead)
def update(
    server_id: str,
    schedule_id: str,
    payload: ScheduleUpdate,
    session: Session = Depends(get_session),
) -> ScheduleRead:
    schedule = _get_schedule(server_id, schedule_id, session)
    updates = payload.model_dump(exclude_unset=True)
    if "action" in updates:
        updates["action"] = updates["action"].value
    merged = {**schedule.model_dump(), **updates}
    _validate(merged["action"], merged["cron"], merged["command"])
    for key, value in updates.items():
        setattr(schedule, key, value)
    session.add(schedule)
    session.commit()
    session.refresh(schedule)
    scheduler_service.sync(schedule)
    return _read(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    server_id: str, schedule_id: str, session: Session = Depends(get_session)
) -> None:
    schedule = _get_schedule(server_id, schedule_id, session)
    scheduler_service.remove(schedule.id)
    session.delete(schedule)
    session.commit()
