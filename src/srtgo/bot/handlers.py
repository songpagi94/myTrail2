"""텔레그램 봇 명령·메시지·콜백 핸들러."""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import secrets
import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import ConversationHandler

from pykorail import PykorailError, Reservation, TransportError

from ..rail.ktx.client import KST
from ..service import auth as svc_auth
from ..service import journal
from ..service import payment as svc_pay
from ..service import reservation as svc_resv
from ..service.progress import PollProgress
from . import notifier, parser, storage
from . import session as _session_mod
from .access import BotContext, erase_input, guarded
from .access import BotUpdate as Update
from .errors import safe_error

logger = logging.getLogger(__name__)


HELP_TEXT = (
    "사용법:\n"
    "/setup — 자격증명 등록 (코레일 ID/PW, 카드)\n"
    "/cards — 카드 목록·추가·삭제\n"
    "/dev_set (/dev-set) — 사용자별 기기 ID 설정\n"
    "/status — 예약·결제 확인 대기 상태\n"
    "/cancel — 진행 중 예약 시도·예약 취소\n"
    "/help — 도움말\n\n"
    "예약 검색 형식:\n"
    "  출발역 도착역 날짜(YYYYMMDD) 시간(HHMM) [KTX] [좌석옵션] [승객유형]\n\n"
    "좌석 옵션: 일반만(기본), 일반우선, 특실만, 특실우선\n"
    "승객 유형: 어린이, 유아, 경로, 중증장애인, 경증장애인 (없으면 성인 기본)\n\n"
    "예:\n"
    "  서울 부산 20261003 1400\n"
    "  서울 부산 20261003 1400 KTX\n"
    "  서울 부산 20261003 1400 KTX 특실우선\n"
    "  서울 부산 20261003 1400 KTX 어린이\n"
    "  부산 서울 20261003 0800"
)

WELCOME_TEXT = "환영합니다. 먼저 /setup 으로 자격증명을 등록해주세요.\n\n" + HELP_TEXT


@guarded
async def cmd_start(update: Update, context: BotContext) -> None:
    await update.message.reply_text(WELCOME_TEXT)


@guarded
async def cmd_help(update: Update, context: BotContext) -> None:
    await update.message.reply_text(HELP_TEXT)


STATE_KTX_ID, STATE_KTX_PW = range(2)
STATE_SETUP_CARD_NUMBER, STATE_SETUP_CARD_PW, STATE_SETUP_CARD_BIRTHDAY, STATE_SETUP_CARD_EXPIRE, STATE_CARD_LABEL = (
    range(4, 9)
)

STATE_CARDS_NUMBER, STATE_CARDS_PW, STATE_CARDS_BIRTHDAY, STATE_CARDS_EXPIRE, STATE_CARDS_NEW_LABEL = range(10, 15)
STATE_CARDS_EDIT_VALUE = 20


@guarded
async def setup_entry(update: Update, context: BotContext) -> int:

    tid = update.effective_user.id
    if _SESSION.is_polling(tid) or journal.current(tid) is not None:
        await update.message.reply_text(
            "진행 중이거나 확인 대기 중인 작업이 있습니다. /cancel 또는 /status를 확인하세요."
        )
        return ConversationHandler.END
    old_search = context.user_data.pop("search", None)
    if old_search:
        await asyncio.to_thread(old_search["rail"].close)
    armed = context.user_data.pop("setup_overwrite_armed", False)
    if storage.exists(tid) and not armed:
        context.user_data["setup_overwrite_armed"] = True
        await update.message.reply_text(
            "이미 입력된 정보가 있습니다.\n덮어쓰려면 /setup 한 번 더 보내주세요. 그대로 두려면 무시하세요."
        )
        return ConversationHandler.END

    context.user_data["setup"] = {}
    await update.message.reply_text(
        "자격증명 등록을 시작합니다.\n\n"
        "1단계: 코레일 회원번호(또는 이메일 또는 휴대폰번호)를 입력해주세요.\n"
        "개인 채팅의 입력도 텔레그램을 거쳐 전송됩니다. 입력 메시지 삭제를 시도하고 서버에 암호화 저장합니다.\n"
        "(취소: /cancel)"
    )
    return STATE_KTX_ID


@guarded
async def setup_ktx_id(update: Update, context: BotContext) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("코레일 로그인 ID를 입력해주세요.")
        return STATE_KTX_ID
    await erase_input(update)
    context.user_data["setup"]["_ktx_id"] = text
    await update.message.reply_text("KTX 비밀번호를 입력해주세요.")
    return STATE_KTX_PW


