"""텔레그램 봇 엔트리포인트."""

from __future__ import annotations

import argparse
import logging
import os

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..service import journal
from . import auth_guard, device_settings, handlers, storage

logger = logging.getLogger(__name__)


def _build_setup_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("setup", handlers.setup_entry)],
        states={
            handlers.STATE_KTX_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.setup_ktx_id),
            ],
            handlers.STATE_KTX_PW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.setup_ktx_pw),
            ],
            handlers.STATE_SETUP_CARD_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.setup_card_number),
            ],
            handlers.STATE_SETUP_CARD_PW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.setup_card_pw),
            ],
            handlers.STATE_SETUP_CARD_BIRTHDAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.setup_card_birthday),
            ],
            handlers.STATE_SETUP_CARD_EXPIRE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.setup_card_expire),
            ],
            handlers.STATE_CARD_LABEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.setup_card_label),
            ],
        },
        fallbacks=[CommandHandler("cancel", handlers.setup_cancel)],
        allow_reentry=True,
    )


def _build_cards_add_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handlers.cards_add_entry, pattern=r"^cards:add$"),
        ],
        states={
            handlers.STATE_CARDS_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.cards_add_number),
            ],
            handlers.STATE_CARDS_PW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.cards_add_pw),
            ],
            handlers.STATE_CARDS_BIRTHDAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.cards_add_birthday),
            ],
            handlers.STATE_CARDS_EXPIRE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.cards_add_expire),
            ],
            handlers.STATE_CARDS_NEW_LABEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.cards_add_label),
            ],
        },
        fallbacks=[CommandHandler("cancel", handlers.cards_add_cancel)],
        per_message=False,
    )


def _build_cards_edit_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handlers.cards_edit_entry, pattern=r"^cards:edit_field:"),
        ],
        states={
            handlers.STATE_CARDS_EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.cards_edit_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", handlers.cards_edit_cancel)],
        per_message=False,
    )


def _build_device_conversation() -> ConversationHandler:
    """정식 명령과 하이픈을 포함한 직접 입력 별칭을 함께 지원합니다."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("dev_set", device_settings.entry),
            MessageHandler(filters.Regex(r"^/dev-set\s*$"), device_settings.entry),
        ],
        states={
            device_settings.MENU: [CallbackQueryHandler(device_settings.choose, pattern=r"^dev:")],
            device_settings.FIELDS: [CallbackQueryHandler(device_settings.choose, pattern=r"^dev:")],
            device_settings.FIELD: [CallbackQueryHandler(device_settings.choose, pattern=r"^dev:")],
            device_settings.INPUT: [
                CallbackQueryHandler(device_settings.choose, pattern=r"^dev:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, device_settings.receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", device_settings.cancel)],
        allow_reentry=True,
    )


async def shutdown(app: Application) -> None:
    await handlers._SESSION.shutdown()
    for data in app.user_data.values():
        search = data.pop("search", None)
        if search is not None:
            search["rail"].close()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("텔레그램 처리 오류: %s", type(context.error).__name__)


def build_application(token: str) -> Application:
    """myTrail의 대화 핸들러 등록 순서를 유지합니다."""
    app = Application.builder().token(token).concurrent_updates(False).post_stop(shutdown).build()
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("cards", handlers.cmd_cards))
    app.add_handler(_build_setup_conversation())
    app.add_handler(_build_cards_add_conversation())
    app.add_handler(_build_cards_edit_conversation())
    app.add_handler(_build_device_conversation())
    app.add_handler(CommandHandler("cancel", handlers.cmd_cancel))
    app.add_handler(CommandHandler("status", handlers.cmd_status))
    app.add_handler(CallbackQueryHandler(handlers.on_resolve, pattern=r"^resolve:"))
    app.add_handler(CallbackQueryHandler(handlers.on_page, pattern=r"^page:"))
    app.add_handler(CallbackQueryHandler(handlers.on_pick, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(handlers.on_preset_card, pattern=r"^preset:"))
    app.add_handler(CallbackQueryHandler(handlers.on_payment_decision, pattern=r"^pay:"))
    app.add_handler(
        CallbackQueryHandler(
            handlers.on_cards_callback,
            pattern=r"^cards:(del|del_confirm|edit|edit_done)(?=:)|^cards:(noop|done)$",
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_free_message))
    app.add_error_handler(error_handler)
    return app


def main(argv: list[str] | None = None) -> None:
    """운영자 설정만 읽습니다. 사용자 계정/카드는 텔레그램 /setup에서 받습니다."""
    parser = argparse.ArgumentParser(description="myTrail 기반 KTX 텔레그램 봇")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("command", nargs="?", choices=["run", "keygen"], default="run")
    args = parser.parse_args(argv)
    if args.command == "keygen":
        print(Fernet.generate_key().decode("ascii"))
        return
    load_dotenv(args.env_file, override=False)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("pykorail.responses").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    lease = None
    try:
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("BOT_TOKEN을 설정하세요.")
        if not auth_guard.get_allowed_ids():
            logger.warning(
                "허용 사용자가 없습니다. 텔레그램 개인 대화에서 /setup으로 ID를 확인한 뒤 BOT_ALLOWED_IDS에 추가하세요."
            )
        if "BOT_ENABLE_PAYMENTS" in os.environ:
            raise ValueError(
                "이전 BOT_ENABLE_PAYMENTS 설정을 제거하세요. "
                "myTrail에서는 사용자가 예약 전 카드를 선택하면 자동결제에 동의합니다."
            )
        if "BOT_POLL_SECONDS" in os.environ:
            logger.warning(
                "BOT_POLL_SECONDS는 더 이상 사용하지 않습니다. 조회 후 대기는 myTrail과 같은 평균 6.5초 감마분포입니다."
            )
        storage._get_cipher()
        # 앞서 만든 별도 봇의 데이터는 다른 형식입니다. 조용히 버리고 새 봇을 켜지 않습니다.
        from pathlib import Path

        old_data = Path(os.environ.get("BOT_DATA_DIR", "bot-data"))
        if old_data.exists() and any(old_data.iterdir()):
            raise ValueError("기존 pykorail_bot 데이터가 있습니다. docs/telegram-bot.md의 전환 절차를 먼저 확인하세요.")
        journal.initialize()
        lease = journal.acquire_instance()
        journal.recover()
        build_application(token).run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    except ValueError as error:
        parser.exit(1, f"설정 오류: {error}\n")
    except Exception as error:
        parser.exit(1, f"실행 오류 ({type(error).__name__}). 비밀정보 보호를 위해 상세 응답은 표시하지 않습니다.\n")
    finally:
        if lease is not None:
            lease.close()


if __name__ == "__main__":
    main()
