"""서버 환경변수 설정. 잘못된 설정은 시작 전에 거부합니다."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pykorail_bot.domain import BotError

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    key: str = field(repr=False)
    allowed: frozenset[int]
    directory: Path
    interval: float = 30
    max_minutes: int = 120
    payments: bool = False
    max_payment: int = 0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        try:
            token, key = values["BOT_TOKEN"].strip(), values["BOT_DB_KEY"].strip()
            allowed = frozenset(int(x.strip()) for x in values["BOT_ALLOWED_IDS"].split(","))
            interval = float(values.get("BOT_POLL_SECONDS", "30"))
            minutes = int(values.get("BOT_MAX_MINUTES", "120"))
            payment_value = values.get("BOT_ENABLE_PAYMENTS", "false").lower()
            limit = int(values.get("BOT_MAX_PAYMENT_WON", "0"))
            if not token or not key or not allowed or min(allowed) <= 0:
                raise ValueError
            if not math.isfinite(interval) or not 10 <= interval <= 3600 or not 1 <= minutes <= 1440:
                raise ValueError
            if payment_value not in {"true", "false"} or limit < 0 or (payment_value == "true" and limit <= 0):
                raise ValueError
        except (KeyError, ValueError):
            raise BotError("봇 환경변수를 확인하세요. docs/telegram-bot.md의 설정 범위를 참고하세요.") from None
        return cls(
            token,
            key,
            allowed,
            Path(values.get("BOT_DATA_DIR", "bot-data")),
            interval,
            minutes,
            payment_value == "true",
            limit,
        )