@guarded
async def setup_ktx_pw(update: Update, context: BotContext) -> int:
    pw = update.message.text or ""
    if not pw:
        await update.message.reply_text("비밀번호를 입력해주세요.")
        return STATE_KTX_PW
    await erase_input(update)
    ktx_id = context.user_data["setup"].pop("_ktx_id")
    context.user_data["setup"]["ktx"] = {"id": ktx_id, "pw": pw}
    await update.message.reply_text("카드번호를 입력해주세요. 등록하지 않으려면 skip.\n")
    return STATE_SETUP_CARD_NUMBER


@guarded
async def setup_card_number(update: Update, context: BotContext) -> int:
    text = (update.message.text or "").strip()
    if text.lower() == "skip":
        storage.save(update.effective_user.id, {**context.user_data.pop("setup"), "cards": []})
        await update.message.reply_text("계정 등록 완료. 카드는 나중에 /cards에서 추가할 수 있습니다.")
        return ConversationHandler.END
    await erase_input(update)
    context.user_data["setup"]["_card"] = {"number": text.replace(" ", "").replace("-", "")}
    await update.message.reply_text("카드 비밀번호 앞 2자리를 입력해주세요.\n예: 12")
    return STATE_SETUP_CARD_PW


@guarded
async def setup_card_pw(update: Update, context: BotContext) -> int:
    await erase_input(update)
    context.user_data["setup"]["_card"]["password"] = (update.message.text or "").strip()
    await update.message.reply_text("생년월일(6자리) 또는 사업자등록번호(10자리)를 입력해주세요.")
    return STATE_SETUP_CARD_BIRTHDAY


@guarded
async def setup_card_birthday(update: Update, context: BotContext) -> int:
    await erase_input(update)
    context.user_data["setup"]["_card"]["birthday"] = (update.message.text or "").strip()
    await update.message.reply_text("유효기간(YYMM)을 입력해주세요.\n예: 3106")
    return STATE_SETUP_CARD_EXPIRE


@guarded
async def setup_card_expire(update: Update, context: BotContext) -> int:
    await erase_input(update)
    context.user_data["setup"]["_card"]["expire"] = (update.message.text or "").strip()
    await update.message.reply_text("카드 별칭을 입력해주세요. (예: '신한', 없으면 'skip')")
    return STATE_CARD_LABEL


@guarded
async def setup_card_label(update: Update, context: BotContext) -> int:
    text = (update.message.text or "").strip()
    label: str | None = None if text.lower() == "skip" or text == "" else text[:32]

    setup_data = context.user_data.get("setup", {})
    pending = setup_data.pop("_card", None)
    if pending is None:
        await update.message.reply_text("등록 상태 손상. /setup 다시 해주세요.")
        context.user_data.pop("setup", None)
        return ConversationHandler.END

    setup_data["cards"] = []
    storage.save(update.effective_user.id, setup_data)
    storage.add_card(update.effective_user.id, pending, label)

    context.user_data.pop("setup", None)
    await update.message.reply_text("등록 완료.\n\n" + HELP_TEXT)
    return ConversationHandler.END


@guarded
async def setup_cancel(update: Update, context: BotContext) -> int:
    context.user_data.pop("setup", None)
    await update.message.reply_text("등록 취소됨.")
    return ConversationHandler.END


def _seat_option_from_intent(rail_type: str, pref: str) -> str:
    from ..rail.ktx.models import ReserveOption

    return getattr(ReserveOption, pref)


PAGE_SIZE = 10


def _card_display(card: dict) -> str:
    last4 = card["number"][-4:]
    if card.get("label"):
        return f"{card['label']} (*{last4})"
    return f"*{last4}"


def _card_select_keyboard(cards: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(_card_display(c), callback_data=f"pay:card:{c['id']}")] for c in cards]
    rows.append([InlineKeyboardButton("← 돌아가기", callback_data="pay:back")])
    return InlineKeyboardMarkup(rows)


def _preset_card_keyboard(cards: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(_card_display(c), callback_data=f"preset:card:{c['id']}")] for c in cards]
    rows.append([InlineKeyboardButton("수동 결제", callback_data="preset:manual")])
    return InlineKeyboardMarkup(rows)


