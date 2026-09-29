"""암호화된 자격증명과 중복 실행 방지용 SQLite 작업 기록."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cryptography.fernet import Fernet, InvalidToken

from pykorail import Card
from pykorail_bot.domain import TERMINAL, BotError, JobRecord

if TYPE_CHECKING:
    from pykorail_bot.domain import State


@dataclass(frozen=True)
class Credential:
    """개인키·비밀번호·카드 원문은 repr에 포함하지 않습니다."""

    membership: str = field(repr=False)
    password: str = field(repr=False)
    profile: str
    card: Card | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # 같은 계정을 이메일/휴대폰으로 중복 등록해 잠금을 우회하지 않도록 회원번호만 받습니다.
        if (
            len(self.membership) != 10
            or not self.membership.isascii()
            or not self.membership.isdigit()
            or not self.password
        ):
            raise BotError("코레일 회원번호 10자리와 비밀번호가 필요합니다.")

    @property
    def account(self) -> str:
        return hashlib.sha256(self.membership.encode("utf-8")).hexdigest()


class Store:
    """생성자는 파일을 열지 않습니다. initialize 후 사용합니다."""

    def __init__(self, directory: Path, key: str) -> None:
        self.directory = directory
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise BotError("BOT_DB_KEY가 유효한 Fernet 키가 아닙니다.") from None
        self._lease: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.directory / "users").mkdir(mode=0o700, exist_ok=True)
        path = self.directory / "jobs.sqlite3"
        path.touch(mode=0o600, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner INTEGER NOT NULL, "
                "account TEXT NOT NULL, state TEXT NOT NULL, note TEXT NOT NULL, reservation_id TEXT NOT NULL, "
                "details TEXT NOT NULL DEFAULT '')"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "details" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN details TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_account ON jobs(account) "
                "WHERE state NOT IN ('PAID', 'STOPPED', 'FAILED', 'CLOSED')"
            )

    def acquire(self) -> None:
        """같은 저장소의 이중 실행을 막습니다. 비정상 종료 시에도 OS가 잠금을 해제합니다."""
        if self._lease is not None:
            raise BotError("이미 실행 잠금을 보유하고 있습니다.")
        path = self.directory / "instance.sqlite3"
        path.touch(mode=0o600, exist_ok=True)
        conn = sqlite3.connect(path, timeout=0)
        try:
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            conn.close()
            raise BotError("같은 BOT_DATA_DIR을 사용하는 봇이나 설정 프로그램이 이미 실행 중입니다.") from None
        self._lease = conn

    def close(self) -> None:
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def _user_path(self, owner: int) -> Path:
        if owner <= 0:
            raise BotError("올바른 텔레그램 사용자 ID가 필요합니다.")
        return self.directory / "users" / f"{owner}.enc"

    def save_credential(self, owner: int, credential: Credential) -> None:
        """암호문만 원자적으로 교체합니다. 중간 파일도 평문을 담지 않습니다."""
        target = self._user_path(owner)
        payload = self.cipher.encrypt(json.dumps(asdict(credential), ensure_ascii=False).encode("utf-8"))
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(payload)
        try:
            Path(temporary).replace(target)
        finally:
            # 원자적 교체에 성공하면 임시 파일은 존재하지 않습니다.
            Path(temporary).unlink(missing_ok=True)

    def has_credential(self, owner: int) -> bool:
        return self._user_path(owner).exists()

    def credential(self, owner: int) -> Credential:
        path = self._user_path(owner)
        if not path.exists():
            raise BotError("서버에서 pykorail-bot setup --user 사용자ID로 먼저 등록하세요.")
        try:
            data = json.loads(self.cipher.decrypt(path.read_bytes()).decode("utf-8"))
            raw_card = data.pop("card", None)
            return Credential(**data, card=Card(**raw_card) if raw_card else None)
        except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
            raise BotError("저장된 정보를 읽지 못했습니다. 암호화 키와 데이터 파일을 확인하세요.") from None

    def save(self, record: JobRecord) -> None:
        with closing(sqlite3.connect(self.directory / "jobs.sqlite3")) as conn, conn:
            conn.execute(
                "INSERT INTO jobs (id, owner, account, state, note, reservation_id, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "state=excluded.state, note=excluded.note, "
                "reservation_id=excluded.reservation_id, details=excluded.details",
                (
                    record.id,
                    record.owner,
                    record.account,
                    record.state,
                    record.note,
                    record.reservation_id,
                    record.details,
                ),
            )

    def records(self) -> list[JobRecord]:
        with closing(sqlite3.connect(self.directory / "jobs.sqlite3")) as conn:
            rows = conn.execute(
                "SELECT id, owner, account, state, note, reservation_id, details FROM jobs ORDER BY rowid"
            ).fetchall()
        return [JobRecord(row[0], row[1], row[2], cast("State", row[3]), row[4], row[5], row[6]) for row in rows]

    def get(self, owner: int, job_id: str) -> JobRecord:
        record = next((r for r in self.records() if r.id == job_id and r.owner == owner), None)
        if record is None:
            raise BotError("이 사용자에게 해당 작업이 없거나 버튼이 만료됐습니다.")
        return record

    def active(self, account: str) -> JobRecord | None:
        return next((r for r in self.records() if r.account == account and r.state not in TERMINAL), None)

    def recover(self) -> None:
        """재시작 후 자동 재예약·재결제를 하지 않고 확인 가능한 상태로 남깁니다."""
        for record in self.records():
            if record.state == "WATCHING":
                self.save(
                    replace(record, state="STOPPED", note="재시작으로 반복 조회가 중단됐습니다. 새 요청이 필요합니다.")
                )
            elif record.state in {"RESERVING", "PAYING"}:
                self.save(
                    replace(
                        record,
                        state="UNKNOWN",
                        note="재시작 전 요청 결과가 불확실합니다. 앱에서 예약·발권을 확인하세요.",
                    )
                )
