"""사용자별 요청 신원 설정과 현재 값 표시."""

from __future__ import annotations

import asyncio
from typing import Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from srtgo.bot import storage
from srtgo.bot.access import BotContext, BotUpdate, erase_input, guarded
from srtgo.service import journal

MENU, INPUT, FIELDS, FIELD = range(30, 34)
LABELS: Final = {
    "device_model": "Device model",
    "os_version": "OS version",
    "os_type": "OS type",
    "sdk_version": "SDK version",
    "app_version": "APP version",
    "sid_key": "Sid key",
    "device_id": "Device ID",
}


def _busy(owner: int) -> bool:
    from srtgo.bot.handlers import _SESSION

    return _SESSION.is_polling(owner) or journal.current(owner) is not None


def _values_text(owner: int) -> str:
    values = storage.request_values_for(owner)
    return "현재 사용자 설정\n\n" + "\n".join(f"{label}: {values[key]}" for key, label in LABELS.items())


def _keyboard(state: int, field: str | None = None) -> InlineKeyboardMarkup:
    if state == MENU:
        rows = [("1. 편집하기", "dev:fields"), ("2. 취소", "dev:cancel")]
    elif state == FIELDS:
        rows = [(label, f"dev:field:{key}") for key, label in LABELS.items()]
        rows.append(("돌아가기", "dev:menu"))
    elif state == INPUT:
        rows = [("돌아가기", f"dev:field:{field}")]
    else:
        rows = [("수동 편집", f"dev:manual:{field}")]
        if field == "device_id":
            rows.append(("자동 재생성", f"dev:regen:{field}"))
        rows.extend([("기본값으로 복원", f"dev:reset:{field}"), ("돌아가기", "dev:fields")])
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)] for label, data in rows])


def _screen(owner: int, state: int, field: str | None = None, notice: str = "") -> str:
    text = _values_text(owner)
    if field is not None:
        text += f"\n\n편집 항목: {LABELS[field]}"
    if state == INPUT:
        hint = (
            "소문자 16진수 16자리"
            if field == "device_id"
            else "ASCII 문자 16자리 (16진수 바이트 변환 없이 그대로 사용)"
            if field == "sid_key"
            else "영숫자와 . _ -로 된 1~16자리"
            if field == "sdk_version"
            else "출력 가능한 ASCII 문자 1~32자리"
        )
        text += f"\n새 값을 입력하세요: {hint}\n/cancel로 설정을 종료할 수 있습니다."
    else:
        text += "\n\n변경은 새 조회부터 적용됩니다."
    return text + (f"\n\n{notice}" if notice else "")


def _remember(context: BotContext, state: int, field: str | None = None) -> None:
    context.user_data["device_settings_state"] = state
    context.user_data["device_settings_field"] = field


def _clear(context: BotContext) -> None:
    for key in ("device_settings_message", "device_settings_state", "device_settings_field"):
        context.user_data.pop(key, None)


async def _apply(owner: int, context: BotContext, field: str, value: str | None, *, regenerate: bool = False) -> None:
    storage.set_request_value(owner, field, value, regenerate=regenerate)
    # 로그인한 세션에 설정을 섞지 않고 새 조회에서 함께 적용합니다.
    search = context.user_data.pop("search", None)
    context.user_data.pop("pending_indices", None)
    if search is not None:
        await asyncio.to_thread(search["rail"].close)


@guarded
async def entry(update: BotUpdate, context: BotContext) -> int:
    """현재 값과 편집·취소 버튼을 표시합니다."""
    if _busy(update.effective_user.id):
        await update.message.reply_text("예약·결제 진행 또는 확인 대기 중입니다. /cancel 또는 /status를 확인하세요.")
        return ConversationHandler.END
    sent = await update.message.reply_text(_screen(update.effective_user.id, MENU), reply_markup=_keyboard(MENU))
    context.user_data["device_settings_message"] = sent.message_id
    _remember(context, MENU)
    return MENU


@guarded
async def choose(update: BotUpdate, context: BotContext) -> int:
    """현재 설정 화면의 선택만 처리합니다."""
    query = update.callback_query
    state = context.user_data.get("device_settings_state", MENU)
    message_id = query.message.message_id if query.message is not None else None
    if message_id is None or context.user_data.get("device_settings_message") != message_id:
        await query.answer("오래된 설정 버튼입니다. /dev_set을 다시 입력하세요.", show_alert=True)
        return state
    await query.answer()
    owner = update.effective_user.id
    if _busy(owner):
        _clear(context)
        await query.edit_message_text("예약·결제 진행 또는 확인 대기 중에는 설정을 변경할 수 없습니다.")
        return ConversationHandler.END
    assert isinstance(query.data, str)
    action, _, field = query.data.removeprefix("dev:").partition(":")
    if action == "cancel":
        _clear(context)
        await query.edit_message_text(_values_text(owner) + "\n\n설정을 종료했습니다.")
        return ConversationHandler.END
    notice = ""
    if action in {"menu", "fields"}:
        state = MENU if action == "menu" else FIELDS
        selected = None
    elif field in LABELS and action in {"field", "manual", "reset", "regen"}:
        selected = field
        state = INPUT if action == "manual" else FIELD
        if action in {"reset", "regen"}:
            await _apply(owner, context, field, None, regenerate=action == "regen")
            notice = "자동 재생성했습니다." if action == "regen" else "기본값으로 복원했습니다."
    else:
        return state
    _remember(context, state, selected)
    await query.edit_message_text(_screen(owner, state, selected, notice), reply_markup=_keyboard(state, selected))
    return state


@guarded
async def receive(update: BotUpdate, context: BotContext) -> int:
    """입력값을 검증·저장하고 갱신된 현재 값과 항목 버튼을 표시합니다."""
    value = (update.message.text or "").strip()
    await erase_input(update)
    owner = update.effective_user.id
    if _busy(owner):
        _clear(context)
        await update.message.reply_text("예약·결제 진행 또는 확인 대기 중에는 설정을 변경할 수 없습니다.")
        return ConversationHandler.END
    field = context.user_data.get("device_settings_field")
    if field not in LABELS:
        _clear(context)
        await update.message.reply_text("설정 입력이 만료되었습니다. /dev_set을 다시 입력하세요.")
        return ConversationHandler.END
    try:
        await _apply(owner, context, field, value)
    except (ValueError, UnicodeError):
        sent = await update.message.reply_text(
            _screen(owner, INPUT, field, "입력 형식이 맞지 않습니다. 저장하지 않았습니다."),
            reply_markup=_keyboard(INPUT, field),
        )
        context.user_data["device_settings_message"] = sent.message_id
        return INPUT
    sent = await update.message.reply_text(
        _screen(owner, FIELD, field, "저장했습니다."),
        reply_markup=_keyboard(FIELD, field),
    )
    context.user_data["device_settings_message"] = sent.message_id
    _remember(context, FIELD, field)
    return FIELD


@guarded
async def cancel(update: BotUpdate, context: BotContext) -> int:
    """입력 중인 값을 저장하지 않고 설정 화면을 종료합니다."""
    _clear(context)
    await update.message.reply_text("설정을 종료했습니다. 이미 저장한 변경은 유지됩니다.")
    return ConversationHandler.END