def _train_keyboard(n_trains: int, page: int = 0) -> InlineKeyboardMarkup:
    """번호 버튼 + 이전/다음 페이지 + 전부/취소.

    한 페이지에 PAGE_SIZE개 표시. n_trains > PAGE_SIZE면 페이지 네비 노출.
    """
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, n_trains)
    page_count = max(1, (n_trains + PAGE_SIZE - 1) // PAGE_SIZE)

    rows = []
    indices_on_page = list(range(start, end))
    for chunk_start in range(0, len(indices_on_page), 5):
        chunk = indices_on_page[chunk_start : chunk_start + 5]
        rows.append([InlineKeyboardButton(str(i + 1), callback_data=f"pick:{i}") for i in chunk])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 이전", callback_data=f"page:{page - 1}"))
    if page < page_count - 1:
        nav.append(InlineKeyboardButton("다음 ▶", callback_data=f"page:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton("이 페이지 전부", callback_data=f"pick:all:{page}"),
            InlineKeyboardButton("취소", callback_data="pick:none"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _format_train_page(trains: list, page: int) -> str:
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(trains))
    page_count = max(1, (len(trains) + PAGE_SIZE - 1) // PAGE_SIZE)
    header = f"어떤 열차로 예약할까요? (페이지 {page + 1}/{page_count}, 총 {len(trains)}편)"
    lines = [header, ""]
    for i in range(start, end):
        lines.append(f"{i + 1}. {trains[i]}")
    return "\n".join(lines)


@guarded
async def on_free_message(update: Update, context: BotContext) -> None:

    tid = update.effective_user.id
    if _SESSION.is_polling(tid) or journal.current(tid) is not None:
        await update.message.reply_text(
            "진행 중이거나 확인 대기 중인 예약이 있습니다. /cancel 또는 /status를 확인하세요."
        )
        return
    old_search = context.user_data.pop("search", None)
    if old_search:
        await asyncio.to_thread(old_search["rail"].close)
    creds = storage.load(tid)
    if creds is None:
        await update.message.reply_text("자격증명 미등록. /setup 부터 해주세요.")
        return

    text = update.message.text or ""
    today = _dt.datetime.now(KST).date().isoformat()

    try:
        intent = parser.parse(text=text, today=today)
    except parser.ParseError as e:
        await update.message.reply_text(f"형식 오류: {e}\n\n" + HELP_TEXT)
        return

    rail_type = intent["rail"]
    cred = creds.get(rail_type.lower())
    if not cred:
        await update.message.reply_text(f"{rail_type} 자격증명 미등록. /setup 다시 해주세요.")
        return

    try:
        rail = await asyncio.to_thread(svc_auth.create_rail, rail_type, credentials=cred, owner=tid)
    except Exception as e:
        await update.message.reply_text(f"로그인 실패: {safe_error(e, operation='login')}")
        return

    date = intent["date"].replace("-", "")
    search_params = {
        "dep": intent["dep"],
        "arr": intent["arr"],
        "date": date,
        "time": intent["time"],
        "passengers": _passengers_to_list(rail_type, intent["passengers"]),
        "include_no_seats": True,
    }
    try:
        trains = await asyncio.to_thread(rail.search_train, **search_params)
    except Exception as e:
        await asyncio.to_thread(rail.close)
        await update.message.reply_text(f"검색 실패: {safe_error(e, operation='search')}")
        return

    if not trains:
        await asyncio.to_thread(rail.close)
        await update.message.reply_text("해당 시간대 열차 없음.")
        return

    context.user_data["search"] = {
        "rail": rail,
        "rail_type": rail_type,
        "trains": trains,
        "search_params": search_params,
        "passengers": _passengers_to_list(rail_type, intent["passengers"]),
        "seat_option": _seat_option_from_intent(rail_type, intent["seat_pref"]),
        "page": 0,
    }
    sent = await update.message.reply_text(
        _format_train_page(trains, 0),
        reply_markup=_train_keyboard(len(trains), 0),
    )
    context.user_data["search"]["message_id"] = sent.message_id


def _passengers_to_list(rail_type: str, p: dict) -> list:
    """intent의 passengers dict → rail이 받는 Passenger 리스트."""
    out = []
    from ..rail.ktx.models import (
        AdultPassenger,
        ChildPassenger,
        Disability1To3Passenger,
        Disability4To6Passenger,
        SeniorPassenger,
        ToddlerPassenger,
    )

    if p.get("adult"):
        out.append(AdultPassenger(p["adult"]))
    if p.get("child"):
        out.append(ChildPassenger(p["child"]))
    if p.get("senior"):
        out.append(SeniorPassenger(p["senior"]))
    if p.get("disability1to3"):
        out.append(Disability1To3Passenger(p["disability1to3"]))
    if p.get("disability4to6"):
        out.append(Disability4To6Passenger(p["disability4to6"]))
    if p.get("toddler"):
        out.append(ToddlerPassenger(p["toddler"]))
    return out


_SESSION = _session_mod.Session()


def _resolve_indices(data: str, n_trains: int) -> list[int] | None:
    """callback data → 인덱스 목록 또는 None(취소).

    pick:none      → None (취소)
    pick:all:<P>   → P 페이지의 인덱스 전부
    pick:<i>       → 단일 인덱스 [i]
    """
    if data == "pick:none":
        return None
    if data.startswith("pick:all:"):
        page = int(data.split(":")[2])
        start = page * PAGE_SIZE
        end = min(start + PAGE_SIZE, n_trains)
        return list(range(start, end))
    return [int(data.removeprefix("pick:"))]


@guarded
async def on_page(update: Update, context: BotContext) -> None:
    """페이지 네비게이션 콜백 (◀ 이전 / 다음 ▶)."""
    cq = update.callback_query
    assert isinstance(cq.data, str)
    await cq.answer()

    search = context.user_data.get("search")
    if not search:
        await cq.edit_message_text("세션 만료. 다시 요청해주세요.")
        return

    new_page = int(cq.data.removeprefix("page:"))
    if not 0 <= new_page < (len(search["trains"]) + PAGE_SIZE - 1) // PAGE_SIZE:
        await cq.edit_message_text("유효하지 않은 페이지입니다.")
        return
    search["page"] = new_page
    trains = search["trains"]
    await cq.edit_message_text(
        _format_train_page(trains, new_page),
        reply_markup=_train_keyboard(len(trains), new_page),
    )


@guarded
async def on_pick(update: Update, context: BotContext) -> None:
    cq = update.callback_query
    assert isinstance(cq.data, str)
    await cq.answer()
    tid = update.effective_user.id

    search = context.user_data.get("search")
    if not search:
        await cq.edit_message_text("세션 만료. 다시 요청해주세요.")
        return

    indices = _resolve_indices(cq.data, len(search["trains"]))
    if indices is None:
        await cq.edit_message_text("취소됨.")
        context.user_data.pop("search", None)
        await asyncio.to_thread(search["rail"].close)
        return

    if _SESSION.is_polling(tid) or journal.current(tid) is not None:
        await cq.edit_message_text("이미 진행 중인 예약 시도가 있어요. /cancel 후 다시.")
        return

    if not indices or any(i < 0 or i >= len(search["trains"]) for i in indices):
        await cq.edit_message_text("유효하지 않은 열차 선택입니다.")
        return
    context.user_data["pending_indices"] = indices
    cards = storage.list_cards(tid)
    await cq.edit_message_text(
        "좌석 확보 후 평균 3분(무작위 대기) 뒤 자동 결제할 카드를 선택하세요. 결제기한이 짧으면 대기를 줄입니다.",
        reply_markup=_preset_card_keyboard(cards),
    )


@guarded
async def on_preset_card(update: Update, context: BotContext) -> None:
    cq = update.callback_query
    assert isinstance(cq.data, str)
    await cq.answer()
    tid = update.effective_user.id

    search = context.user_data.get("search")
    indices = context.user_data.pop("pending_indices", None)
    if not search or indices is None:
        await cq.edit_message_text("세션 만료. 다시 요청해주세요.")
        return

    preset_card_id = None if cq.data == "preset:manual" else cq.data.removeprefix("preset:card:")

    if _SESSION.is_polling(tid) or journal.current(tid) is not None:
        await cq.edit_message_text("이미 진행 중이거나 확인 대기 중입니다. /status를 확인하세요.")
        return
    preset_card = storage.get_card(tid, preset_card_id) if preset_card_id else None
    if preset_card_id and preset_card is None:
        await cq.edit_message_text("카드가 삭제되었습니다. 다시 조회해 선택하세요.")
        return
    cancel_event = threading.Event()
    bot = context.application.bot
    loop = asyncio.get_running_loop()
    progress = PollProgress()

    def on_payment_scheduled(scheduled: _dt.datetime) -> None:
        asyncio.run_coroutine_threadsafe(
            notifier.send_text(
                bot,
                tid,
                f"예약 성공! 아직 결제 전입니다.\n자동결제 예정: {scheduled:%Y-%m-%d %H:%M:%S} (한국시간)\n"
                "대기 중 /cancel을 보내면 자동결제를 중단하고 예약을 취소합니다.",
            ),
            loop,
        )

    def on_success(reservation: Reservation) -> None:
        # 카드 선택은 자동결제 동의입니다. 선택 당시 정보로만 결제하며 중단 후에는 결제하지 않습니다.
        if preset_card and not cancel_event.is_set() and not reservation.is_waiting:
            try:
                ready = svc_pay.wait_for_auto_payment(reservation, cancel_event, on_payment_scheduled)
                ok = ready and svc_pay.pay_with_saved_card(search["rail"], reservation, preset_card)
            except Exception:
                asyncio.run_coroutine_threadsafe(
                    notifier.send_text(
                        bot,
                        tid,
                        "결제 결과를 확인해야 합니다. 코레일 앱과 /status에서 확인하세요. 자동 재결제하지 않습니다.",
                    ),
                    loop,
                )
                return
            if ok:
                asyncio.run_coroutine_threadsafe(
                    notifier.send_text(bot, tid, f"좌석 확보 및 결제 완료!\n{reservation}"), loop
                )
                return
        pending = {"reservation": reservation, "rail": search["rail"], "message_id": None}
        _SESSION.set_pending(tid, pending)
        if cancel_event.is_set():
            # /cancel은 이 콜백이 끝난 뒤 예약을 취소합니다. 새 결제 버튼을 보내지 않습니다.
            return

        async def announce() -> None:
            pending["message_id"] = await notifier.send_seat_secured(bot, tid, reservation)

        asyncio.run_coroutine_threadsafe(announce(), loop)

    def on_error(exc: Exception) -> bool:
        permanent = isinstance(exc, journal.UncertainOperationError) or (
            isinstance(exc, PykorailError) and not isinstance(exc, TransportError)
        )
        if permanent:
            asyncio.run_coroutine_threadsafe(notifier.send_text(bot, tid, f"예약 시도 중단: {safe_error(exc)}"), loop)
        return not permanent

    async def runner() -> None:
        try:
            await asyncio.to_thread(
                svc_resv.poll_and_reserve,
                search["rail"],
                search["search_params"],
                indices,
                search["seat_option"],
                on_success,
                on_error,
                cancel_event,
                search["passengers"],
                search["trains"],
                progress,
            )
        except Exception as error:
            await notifier.send_text(bot, tid, f"예약 작업 중단: {safe_error(error)} /status로 확인하세요.")
        finally:
            if _SESSION.get_pending(tid) is None:
                await asyncio.to_thread(search["rail"].close)

    task = asyncio.create_task(runner())
    _SESSION.start_poll(tid, task, cancel_event, progress)
    context.user_data.pop("search", None)
    start_msg = (
        "예약 시도 시작. 좌석 확보 후 약 3분 뒤 자동 결제합니다."
        if preset_card_id
        else "예약 시도 시작. 좌석 잡히면 알림 드립니다."
    )
    await cq.edit_message_text(start_msg)


@guarded
async def on_payment_decision(update: Update, context: BotContext) -> None:
    cq = update.callback_query
    assert isinstance(cq.data, str)
    await cq.answer()
    tid = update.effective_user.id

    if cq.data == "pay:cancel":
        await _handle_pay_cancel(cq, tid)
        return
    if cq.data == "pay:back":
        await _handle_pay_back(cq, tid)
        return
    if cq.data == "pay:confirm":
        await _handle_pay_confirm(cq, tid)
        return
    if cq.data.startswith("pay:card:"):
        card_id = cq.data.removeprefix("pay:card:")
        await _handle_pay_card(cq, tid, card_id)
        return


async def _handle_pay_cancel(cq, tid: int) -> None:
    pending = _SESSION.get_pending(tid)
    if not pending:
        await cq.edit_message_text("대기 중인 예약이 없어요. 결제 마감이 지났을 수 있습니다.")
        return
    try:
        await asyncio.to_thread(pending["rail"].cancel, pending["reservation"])
    except Exception as e:
        logger.error("예약 취소 실패: %s", type(e).__name__)
        await cq.edit_message_text("예약 취소 결과를 확인하지 못했습니다. 코레일 앱과 /status에서 확인하세요.")
        return
    _SESSION.clear_pending(tid)
    await cq.edit_message_text("예약 취소됨.")


async def _handle_pay_confirm(cq, tid: int) -> None:
    pending = _SESSION.get_pending(tid)
    if not pending:
        await cq.edit_message_text("대기 중인 예약이 없어요. 결제 마감이 지났을 수 있습니다.")
        return

    cards = storage.list_cards(tid)
    if not cards:
        await cq.edit_message_text(
            "등록된 카드가 없어요. /cards 에서 추가 후 다시 결제 눌러주세요.",
            reply_markup=notifier.confirm_keyboard(),
        )
        return

    await cq.edit_message_text(
        "어느 카드로 결제할까요?",
        reply_markup=_card_select_keyboard(cards),
    )


async def _handle_pay_back(cq, tid: int) -> None:
    pending = _SESSION.get_pending(tid)
    if not pending:
        await cq.edit_message_text("대기 중인 예약이 없어요.")
        return
    await cq.edit_message_text(
        notifier.format_seat_secured_message(pending["reservation"]),
        reply_markup=notifier.confirm_keyboard(),
    )


async def _handle_pay_card(cq, tid: int, card_id: str) -> None:
    pending = _SESSION.get_pending(tid)
    if not pending:
        await cq.edit_message_text("대기 중인 예약이 없어요. 결제 마감이 지났을 수 있습니다.")
        return

    card = storage.get_card(tid, card_id)
    if card is None:
        await cq.edit_message_text(
            "이 카드는 삭제됐어요. 다시 결제 눌러 카드를 골라주세요.",
            reply_markup=notifier.confirm_keyboard(),
        )
        return

    rail = pending["rail"]
    reservation = pending["reservation"]
    try:
        ok = await asyncio.to_thread(svc_pay.pay_with_saved_card, rail, reservation, card)
    except Exception as e:
        logger.error("결제 예외: %s", type(e).__name__)
        await cq.edit_message_text(
            "결제 결과를 확인하지 못했습니다. 코레일 앱과 /status에서 확인하세요. 다시 결제하지 마세요."
        )
        _SESSION.clear_pending(tid)
        return

    _SESSION.clear_pending(tid)
    if ok:
        await cq.edit_message_text("결제 완료. 승차권은 코레일 앱에서 확인해주세요.")
    else:
        await cq.edit_message_text("결제 실패 (카드 정보 확인 필요).")


def _cards_keyboard(cards: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(f"편집 {_card_display(c)}", callback_data=f"cards:edit:{c['id']}"),
            InlineKeyboardButton(f"🗑 {_card_display(c)}", callback_data=f"cards:del:{c['id']}"),
        ]
        for c in cards
    ]
    rows.append([InlineKeyboardButton("➕ 카드 추가", callback_data="cards:add")])
    rows.append([InlineKeyboardButton("완료", callback_data="cards:done")])
    return InlineKeyboardMarkup(rows)


def _card_edit_keyboard(card_id: str) -> InlineKeyboardMarkup:
    fields = [
        ("카드번호", "number"),
        ("비밀번호", "password"),
        ("생년월일/사업자번호", "birthday"),
        ("유효기간", "expire"),
        ("별칭", "label"),
    ]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"cards:edit_field:{card_id}:{field}")] for label, field in fields
    ]
    rows.append([InlineKeyboardButton("완료", callback_data=f"cards:edit_done:{card_id}")])
    return InlineKeyboardMarkup(rows)


