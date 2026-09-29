"""불확실한 원격 쓰기의 재전송 방지. 자격증명은 기록하지 않습니다."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path


class UncertainOperationError(Exception):
    """앱에서 확인하기 전 추가 예약·결제를 할 수 없는 상태."""


def _path() -> Path:
    directory = Path(os.environ.get("BOT_USERS_DIR", "data/users")).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "operations.sqlite3"
    path.touch(mode=0o600, exist_ok=True)
    return path


def initialize() -> None:
    with closing(sqlite3.connect(_path())) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS operations (owner INTEGER PRIMARY KEY, "
            "account TEXT NOT NULL UNIQUE, state TEXT NOT NULL, reservation TEXT NOT NULL)"
        )


def account_key(membership: str) -> str:
    return hashlib.sha256(membership.encode("utf-8")).hexdigest()


def current(owner: int) -> tuple[str, str] | None:
    with closing(sqlite3.connect(_path())) as conn:
        return conn.execute("SELECT state, reservation FROM operations WHERE owner=?", (owner,)).fetchone()


def begin(owner: int, account: str, state: str, reservation: str = "") -> None:
    with closing(sqlite3.connect(_path())) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner, state, reservation, account FROM operations WHERE owner=? OR account=?", (owner, account)
        ).fetchone()
        if row is not None and not (
            state in {"PAYING", "CANCELLING"} and row == (owner, "RESERVED", reservation, account) and reservation
        ):
            raise UncertainOperationError("확인 대기 중인 예약·결제가 있습니다. /status로 확인하세요.")
        if row is None and state != "RESERVING":
            raise UncertainOperationError("이 봇이 확보한 예약인지 확인할 수 없습니다.")
        conn.execute(
            "INSERT INTO operations VALUES (?, ?, ?, ?) ON CONFLICT(owner) DO UPDATE SET "
            "state=excluded.state, reservation=excluded.reservation",
            (owner, account, state, reservation),
        )


def reserved(owner: int, reservation: str) -> None:
    with closing(sqlite3.connect(_path())) as conn, conn:
        conn.execute("UPDATE operations SET state='RESERVED', reservation=? WHERE owner=?", (reservation, owner))


def uncertain(owner: int) -> None:
    with closing(sqlite3.connect(_path())) as conn, conn:
        conn.execute("UPDATE operations SET state='UNKNOWN' WHERE owner=?", (owner,))


def finish(owner: int) -> None:
    with closing(sqlite3.connect(_path())) as conn, conn:
        conn.execute("DELETE FROM operations WHERE owner=?", (owner,))


def recover() -> None:
    with closing(sqlite3.connect(_path())) as conn, conn:
        conn.execute("UPDATE operations SET state='UNKNOWN' WHERE state != 'RESERVED'")


def acquire_instance() -> sqlite3.Connection:
    """프로세스 종료 시 OS가 자동 해제하는 단일 실행 잠금."""
    path = _path().with_name("instance.sqlite3")
    path.touch(mode=0o600, exist_ok=True)
    conn = sqlite3.connect(path, timeout=0)
    try:
        conn.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError:
        conn.close()
        raise RuntimeError("같은 저장소의 봇이 이미 실행 중입니다.") from None
    return conn
