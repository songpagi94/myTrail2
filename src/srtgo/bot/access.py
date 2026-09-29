"""텔레그램 경계의 허용 사용자 검사와 입력 메시지 최소 보관."""

from __future__ import annotations

import logging
from functools import wraps
from typing import TYPE_CHECKING, Any, Protocol, cast

from telegram import Update as TelegramUpdate
from telegram.ext import ContextTypes, ConversationHandler

from . import auth_guard

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram import Bot, CallbackQuery, Message, User
    from telegram.ext import Application

logger = logging.getLogger(__name__)


class BotUpdate(Protocol):
    """guarded에서 검증한 업데이트. 명령은 message, 콜백은 callback_query를 사용합니다."""

    effective_user: User
    message: Message
    callback_query: CallbackQuery


class BotContext(Protocol):
    """사용자 데이터가 있는 개인 대화의 Context 경계."""

    user_data: dict[str, Any]
    bot: Bot
    application: Application


def guarded(function: Callable[[BotUpdate, BotContext], Awaitable[Any]]) -> Callable:
    """모든 등록 단계·콜백에서도 허용 사용자와 개인 대화를 검증합니다."""

    @wraps(function)
    async def wrapper(update: TelegramUpdate, context: ContextTypes.DEFAULT_TYPE) -> Any:
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None:
            return ConversationHandler.END
        if chat.type != "private" or chat.id != user.id or not auth_guard.is_allowed(user.id):
            message = (
                "허용된 사용자의 개인 대화에서만 사용하세요.\n"
                f"사용자 ID: {user.id}\n"
                "이 ID를 관리자에게 전달해 허용 목록 등록을 요청하세요."
            )
            if update.callback_query is not None:
                await update.callback_query.answer(message, show_alert=True)
            elif update.effective_message is not None:
                await update.effective_message.reply_text(message)
            return ConversationHandler.END
        if context.user_data is None:
            return ConversationHandler.END
        if update.callback_query is not None and not isinstance(update.callback_query.data, str):
            return ConversationHandler.END
        if update.callback_query is not None:
            callback = update.callback_query
            kind = str(callback.data).split(":", 1)[0]
            if kind in {"page", "pick", "preset", "pay"}:
                from .handlers import _SESSION

                record = _SESSION.get_pending(user.id) if kind == "pay" else context.user_data.get("search")
                message_id = callback.message.message_id if callback.message is not None else None
                if record is None or record.get("message_id") != message_id:
                    await callback.answer("오래된 버튼입니다. /status 또는 새 조회를 사용하세요.", show_alert=True)
                    return ConversationHandler.END
        try:
            return await function(cast("BotUpdate", update), cast("BotContext", context))
        except Exception as error:
            # Update와 예외 원문은 비밀번호·카드·토큰을 포함할 수 있습니다.
            logger.error("봇 처리 실패: %s", type(error).__name__)
            await context.bot.send_message(user.id, "요청을 처리하지 못했습니다. /status로 확인하세요.")
            context.user_data.pop("setup", None)
            context.user_data.pop("cards_new", None)
            context.user_data.pop("cards_edit", None)
            return ConversationHandler.END

    return wrapper


async def erase_input(update: BotUpdate) -> None:
    """입력 원문 삭제를 시도합니다. 삭제해도 전송·저장의 모든 흔적 제거를 보장하지 않습니다."""
    try:
        await update.message.delete()
    except Exception:
        await update.message.reply_text("입력 메시지 자동 삭제에 실패했습니다. 직접 삭제해주세요.")