def _cards_list_text(cards: list[dict]) -> str:
    if not cards:
        return "등록된 카드가 없어요. ➕ 카드 추가 버튼을 눌러주세요."
    lines = ["등록된 카드:"]
    for c in cards:
        lines.append(f"- {_card_display(c)}")
    return "\n".join(lines)


def _del_confirm_keyboard(card_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("예, 삭제", callback_data=f"cards:del_confirm:{card_id}"),
                InlineKeyboardButton("아니오", callback_data="cards:noop"),
            ]
        ]
    )


@guarded
async def cmd_cards(update: Update, context: BotContext) -> None:

    tid = update.effective_user.id
    if not storage.exists(tid):
        await update.message.reply_text("/setup 부터 해주세요.")
        return

    cards = storage.list_cards(tid)
    await update.message.reply_text(
        _cards_list_text(cards),
        reply_markup=_cards_keyboard(cards),
    )


async def _redraw_cards_list(cq, tid: int) -> None:
    cards = storage.list_cards(tid)
    await cq.edit_message_text(
        _cards_list_text(cards),
        reply_markup=_cards_keyboard(cards),
    )


@guarded
async def on_cards_callback(update: Update, context: BotContext) -> None:
    """cards:del:<id>, cards:del_confirm:<id>, cards:noop 처리.

    cards:add 는 별도 ConversationHandler 진입점으로 등록됨.
    """
    cq = update.callback_query
    assert isinstance(cq.data, str)
    await cq.answer()
    tid = update.effective_user.id

    if cq.data.startswith("cards:del_confirm:"):
        card_id = cq.data.removeprefix("cards:del_confirm:")
        storage.remove_card(tid, card_id)
        await _redraw_cards_list(cq, tid)
        return

    if cq.data.startswith("cards:del:"):
        card_id = cq.data.removeprefix("cards:del:")
        card = storage.get_card(tid, card_id)
        if card is None:
            await _redraw_cards_list(cq, tid)
            return
        await cq.edit_message_text(
            f"정말 삭제할까요?\n  {_card_display(card)}",
            reply_markup=_del_confirm_keyboard(card_id),
        )
        return

    if cq.data.startswith("cards:edit:"):
        card_id = cq.data.removeprefix("cards:edit:")
        card = storage.get_card(tid, card_id)
        if card is None:
            await _redraw_cards_list(cq, tid)
            return
        await cq.edit_message_text(
            f"어떤 정보를 편집할까요?\n  {_card_display(card)}",
            reply_markup=_card_edit_keyboard(card_id),
        )
        return

    if cq.data.startswith("cards:edit_done:"):
        await _redraw_cards_list(cq, tid)
        return

    if cq.data == "cards:done":
        await cq.edit_message_text("완료.")
        return

    if cq.data == "cards:noop":
        await _redraw_cards_list(cq, tid)
        return


