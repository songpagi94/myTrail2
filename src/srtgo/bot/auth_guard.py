"""텔레그램 사용자 ID 화이트리스트."""

from __future__ import annotations

import os


def get_allowed_ids() -> set[int]:
    raw = os.environ.get("BOT_ALLOWED_IDS", "")
    out = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def is_allowed(telegram_id: int) -> bool:
    return telegram_id in get_allowed_ids()
