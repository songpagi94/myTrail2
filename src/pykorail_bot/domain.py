"""봇 입력과 작업 상태. 철도 API 응답 모델과 분리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from pykorail import AdultPassenger, ChildPassenger, ReserveOption, SeniorPassenger

if TYPE_CHECKING:
    from pykorail import Passenger, Train
    from pykorail.options import ReserveOptionCode

KST = timezone(timedelta(hours=9))
State = Literal[
    "WATCHING", "RESERVING", "RESERVED", "WAITLIST", "PAYING", "PAID", "UNKNOWN", "STOPPED", "FAILED", "CLOSED"
]
TERMINAL = frozenset({"PAID", "STOPPED", "FAILED", "CLOSED"})
SEAT_OPTIONS: dict[str, ReserveOptionCode] = {
    "일반만": ReserveOption.GENERAL_ONLY,
    "일반우선": ReserveOption.GENERAL_FIRST,
    "특실만": ReserveOption.SPECIAL_ONLY,
    "특실우선": ReserveOption.SPECIAL_FIRST,
}


class BotError(Exception):
    """민감정보 없이 사용자에게 그대로 보여줄 수 있는 봇 오류."""


def now_kst() -> datetime:
    """서버 시간대에 무관하게 한국시간을 반환합니다."""
    return datetime.now(KST)


@dataclass(frozen=True)
class Query:
    """사용자가 확인할 조회 조건."""

    departure: str
    arrival: str
    after: datetime
    seat: ReserveOptionCode = ReserveOption.GENERAL_ONLY
    adults: int = 1
    children: int = 0
    seniors: int = 0

    def passengers(self) -> tuple[Passenger, ...]:
        return tuple(
            kind(count)
            for kind, count in (
                (AdultPassenger, self.adults),
                (ChildPassenger, self.children),
                (SeniorPassenger, self.seniors),
            )
            if count
        )

    def summary(self) -> str:
        return (
            f"{self.departure} → {self.arrival} | {self.after:%Y-%m-%d %H:%M} 이후 KST\n"
            f"{self.seat} | 성인 {self.adults}, 어린이 {self.children}, 경로 {self.seniors}"
        )


def parse_query(text: str, *, now: datetime | None = None) -> Query:
    """고정 형식 입력을 검증합니다. 날짜·역을 추측하거나 자동 보정하지 않습니다."""
    parts = text.split()
    if len(parts) < 4 or len(parts) > 9:
        raise BotError(
            "입력: 출발역 도착역 YYYYMMDD HHMM [KTX] [일반만|일반우선|특실만|특실우선] [성인1 어린이0 경로0]"
        )
    departure, arrival, day, clock = parts[:4]
    if len(departure) > 30 or len(arrival) > 30 or departure == arrival:
        raise BotError("출발역과 도착역을 서로 다르게 입력하세요.")
    try:
        if len(day) != 8 or len(clock) != 4 or not (day + clock).isascii() or not (day + clock).isdigit():
            raise ValueError
        moment = datetime.strptime(day + clock, "%Y%m%d%H%M").replace(tzinfo=KST)
    except ValueError:
        raise BotError("날짜와 시각을 확인하세요. 예: 20261003 0900") from None
    if moment <= (now or now_kst()):
        raise BotError("미래의 출발 날짜·시각을 한국시간으로 입력하세요.")
    seat = ReserveOption.GENERAL_ONLY
    counts = {"성인": 1, "어린이": 0, "경로": 0}
    seen: set[str] = set()
    for part in parts[4:]:
        if part.upper() == "KTX":
            key = "KTX"
        elif part in SEAT_OPTIONS:
            key = "좌석"
            seat = SEAT_OPTIONS[part]
        else:
            key = next((name for name in counts if part.startswith(name)), "")
            suffix = part[len(key) :]
            if not key or not suffix.isascii() or not suffix.isdigit() or len(suffix) > 1:
                raise BotError("KTX와 지원하는 좌석·승객 옵션만 입력하세요.")
            counts[key] = int(suffix)
        if key in seen:
            raise BotError("같은 옵션을 두 번 지정할 수 없습니다.")
        seen.add(key)
    if not 1 <= sum(counts.values()) <= 9:
        raise BotError("승객은 합계 1~9명으로 입력하세요.")
    return Query(departure, arrival, moment, seat, counts["성인"], counts["어린이"], counts["경로"])


@dataclass(frozen=True)
class TrainKey:
    """목록 순번이 바뀌어도 같은 열차·같은 구간만 선택합니다."""

    date: str
    number: str
    departure: str
    arrival: str
    time: str
    group: str

    @classmethod
    def of(cls, train: Train) -> TrainKey:
        key = cls(train.dep_date, train.train_no, train.dep_code, train.arr_code, train.dep_time, train.train_group)
        if not all((key.date, key.number, key.departure, key.arrival, key.time, key.group)):
            raise BotError("열차 식별정보가 불완전하여 예약할 수 없습니다.")
        return key

    def departure_time(self) -> datetime:
        return datetime.strptime(self.date + self.time, "%Y%m%d%H%M%S").replace(tzinfo=KST)


def seat_available(train: Train, option: ReserveOptionCode) -> bool:
    """첫 버전은 실좌석만 확보합니다. 코레일 예약대기에 자동 등록하지 않습니다."""
    if option == ReserveOption.GENERAL_ONLY:
        return train.has_general_seat()
    if option == ReserveOption.SPECIAL_ONLY:
        return train.has_special_seat()
    return train.has_seat()


@dataclass(frozen=True)
class JobRecord:
    """재시작해도 중복 예약·결제를 막기 위해 디스크에 남기는 최소 상태."""

    id: str
    owner: int
    account: str
    state: State
    note: str
    reservation_id: str = ""
    details: str = ""