@guarded
async def cmd_cancel(update: Update, context: BotContext) -> None:

    tid = update.effective_user.id
    actions = []

    if _SESSION.cancel_poll(tid):
        await _SESSION.wait_poll(tid)
        actions.append("예약 시도 중단됨")
    old_search = context.user_data.pop("search", None)
    if old_search:
        await asyncio.to_thread(old_search["rail"].close)

    pending = _SESSION.get_pending(tid)
    if pending:
        try:
            await asyncio.to_thread(pending["rail"].cancel, pending["reservation"])
            actions.append("대기 중 예약 취소됨")
        except Exception as e:
            actions.append(f"예약 취소 결과 확인 필요: {safe_error(e)}")
            await update.message.reply_text("\n".join(actions))
            return
        _SESSION.clear_pending(tid)

    if not actions:
        await update.message.reply_text("진행 중인 작업이 없어요.")
    else:
        await update.message.reply_text("\n".join(actions))


@guarded
async def cards_add_entry(update: Update, context: BotContext) -> int:
    """cards:add 콜백 진입점."""
    cq = update.callback_query
    assert isinstance(cq.data, str)
    await cq.answer()

    context.user_data.pop("cards_new", None)
    context.user_data["cards_new"] = {}

    await cq.edit_message_text("추가할 카드번호를 입력해주세요.\n예: 1111222233334444\n(취소: /cancel)")
    return STATE_CARDS_NUMBER


