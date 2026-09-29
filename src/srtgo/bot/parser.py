"""고정 형식 텍스트를 intent dict로 파싱."""

from __future__ import annotations

import datetime
import logging
import re

from ..rail.ktx.client import KST

logger = logging.getLogger(__name__)

_SEAT_ALIAS: dict[str, str] = {
    "일반만": "GENERAL_ONLY",
    "일반우선": "GENERAL_FIRST",
    "일반": "GENERAL_ONLY",
    "특실만": "SPECIAL_ONLY",
    "특실우선": "SPECIAL_FIRST",
    "특실": "SPECIAL_ONLY",
}

_PASSENGER_ALIAS: dict[str, str] = {
    "어린이": "child",
    "경로": "senior",
    "중증장애인": "disability1to3",
    "중증": "disability1to3",
    "경증장애인": "disability4to6",
    "경증": "disability4to6",
    "유아": "toddler",
}

# KTX 역명 별칭 (코레일 API는 한국어 역명을 직접 수신)
_KTX_ALIAS: dict[str, str] = {}


class ParseError(Exception):
    """파싱 실패."""


def _normalize_date(s: str, today: str | None = None) -> str:
    """YYYYMMDD / YYYY-MM-DD / MM/DD → YYYY-MM-DD"""
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{1,2}/\d{1,2}", s):
        m, d = s.split("/")
        year = datetime.date.fromisoformat(today).year if today else datetime.datetime.now(KST).year
        return f"{year}-{int(m):02d}-{int(d):02d}"
    raise ParseError(f"날짜 형식 오류: '{s}' — YYYYMMDD, YYYY-MM-DD, 또는 MM/DD")


def _normalize_time(s: str) -> str:
    """HHMM / HH:MM → HHMMSS"""
    clean = s.replace(":", "")
    if re.fullmatch(r"\d{4}", clean):
        return clean + "00"
    if re.fullmatch(r"\d{6}", clean):
        return clean
    raise ParseError(f"시간 형식 오류: '{s}' — HHMM 또는 HH:MM")


def _normalize_station(name: str, rail: str) -> str:
    return _KTX_ALIAS.get(name, name)


def _parse_rail(tok: str) -> str | None:
    upper = tok.upper()
    if upper in ("KTX", "코레일"):
        return "KTX"
    return None


def parse(text: str, today: str | None = None, **_kwargs) -> dict:
    """고정 형식 텍스트 → intent dict.

    형식: 출발역 도착역 날짜(YYYYMMDD) 시간(HHMM) [KTX] [좌석옵션] [승객유형]
    예:  서울 부산 20260515 1400
         서울 부산 20260515 1400 KTX 특실우선
         서울 부산 20260515 1400 KTX 어린이
    """
    tokens = text.strip().split()
    if len(tokens) < 4:
        raise ParseError(
            "입력 형식: 출발역 도착역 날짜(YYYYMMDD) 시간(HHMM) [KTX] [좌석옵션] [승객유형]\n"
            "예: 서울 부산 20260515 1400"
        )

    dep_raw, arr_raw, date_raw, time_raw = tokens[:4]

    date = _normalize_date(date_raw, today)
    time_val = _normalize_time(time_raw)
    try:
        datetime.datetime.strptime(date + time_val, "%Y-%m-%d%H%M%S")
    except ValueError:
        raise ParseError("유효하지 않은 날짜 또는 시간입니다.") from None
    if dep_raw == arr_raw:
        raise ParseError("출발역과 도착역을 다르게 입력하세요.")

    rail = "KTX"
    seat_pref = "GENERAL_ONLY"
    passenger_type = None
    for tok in tokens[4:]:
        r = _parse_rail(tok)
        if r:
            rail = r
        elif tok in _SEAT_ALIAS:
            seat_pref = _SEAT_ALIAS[tok]
        elif tok in _PASSENGER_ALIAS:
            passenger_type = _PASSENGER_ALIAS[tok]
        else:
            raise ParseError(
                f"'{tok}' 미인식 — 사용 가능: KTX, "
                "일반만, 일반우선, 특실만, 특실우선, "
                "어린이, 유아, 경로, 중증장애인, 경증장애인"
            )

    dep = _normalize_station(dep_raw, rail)
    arr = _normalize_station(arr_raw, rail)

    passengers = {"adult": 1, "child": 0, "senior": 0, "disability1to3": 0, "disability4to6": 0, "toddler": 0}
    if passenger_type:
        passengers["adult"] = 0
        passengers[passenger_type] = 1

    return {
        "rail": rail,
        "dep": dep,
        "arr": arr,
        "date": date,
        "time": time_val,
        "passengers": passengers,
        "seat_pref": seat_pref,
        "needs_clarification": [],
    }
