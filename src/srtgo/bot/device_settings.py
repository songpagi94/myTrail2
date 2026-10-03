"""사용자별 기기 ID 설정 대화."""

from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from pykorail.device.android_id import validate_android_id
from srtgo.bot import storage
from srtgo.bot.access import BotContext, BotUpdate, erase_input, guarded
from srtgo.service import journal

MENU, INPUT = range(30, 32)


def _busy(owner: int) -> bool:
    from srtgo.bot.handlers import _SESSION

    return _SESSION.is_polling(owner) or journal.current(owner) is not None


async def _apply(owner: int, context: BotContext, value: str | None) -> None:
    storage.set_android_id(owner, value)
    # 이미 로그인한 조회 세션은 이전 ID를 쓰므로 새 조회에서 다시 연결합니다.
    search = context.user_data.pop("search", None)
    context.user_data.pop("pending_indices", None)
    context.user_data.pop("device_settings_message", None)
    if search is not None:
        await asyncio.to_thread(search["rail"].close)


@guarded
async def entry(update: BotUpdate, context: BotContext) -> int:
    """기본값 복원과 직접 입력 버튼을 표시합니다."""
    if _busy(update.effective_user.id):
        await update.message.reply_text("예약·결제 진행 또는 확인 대기 중입니다. /cancel 또는 /status를 확인하세요.")
        return ConversationHandler.END
    storage.android_id_for(update.effective_user.id)
    sent = await update.message.reply_text(
        "기기 ID 설정 방법을 선택하세요. 변경은 다음 로그인부터 적용됩니다.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("기본값 사용", callback_data="dev:default")],
                [InlineKeyboardButton("dev_id 임의 입력", callback_data="dev:custom")],
            ]
        ),
    )
    context.user_data["device_settings_message"] = sent.message_id
    return MENU


@guarded
async def choose(update: BotUpdate, context: BotContext) -> int:
    """현재 설정 화면의 선택만 처리합니다."""
    query = update.callback_query
    message_id = query.message.message_id if query.message is not None else None
    if message_id is None or context.user_data.get("device_settings_message") != message_id:
        await query.answer("오래된 설정 버튼입니다. /dev_set을 다시 입력하세요.", show_alert=True)
        return MENU
    await query.answer()
    if _busy(update.effective_user.id):
        context.user_data.pop("device_settings_message", None)
        await query.edit_message_text("예약·결제 진행 또는 확인 대기 중에는 기기 ID를 변경할 수 없습니다.")
        return ConversationHandler.END
    if query.data == "dev:default":
        await _apply(update.effective_user.id, context, None)
        await query.edit_message_text("처음 생성한 기본 기기 ID로 복원했습니다. 새 조회부터 적용됩니다.")
        return ConversationHandler.END
    await query.edit_message_text("dev_id를 입력하세요. 소문자 16진수 16자리여야 합니다.\n입력 취소: /cancel")
    return INPUT


@guarded
async def receive(update: BotUpdate, context: BotContext) -> int:
    """입력 형식을 검증하고 사용자 암호화 저장소에 반영합니다."""
    value = (update.message.text or "").strip()
    await erase_input(update)
    if _busy(update.effective_user.id):
        context.user_data.pop("device_settings_message", None)
        await update.message.reply_text("예약·결제 진행 또는 확인 대기 중에는 기기 ID를 변경할 수 없습니다.")
        return ConversationHandler.END
    try:
        validate_android_id(value)
    except ValueError:
        await update.message.reply_text("소문자 a~f와 숫자 0~9로 된 16자리를 입력하세요. 취소: /cancel")
        return INPUT
    await _apply(update.effective_user.id, context, value)
    await update.message.reply_text("사용자 지정 기기 ID를 저장했습니다. 새 조회부터 적용됩니다.")
    return ConversationHandler.END


@guarded
async def cancel(update: BotUpdate, context: BotContext) -> int:
    """기기 ID를 변경하지 않고 설정 입력을 종료합니다."""
    context.user_data.pop("device_settings_message", None)
    await update.message.reply_text("기기 ID 설정을 취소했습니다.")
    return ConversationHandler.END