@guarded
async def cards_add_number(update: Update, context: BotContext) -> int:
    await erase_input(update)
    context.user_data["cards_new"]["number"] = (update.message.text or "").strip().replace(" ", "")
    await update.message.reply_text("카드 비밀번호 앞 2자리를 입력해주세요.\n예: 12")
    return STATE_CARDS_PW


@guarded
async def cards_add_pw(update: Update, context: BotContext) -> int:
    await erase_input(update)
    context.user_data["cards_new"]["password"] = (update.message.text or "").strip()
    await update.message.reply_text("생년월일(6자리) 또는 사업자등록번호(10자리)를 입력해주세요.")
    return STATE_CARDS_BIRTHDAY


@guarded
async def cards_add_birthday(update: Update, context: BotContext) -> int:
    await erase_input(update)
    context.user_data["cards_new"]["birthday"] = (update.message.text or "").strip()
    await update.message.reply_text("유효기간(YYMM)을 입력해주세요.\n예: 3106")
    return STATE_CARDS_EXPIRE


@guarded
async def cards_add_expire(update: Update, context: BotContext) -> int:
    await erase_input(update)
    context.user_data["cards_new"]["expire"] = (update.message.text or "").strip()
    await update.message.reply_text("카드 별칭을 입력해주세요. (예: '신한', 없으면 'skip')")
    return STATE_CARDS_NEW_LABEL


