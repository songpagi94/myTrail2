"""myTrail의 고정 형식 입력·페이지·버튼 UX를 KTX 전용으로 연결합니다."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from pykorail_bot.domain import BotError, parse_query
from pykorail_bot.jobs import Controller, safe_error

if TYPE_CHECKING:
    from telegram import Bot, Update
    from telegram.ext import ContextTypes

    from pykorail_bot.config import Settings
    from pykorail_bot.domain import JobRecord
    from pykorail_bot.jobs import Search
    from pykorail_bot.storage import Store

logger = logging.getLogger(__name__)
PAGE_SIZE = 5
HELP = (
    "KTX 조회: 출발역 도착역 YYYYMMDD HHMM [좌석옵션] [성인1 어린이0 경로0]\n"
    "예: 부산 서울 20261003 0900 일반우선\n"
    "좌석옵션: 일반만(기본), 일반우선, 특실만, 특실우선\n"
    "한국시간 기준이며 매진 열차도 표시합니다. 열차 선택 후 시작을 확인해야 예약을 시도합니다.\n\n"
    "/status — 작업 상태\n/check 작업ID — 예약·발권 재확인\n"
    "/stop 또는 /cancel — 반복 시도만 중단 (예약·승차권 취소 아님)\n"
    "/setup — 서버 등록 방법\n/help — 도움말\n"
    "비밀번호·카드번호를 텔레그램에 보내지 마세요. 자동결제·환불은 지원하지 않습니다."
)


def job_text(record: JobRecord) -> str:
    return f"작업 {record.id} [{record.state}]\n{record.details}\n{record.note}"


def job_buttons(record: JobRecord, payments: bool) -> InlineKeyboardMarkup | None:
    rows = []
    if record.state in {"RESERVED", "WAITLIST", "UNKNOWN"}:
        rows.append([InlineKeyboardButton("예약·발권 재확인", callback_data=f"check:{record.id}")])
        if record.state == "RESERVED" and payments:
            rows.append([InlineKeyboardButton("수동 결제 확인", callback_data=f"quote:{record.id}")])
        rows.append([InlineKeyboardButton("앱에서 처리완료 확인", callback_data=f"ack:{record.id}")])
    return InlineKeyboardMarkup(rows) if rows else None


def train_page(search: Search, page: int) -> tuple[str, InlineKeyboardMarkup]:
    pages = (len(search.trains) + PAGE_SIZE - 1) // PAGE_SIZE
    if page < 0 or page >= pages:
        raise BotError("유효하지 않은 페이지입니다.")
    start, end = page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(search.trains))
    lines = [search.query.summary(), f"페이지 {page + 1}/{pages} — 매진 포함, 아직 예약하지 않았습니다."]
    lines.extend(f"{i + 1}. {search.trains[i]}" for i in range(start, end))
    rows = [[InlineKeyboardButton(str(i + 1), callback_data=f"pick:{search.id}:{i}") for i in range(start, end)]]
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton("◀ 이전", callback_data=f"page:{search.id}:{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton("다음 ▶", callback_data=f"page:{search.id}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("이 페이지 중 한 편", callback_data=f"pickall:{search.id}:{page}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


class BotUI:
    """메시지와 모든 콜백에서 허용 사용자·개인 채팅을 먼저 검증합니다."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.controller = Controller(settings, store, self.notify)
        self.bot: Bot | None = None

    async def authorize(self, update: Update) -> int | None:
        user, chat = update.effective_user, update.effective_chat
        if (
            user is not None
            and chat is not None
            and chat.type == "private"
            and chat.id == user.id
            and user.id in self.settings.allowed
        ):
            return user.id
        message = "허용된 사용자의 개인 대화에서만 사용할 수 있습니다."
        if user is not None and chat is not None and chat.type == "private":
            message += f" 사용자 ID: {user.id}"
        if update.callback_query is not None:
            await update.callback_query.answer(message, show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(message)
        return None

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.authorize(update) is not None and update.effective_message is not None:
            await update.effective_message.reply_text(HELP)

    async def setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner = await self.authorize(update)
        if owner is not None and update.effective_message is not None:
            await update.effective_message.reply_text(
                f"봇을 중단한 뒤 서버에서 실행하세요:\nuv run --extra bot pykorail-bot setup --user {owner}\n"
                "회원번호·비밀번호와 선택적 카드 정보는 서버 터미널에서만 입력합니다."
            )

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner = await self.authorize(update)
        message = update.effective_message
        if owner is None or message is None:
            return
        try:
            query = parse_query(message.text or "")
            await message.reply_text("열차를 조회합니다. 아직 예약하지 않습니다.")
            search = await self.controller.search(owner, query)
            text, keyboard = train_page(search, 0)
            await message.reply_text(text, reply_markup=keyboard)
        except Exception as error:
            await message.reply_text(safe_error(error))

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner = await self.authorize(update)
        if owner is not None and update.effective_message is not None:
            await update.effective_message.reply_text(self.controller.stop(owner))

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner = await self.authorize(update)
        if owner is None or update.effective_message is None:
            return
        records = self.controller.status(owner)
        if not records:
            await update.effective_message.reply_text("저장된 예약 작업이 없습니다.")
        for record in records:
            await update.effective_message.reply_text(
                job_text(record), reply_markup=job_buttons(record, self.settings.payments)
            )

    async def check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner = await self.authorize(update)
        if owner is None or update.effective_message is None:
            return
        try:
            if not context.args or len(context.args) != 1:
                raise BotError("사용법: /check 작업ID (ID는 /status에서 확인)")
            record = await self.controller.check(owner, context.args[0])
            await update.effective_message.reply_text(
                job_text(record), reply_markup=job_buttons(record, self.settings.payments)
            )
        except Exception as error:
            await update.effective_message.reply_text(safe_error(error))

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner = await self.authorize(update)
        callback = update.callback_query
        if owner is None or callback is None:
            return
        await callback.answer()
        try:
            parts = (callback.data or "").split(":")
            action, token = parts[:2]
            if action in {"page", "pick", "pickall"}:
                search = self.controller.result(owner, token)
                index = int(parts[2])
                if action == "page":
                    text, keyboard = train_page(search, index)
                else:
                    if action == "pickall":
                        train_page(search, index)
                        indices = list(range(index * PAGE_SIZE, min((index + 1) * PAGE_SIZE, len(search.trains))))
                    else:
                        indices = [index]
                    search = self.controller.prepare(owner, token, indices)
                    chosen = "\n".join(str(search.trains[i]) for i in indices)
                    text = (
                        f"{search.query.summary()}\n{chosen}\n\n이 중 한 편의 좌석 확보를 시도할까요?\n"
                        "자동결제·예약대기 등록은 하지 않습니다. /stop으로 중단할 수 있습니다."
                    )
                    keyboard = InlineKeyboardMarkup(
                        [[InlineKeyboardButton("예약 시도 시작", callback_data=f"start:{token}:{search.confirmation}")]]
                    )
                await callback.edit_message_text(text, reply_markup=keyboard)
            elif action == "start":
                record = self.controller.start(owner, token, parts[2])
                await callback.edit_message_text(job_text(record) + "\n/stop으로 중단할 수 있습니다.")
            elif action == "check":
                record = await self.controller.check(owner, token)
                await callback.edit_message_text(
                    job_text(record), reply_markup=job_buttons(record, self.settings.payments)
                )
            elif action == "quote":
                quote = await self.controller.quote(owner, token)
                card = quote.credential.card
                if card is None:
                    raise BotError("등록된 카드가 없습니다.")
                await callback.edit_message_text(
                    f"작업 {token}\n{self.controller.store.get(owner, token).details}\n"
                    f"카드 끝 4자리: {card.number[-4:]}\n결제 금액: {quote.price:,}원\n"
                    "60초 안에 확인해야 합니다. 실제 카드 결제입니다.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("이 금액으로 실제 결제", callback_data=f"pay:{token}:{quote.nonce}")]]
                    ),
                )
            elif action == "pay":
                await self.controller.pay(owner, token, parts[2])
                await callback.edit_message_text(
                    "결제 요청 처리가 끝났습니다. 알림 또는 /status에서 결과를 확인하세요."
                )
            elif action == "ack":
                self.controller.store.get(owner, token)
                await callback.edit_message_text(
                    "코레일 앱에서 해당 예약·결제 결과를 직접 확인하고 필요한 처리를 마쳤나요?\n"
                    "이 버튼은 봇의 확인 대기를 해제할 뿐, 예약을 취소하거나 환불하지 않습니다.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("앱에서 확인·처리 완료", callback_data=f"ackdone:{token}")]]
                    ),
                )
            elif action == "ackdone":
                await callback.edit_message_text(job_text(self.controller.acknowledge(owner, token)))
            else:
                raise BotError("지원하지 않거나 만료된 버튼입니다.")
        except (ValueError, IndexError):
            await callback.edit_message_text("유효하지 않은 버튼입니다. /status 또는 새 조회를 사용하세요.")
        except Exception as error:
            await callback.edit_message_text(safe_error(error))

    async def notify(self, record: JobRecord) -> None:
        if self.bot is not None:
            await self.bot.send_message(
                record.owner, job_text(record), reply_markup=job_buttons(record, self.settings.payments)
            )

    async def shutdown(self, application: Application) -> None:
        await self.controller.close()

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("텔레그램 처리 오류 (%s). 원문과 Update는 기록하지 않습니다.", type(context.error).__name__)


def build_application(settings: Settings, store: Store) -> Application:
    """등록만 수행합니다. polling은 명시적으로 실행한 CLI에서만 시작합니다."""
    ui = BotUI(settings, store)
    # 네트워크 대기 중에도 다른 사용자의 /stop이 처리되도록 합니다. 계정 직렬화는 Controller가 담당합니다.
    application = Application.builder().token(settings.token).concurrent_updates(8).post_stop(ui.shutdown).build()
    ui.bot = application.bot
    application.add_handler(CommandHandler(["start", "help"], ui.help))
    application.add_handler(CommandHandler("setup", ui.setup))
    application.add_handler(CommandHandler(["stop", "cancel"], ui.stop))
    application.add_handler(CommandHandler("status", ui.status))
    application.add_handler(CommandHandler("check", ui.check))
    application.add_handler(CallbackQueryHandler(ui.callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ui.text))
    application.add_error_handler(ui.error)
    return application
