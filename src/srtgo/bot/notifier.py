"""봇이 사용자에게 푸시 메시지 보낼 때 쓰는 헬퍼.

핸들러는 telegram Update가 있어 reply_text를 쓰면 되지만,
폴링 콜백처럼 update가 없는 경로에서는 이 모듈로 보낸다.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def _payment_deadline_str(reservation: Any) -> str | None:
    """코레일 예약의 결제 기한을 표시합니다."""
    pd = reservation.buy_limit_date
    pt = reservation.buy_limit_time
    if not pd or not pt or pd == "00000000":
        return None
    try:
        return f"{int(pd[4:6])}/{int(pd[6:8])} {pt[:2]}:{pt[2:4]}"
    except (ValueError, IndexError):
        return None


def format_seat_secured_message(reservation: Any) -> str:
    deadline = _payment_deadline_str(reservation)
    base = f"{'예약대기 등록' if reservation.is_waiting else '좌석 확보!'}\n{reservation}"
    if deadline:
        base += f"\n결제마감: {deadline}"
    return base


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 결제", callback_data="pay:confirm"),
                InlineKeyboardButton("❌ 취소", callback_data="pay:cancel"),
            ]
        ]
    )


async def send_seat_secured(bot: Bot, telegram_id: int, reservation: Any) -> int:
    msg = await bot.send_message(
        chat_id=telegram_id,
        text=format_seat_secured_message(reservation),
        reply_markup=(
            InlineKeyboardMarkup([[InlineKeyboardButton("취소", callback_data="pay:cancel")]])
            if reservation.is_waiting
            else confirm_keyboard()
        ),
    )
    return msg.message_id


async def send_text(bot: Bot, telegram_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
    except Exception as e:
        logger.error("푸시 실패 tid=%d: %s", telegram_id, type(e).__name__)