@guarded
async def cards_add_label(update: Update, context: BotContext) -> int:
    text = (update.message.text or "").strip()
    label: str | None = None if text.lower() == "skip" or text == "" else text[:32]

    fields = context.user_data.pop("cards_new", None)
    if not fields:
        await update.message.reply_text("등록 상태 손상. /cards 다시 해주세요.")
        return ConversationHandler.END

    storage.add_card(update.effective_user.id, fields, label)
    cards = storage.list_cards(update.effective_user.id)
    await update.message.reply_text(
        _cards_list_text(cards),
        reply_markup=_cards_keyboard(cards),
    )
    return ConversationHandler.END


@guarded
async def cards_add_cancel(update: Update, context: BotContext) -> int:
    context.user_data.pop("cards_new", None)
    await update.message.reply_text("카드 추가 취소됨.")
    return ConversationHandler.END


@guarded
async def cards_edit_entry(update: Update, context: BotContext) -> int:
    """cards:edit_field:<id>:<field> 콜백 진입점."""
    cq = update.callback_query
    assert isinstance(cq.data, str)
    await cq.answer()

    data = cq.data.removeprefix("cards:edit_field:")
    card_id, field = data.split(":", 1)
    if (
        field not in {"number", "password", "birthday", "expire", "label"}
        or storage.get_card(update.effective_user.id, card_id) is None
    ):
        await cq.edit_message_text("유효하지 않은 카드 편집입니다.")
        return ConversationHandler.END
    field_prompts = {
        "number": "새 카드번호를 입력해주세요.\n예: 1111222233334444",
        "password": "새 카드 비밀번호 앞 2자리를 입력해주세요.\n예: 12",
        "birthday": "새 생년월일(6자리) 또는 사업자등록번호(10자리)를 입력해주세요.",
        "expire": "새 유효기간(YYMM)을 입력해주세요.\n예: 3106",
        "label": "새 카드 별칭을 입력해주세요. (없으면 'skip')",
    }
    msg = await cq.edit_message_text(field_prompts.get(field, f"새 {field} 값을 입력해주세요.") + "\n(취소: /cancel)")
    assert isinstance(msg, Message)
    context.user_data["cards_edit"] = {
        "card_id": card_id,
        "field": field,
        "chat_id": msg.chat_id,
        "message_id": msg.message_id,
    }
    return STATE_CARDS_EDIT_VALUE


@guarded
async def cards_edit_value(update: Update, context: BotContext) -> int:
    await erase_input(update)
    tid = update.effective_user.id
    edit_info = context.user_data.pop("cards_edit", None)
    if not edit_info:
        await update.message.reply_text("편집 상태 손상. /cards 다시 해주세요.")
        return ConversationHandler.END

    card_id = edit_info["card_id"]
    field = edit_info["field"]
    value = (update.message.text or "").strip()

    if field == "label" and (value.lower() == "skip" or value == ""):
        value = None
    elif field == "number":
        value = value.replace(" ", "")

    data = storage.load(tid)
    if data is None:
        await update.message.reply_text("저장된 정보가 없습니다.")
        return ConversationHandler.END

    updated = False
    for card in data.get("cards", []):
        if card["id"] == card_id:
            card[field] = value
            updated = True
            break

    if not updated:
        await update.message.reply_text("카드를 찾을 수 없습니다.")
        return ConversationHandler.END

    storage.save(tid, data)
    card = storage.get_card(tid, card_id)
    card_name = _card_display(card) if card else card_id
    await context.bot.edit_message_text(
        chat_id=edit_info["chat_id"],
        message_id=edit_info["message_id"],
        text=f"수정 완료. 계속 편집하려면 항목을 선택하세요.\n  {card_name}",
        reply_markup=_card_edit_keyboard(card_id),
    )
    return ConversationHandler.END


@guarded
async def cards_edit_cancel(update: Update, context: BotContext) -> int:
    context.user_data.pop("cards_edit", None)
    await update.message.reply_text("편집 취소됨.")
    return ConversationHandler.END


@guarded
async def cmd_status(update: Update, context: BotContext) -> None:
    """재시작/통신 단절 뒤에도 예약·결제를 무조건 재전송하지 않습니다."""
    tid = update.effective_user.id
    record = journal.current(tid)
    if _SESSION.is_polling(tid):
        message = (
            "예약 완료 후 자동결제 대기 또는 처리 중입니다."
            if record and record[0] != "RESERVING"
            else "예약 시도 중입니다."
        )
        progress = _SESSION.get_progress(tid)
        if progress is not None:
            message += f"\n경과: {progress.elapsed_seconds}초 · 조회 시도: {progress.attempts}회"
            if progress.searching:
                message += f"\n현재 {progress.attempts}번째 조회 응답을 기다리고 있습니다."
            elif not record:
                message += "\n조회 준비·결과 처리 또는 다음 조회 대기 중입니다."
        await update.message.reply_text(message + " /cancel로 중단할 수 있지만 이미 전송한 결제는 철회할 수 없습니다.")
        return
    if record is None:
        await update.message.reply_text("확인 대기 중인 예약·결제가 없습니다.")
        return
    nonce = secrets.token_hex(6)
    context.user_data["resolve"] = (nonce, record)
    await update.message.reply_text(
        f"확인 대기: {record[0]} / 예약번호: {record[1] or '확인 불가'}\n"
        "코레일 앱에서 예약·결제·취소 상태를 직접 확인하세요.\n"
        "아래 버튼은 봇의 확인 대기만 해제하며 예약 취소/환불을 하지 않습니다.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("앱에서 확인·처리 완료", callback_data=f"resolve:{nonce}")]]
        ),
    )


@guarded
async def on_resolve(update: Update, context: BotContext) -> None:
    """앱에서 처리했다는 사용자의 명시적인 확인 후에만 잠금을 해제합니다."""
    cq = update.callback_query
    assert isinstance(cq.data, str)
    await cq.answer()
    tid = update.effective_user.id
    confirmation = context.user_data.pop("resolve", None)
    if (
        confirmation is None
        or cq.data != f"resolve:{confirmation[0]}"
        or confirmation[1] != journal.current(tid)
        or _SESSION.is_polling(tid)
    ):
        await cq.edit_message_text("확인이 만료되었습니다. /status에서 다시 확인하세요.")
        return
    _SESSION.clear_pending(tid)
    journal.finish(tid)
    await cq.edit_message_text("앱에서의 처리완료 확인을 기록했습니다. 봇은 취소·결제를 실행하지 않았습니다.")
