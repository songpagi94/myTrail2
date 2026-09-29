"""myTrail 텔레그램 등록·카드·예매 흐름의 오프라인 통합 테스트."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import Mock

import pytest
from telegram.ext import ConversationHandler

from pykorail import LoginFailedError
from srtgo.bot import handlers as h
from srtgo.bot import storage
from srtgo.service import journal
from tests.mytrail_support import (
    CARD,
    RESERVATION,
    TRAIN,
    context,
    pending,
    registered,
    saved_card,
    search_context,
    update,
)
from tests.mytrail_support import (
    saved as saved_user,
)


async def test_setup_from_telegram_saves_account_and_card(trail_env) -> None:
    # given
    ctx = context()
    password = update(" test password ")

    # when
    await h.setup_entry(update(), ctx)
    await h.setup_ktx_id(update("test@example.test"), ctx)
    await h.setup_ktx_pw(password, ctx)
    await h.setup_card_number(update(CARD["number"]), ctx)
    await h.setup_card_pw(update(CARD["password"]), ctx)
    await h.setup_card_birthday(update(CARD["birthday"]), ctx)
    await h.setup_card_expire(update(CARD["expire"]), ctx)
    result = await h.setup_card_label(update("테스트 카드"), ctx)

    # then
    assert result == ConversationHandler.END
    saved = saved_user(111)
    assert saved["ktx"] == {"id": "test@example.test", "pw": " test password "}
    assert saved["cards"][0]["number"] == CARD["number"]
    assert saved["cards"][0]["label"] == "테스트 카드"
    assert "srt" not in saved
    assert "setup" not in ctx.user_data
    password.message.delete.assert_awaited_once()
    assert b"test password" not in storage._path(111).read_bytes()


async def test_two_users_register_independently_without_admin_credentials(trail_env) -> None:
    # given
    first, second = context(), context()

    # when
    await h.setup_entry(update(owner=111), first)
    await h.setup_entry(update(owner=222), second)
    await h.setup_ktx_id(update("first", owner=111), first)
    await h.setup_ktx_id(update("second", owner=222), second)
    await h.setup_ktx_pw(update("one", owner=111), first)
    await h.setup_ktx_pw(update("two", owner=222), second)
    await h.setup_card_number(update("skip", owner=222), second)
    await h.setup_card_number(update("skip", owner=111), first)

    # then
    assert saved_user(111)["ktx"] == {"id": "first", "pw": "one"}
    assert saved_user(222)["ktx"] == {"id": "second", "pw": "two"}
    assert saved_user(111)["cards"] == []
    cast(Mock, h.svc_auth.create_rail).assert_not_called()


async def test_setup_requires_overwrite_confirmation(trail_env) -> None:
    # given
    registered()
    ctx = context()

    # when
    first = await h.setup_entry(update(), ctx)
    second = await h.setup_entry(update(), ctx)

    # then
    assert first == ConversationHandler.END
    assert second == h.STATE_KTX_ID
    assert saved_user(111)["ktx"]["id"] == "user111@example.test"


async def test_setup_cancel_keeps_previous_saved_information(trail_env) -> None:
    # given
    previous = registered()
    ctx = context()
    ctx.user_data["setup"] = {"ktx": {"id": "new", "pw": "secret"}}

    # when
    await h.setup_cancel(update(), ctx)

    # then
    assert storage.load(111) == previous
    assert "setup" not in ctx.user_data


@pytest.mark.parametrize(("function", "state"), [(h.setup_ktx_id, h.STATE_KTX_ID), (h.setup_ktx_pw, h.STATE_KTX_PW)])
async def test_empty_credentials_stay_in_registration_step(trail_env, function, state) -> None:
    # given
    ctx = context()
    ctx.user_data["setup"] = {}

    # when
    result = await function(update(""), ctx)

    # then
    assert result == state
    assert not storage.exists(111)


@pytest.mark.parametrize(
    "function",
    [
        h.cmd_start,
        h.cmd_help,
        h.setup_entry,
        h.setup_ktx_id,
        h.setup_ktx_pw,
        h.setup_card_number,
        h.setup_card_pw,
        h.setup_card_birthday,
        h.setup_card_expire,
        h.setup_card_label,
        h.cmd_cards,
        h.on_free_message,
        h.cmd_cancel,
        h.cmd_status,
    ],
)
@pytest.mark.parametrize("allowed_ids", ["", "111,222"])
async def test_every_message_path_rejects_unallowed_user(trail_env, monkeypatch, function, allowed_ids) -> None:
    # given
    monkeypatch.setenv("BOT_ALLOWED_IDS", allowed_ids)
    event = update("secret", owner=999)
    ctx = context()

    # when
    result = await function(event, ctx)

    # then
    assert result == ConversationHandler.END
    assert "허용" in event.message.reply_text.call_args.args[0]
    assert "사용자 ID: 999" in event.message.reply_text.call_args.args[0]
    assert "관리자" in event.message.reply_text.call_args.args[0]
    assert not storage.exists(999)
    assert ctx.user_data == {}
    cast(Mock, h.svc_auth.create_rail).assert_not_called()


@pytest.mark.parametrize(
    "function",
    [
        h.on_page,
        h.on_pick,
        h.on_preset_card,
        h.on_payment_decision,
        h.on_cards_callback,
        h.cards_add_entry,
        h.cards_edit_entry,
        h.on_resolve,
    ],
)
@pytest.mark.parametrize("allowed_ids", ["", "111,222"])
async def test_every_callback_rejects_unallowed_user(trail_env, monkeypatch, function, allowed_ids) -> None:
    # given
    monkeypatch.setenv("BOT_ALLOWED_IDS", allowed_ids)
    event = update(owner=999, data="cards:add")

    # when
    await function(event, context())

    # then
    event.callback_query.answer.assert_awaited_once()
    event.callback_query.edit_message_text.assert_not_awaited()


async def test_group_chat_cannot_collect_credentials(trail_env) -> None:
    # given
    event = update("private-password")
    event.effective_chat.type = "group"

    # when
    await h.setup_ktx_pw(event, context())

    # then
    assert not storage.exists(111)
    assert "개인" in event.message.reply_text.call_args.args[0]


async def test_sensitive_message_deletion_failure_is_visible(trail_env) -> None:
    # given
    ctx = context()
    ctx.user_data["setup"] = {}
    event = update("test@example.test")
    event.message.delete.side_effect = RuntimeError()

    # when
    await h.setup_ktx_id(event, ctx)

    # then
    assert "직접 삭제" in event.message.reply_text.call_args_list[0].args[0]


async def test_cards_add_and_edit_follow_original_steps(trail_env) -> None:
    # given
    registered()
    ctx = context()

    # when
    await h.cards_add_entry(update(data="cards:add"), ctx)
    await h.cards_add_number(update(CARD["number"]), ctx)
    await h.cards_add_pw(update(CARD["password"]), ctx)
    await h.cards_add_birthday(update(CARD["birthday"]), ctx)
    await h.cards_add_expire(update(CARD["expire"]), ctx)
    await h.cards_add_label(update("카드1"), ctx)
    card_id = storage.list_cards(111)[0]["id"]
    await h.cards_edit_entry(update(data=f"cards:edit_field:{card_id}:label"), ctx)
    await h.cards_edit_value(update("수정한 카드"), ctx)

    # then
    assert saved_card(111, card_id)["label"] == "수정한 카드"
    assert "cards_new" not in ctx.user_data
    assert "cards_edit" not in ctx.user_data


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("label", "skip", None),
        ("number", "0000 0000 0000 0000", "0" * 16),
        ("password", "11", "11"),
        ("birthday", "111111", "111111"),
        ("expire", "3012", "3012"),
    ],
)
async def test_card_field_edits_are_user_scoped(trail_env, field, value, expected) -> None:
    # given
    registered(cards=True)
    registered(222, cards=True)
    other = storage.load(222)
    card_id = storage.list_cards(111)[0]["id"]
    ctx = context()

    # when
    await h.cards_edit_entry(update(data=f"cards:edit_field:{card_id}:{field}"), ctx)
    await h.cards_edit_value(update(value), ctx)

    # then
    assert saved_card(111, card_id)[field] == expected
    assert storage.load(222) == other


@pytest.mark.parametrize("action", ["del", "edit", "edit_done", "noop", "done"])
async def test_card_buttons_do_not_delete_without_confirmation(trail_env, action: str) -> None:
    # given
    registered(cards=True)
    card_id = storage.list_cards(111)[0]["id"]
    event = update(data=f"cards:{action}:{card_id}" if action not in {"noop", "done"} else f"cards:{action}")

    # when
    await h.on_cards_callback(event, context())

    # then
    assert len(storage.list_cards(111)) == 1
    event.callback_query.edit_message_text.assert_awaited_once()


async def test_card_delete_affects_only_own_card(trail_env) -> None:
    # given
    registered(cards=True)
    registered(222, cards=True)
    card_id = storage.list_cards(111)[0]["id"]

    # when
    await h.on_cards_callback(update(data=f"cards:del_confirm:{card_id}"), context())

    # then
    assert storage.list_cards(111) == []
    assert len(storage.list_cards(222)) == 1


@pytest.mark.parametrize("owner", [111, 222])
async def test_cards_list_contains_only_masked_own_cards(trail_env, owner: int) -> None:
    # given
    registered(owner, cards=True)
    event = update(owner=owner)

    # when
    await h.cmd_cards(event, context())

    # then
    text = event.message.reply_text.call_args.args[0]
    assert "0000" in text
    assert CARD["number"] not in text


@pytest.mark.parametrize(("function", "key"), [(h.cards_add_cancel, "cards_new"), (h.cards_edit_cancel, "cards_edit")])
async def test_card_conversation_cancel_clears_partial_secrets(trail_env, function, key) -> None:
    # given
    ctx = context()
    ctx.user_data[key] = {"password": "private"}

    # when
    await function(update(), ctx)

    # then
    assert key not in ctx.user_data


async def test_search_uses_registered_user_and_does_not_reserve(trail_env, monkeypatch) -> None:
    # given
    registered()
    rail = Mock()
    rail.search_train.return_value = [TRAIN]
    factory = Mock(return_value=rail)
    monkeypatch.setattr(h.svc_auth, "create_rail", factory)
    ctx = context()
    event = update("서울 부산 20991003 0900 KTX 어린이")

    # when
    await h.on_free_message(event, ctx)

    # then
    assert factory.call_args.kwargs["owner"] == 111
    assert factory.call_args.kwargs["credentials"] == saved_user(111)["ktx"]
    assert ctx.user_data["search"]["rail"] is rail
    rail.reserve.assert_not_called()
    assert "페이지" in event.message.reply_text.call_args.args[0]


@pytest.mark.parametrize("text", ["서울 부산", "서울 부산 20991003 0900 SRT"])
async def test_invalid_search_never_logs_in(trail_env, text: str) -> None:
    # given
    registered()
    event = update(text)

    # when
    await h.on_free_message(event, context())

    # then
    assert "형식" in event.message.reply_text.call_args.args[0]
    cast(Mock, h.svc_auth.create_rail).assert_not_called()


@pytest.mark.parametrize("result", [[], RuntimeError("private response")])
async def test_search_failure_closes_session_and_redacts(trail_env, monkeypatch, result) -> None:
    # given
    registered()
    rail = Mock()
    rail.search_train.side_effect = [result]
    monkeypatch.setattr(h.svc_auth, "create_rail", Mock(return_value=rail))
    event = update("서울 부산 20991003 0900")

    # when
    await h.on_free_message(event, context())

    # then
    rail.close.assert_called_once()
    assert "private response" not in event.message.reply_text.call_args.args[0]


async def test_login_failure_is_redacted(trail_env, monkeypatch) -> None:
    # given
    registered()
    monkeypatch.setattr(h.svc_auth, "create_rail", Mock(side_effect=LoginFailedError("private")))
    event = update("서울 부산 20991003 0900")

    # when
    await h.on_free_message(event, context())

    # then
    assert "로그인" in event.message.reply_text.call_args.args[0]
    assert "private" not in event.message.reply_text.call_args.args[0]


@pytest.mark.parametrize("data", ["pick:0", "pick:all:0"])
async def test_train_selection_offers_auto_or_manual_payment(trail_env, data) -> None:
    # given
    registered(cards=True)
    ctx = search_context()
    event = update(data=data)

    # when
    await h.on_pick(event, ctx)

    # then
    assert ctx.user_data["pending_indices"] == [0]
    assert "자동 결제" in event.callback_query.edit_message_text.call_args.args[0]
    ctx.user_data["search"]["rail"].reserve.assert_not_called()


async def test_old_search_buttons_cannot_act_on_new_search(trail_env) -> None:
    # given
    ctx = search_context()
    event = update(data="pick:0", message_id=9)

    # when
    await h.on_pick(event, ctx)

    # then
    assert "오래된" in event.callback_query.answer.call_args.args[0]
    assert "pending_indices" not in ctx.user_data


@pytest.mark.parametrize("data", ["pick:none", "pick:50", "page:5", "page:0"])
async def test_search_buttons_handle_cancel_invalid_page_and_selection(trail_env, data) -> None:
    # given
    ctx = search_context()
    event = update(data=data)
    function = h.on_page if data.startswith("page") else h.on_pick

    # when
    await function(event, ctx)

    # then
    event.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.parametrize("automatic", [False, True])
async def test_original_booking_and_payment_choice(trail_env, monkeypatch, automatic: bool) -> None:
    # given
    registered(cards=True)
    ctx = search_context()
    ctx.user_data["pending_indices"] = [0]
    rail = ctx.user_data["search"]["rail"]
    card_id = storage.list_cards(111)[0]["id"]
    monkeypatch.setattr(
        h.svc_resv, "poll_and_reserve", lambda rail, params, indices, option, success, *rest: success(RESERVATION)
    )
    data = f"preset:card:{card_id}" if automatic else "preset:manual"

    # when
    await h.on_preset_card(update(data=data), ctx)
    await h._SESSION.wait_poll(111)
    await asyncio.sleep(0)

    # then
    assert rail.pay_with_card.call_count == int(automatic)
    assert (h._SESSION.get_pending(111) is None) is automatic
    assert "search" not in ctx.user_data
    assert ctx.bot.send_message.await_count >= 1


async def test_auto_payment_exception_does_not_offer_second_charge(trail_env, monkeypatch) -> None:
    # given
    registered(cards=True)
    ctx = search_context()
    ctx.user_data["pending_indices"] = [0]
    rail = ctx.user_data["search"]["rail"]
    rail.pay_with_card.side_effect = TimeoutError()
    card_id = storage.list_cards(111)[0]["id"]
    monkeypatch.setattr(
        h.svc_resv, "poll_and_reserve", lambda rail, params, indices, option, success, *rest: success(RESERVATION)
    )

    # when
    await h.on_preset_card(update(data=f"preset:card:{card_id}"), ctx)
    await h._SESSION.wait_poll(111)
    await asyncio.sleep(0)

    # then
    rail.pay_with_card.assert_called_once()
    assert h._SESSION.get_pending(111) is None
    assert "자동 재결제하지 않습니다" in ctx.bot.send_message.call_args.kwargs["text"]


async def test_cancel_during_reserve_prevents_selected_auto_payment(trail_env, monkeypatch) -> None:
    # given
    registered(cards=True)
    ctx = search_context()
    ctx.user_data["pending_indices"] = [0]
    rail = ctx.user_data["search"]["rail"]
    card_id = storage.list_cards(111)[0]["id"]

    def reserve_after_stop(rail, params, indices, option, success, error, stop, passengers, selected) -> None:
        stop.set()
        success(RESERVATION)

    monkeypatch.setattr(h.svc_resv, "poll_and_reserve", reserve_after_stop)

    # when
    await h.on_preset_card(update(data=f"preset:card:{card_id}"), ctx)
    await h._SESSION.wait_poll(111)
    await asyncio.sleep(0)

    # then
    rail.pay_with_card.assert_not_called()
    assert h._SESSION.get_pending(111) is not None


async def test_waitlist_does_not_trigger_auto_payment(trail_env, monkeypatch) -> None:
    # given
    from dataclasses import replace

    registered(cards=True)
    ctx = search_context()
    ctx.user_data["pending_indices"] = [0]
    rail = ctx.user_data["search"]["rail"]
    card_id = storage.list_cards(111)[0]["id"]
    waiting = replace(RESERVATION, buy_limit_date="00000000")
    monkeypatch.setattr(
        h.svc_resv, "poll_and_reserve", lambda rail, params, indices, option, success, *rest: success(waiting)
    )

    # when
    await h.on_preset_card(update(data=f"preset:card:{card_id}"), ctx)
    await h._SESSION.wait_poll(111)
    await asyncio.sleep(0)

    # then
    rail.pay_with_card.assert_not_called()
    assert "예약대기" in ctx.bot.send_message.call_args.kwargs["text"]
    buttons = ctx.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    assert [button.callback_data for row in buttons for button in row] == ["pay:cancel"]


async def test_failed_manual_payment_cannot_be_clicked_again(trail_env) -> None:
    # given
    registered(cards=True)
    rail = pending()
    rail.pay_with_card.side_effect = TimeoutError("private")
    card_id = storage.list_cards(111)[0]["id"]
    event = update(data=f"pay:card:{card_id}")

    # when
    await h.on_payment_decision(event, context())
    await h.on_payment_decision(event, context())

    # then
    rail.pay_with_card.assert_called_once()
    assert "private" not in event.callback_query.edit_message_text.call_args.args[0]
    assert h._SESSION.get_pending(111) is None


@pytest.mark.parametrize("function", [h.setup_entry, h.on_free_message])
async def test_unresolved_reservation_blocks_setup_and_new_booking(trail_env, function) -> None:
    # given
    pending()
    event = update("서울 부산 20991003 0900")

    # when
    await function(event, context())

    # then
    assert "확인 대기" in event.message.reply_text.call_args.args[0]


async def test_private_exception_is_redacted_and_partial_secrets_cleared(trail_env, monkeypatch, caplog) -> None:
    # given
    ctx = context()
    ctx.user_data["setup"] = {"password": "private-secret"}
    monkeypatch.setattr(storage, "exists", Mock(side_effect=RuntimeError("private-secret")))

    # when
    await h.cmd_cards(update(), ctx)

    # then
    assert "private-secret" not in caplog.text
    assert "setup" not in ctx.user_data
    assert "private-secret" not in ctx.bot.send_message.call_args.args[1]


@pytest.mark.parametrize("data", ["pay:confirm", "pay:back"])
async def test_manual_payment_confirmation_displays_cards(trail_env, data) -> None:
    # given
    registered(cards=True)
    rail = pending()
    event = update(data=data)

    # when
    await h.on_payment_decision(event, context())

    # then
    rail.pay_with_card.assert_not_called()
    event.callback_query.edit_message_text.assert_awaited_once()


async def test_selected_manual_card_pays_once(trail_env) -> None:
    # given
    registered(cards=True)
    rail = pending()
    card_id = storage.list_cards(111)[0]["id"]
    event = update(data=f"pay:card:{card_id}")
    ctx = context()

    # when
    await h.on_payment_decision(event, ctx)
    await h.on_payment_decision(event, ctx)

    # then
    rail.pay_with_card.assert_called_once()
    assert h._SESSION.get_pending(111) is None


@pytest.mark.parametrize("failure", [False, True])
async def test_cancel_reports_only_confirmed_success(trail_env, failure) -> None:
    # given
    rail = pending()
    rail.cancel.side_effect = TimeoutError() if failure else None
    event = update(data="pay:cancel")

    # when
    await h.on_payment_decision(event, context())

    # then
    assert (h._SESSION.get_pending(111) is not None) is failure
    assert ("예약 취소됨" in event.callback_query.edit_message_text.call_args.args[0]) is not failure


async def test_status_resolution_only_releases_local_hold(trail_env) -> None:
    # given
    rail = pending()
    ctx = context()
    event = update()
    await h.cmd_status(event, ctx)
    nonce = ctx.user_data["resolve"][0]

    # when
    await h.on_resolve(update(data=f"resolve:{nonce}"), ctx)

    # then
    assert journal.current(111) is None
    rail.cancel.assert_not_called()
    rail.pay_with_card.assert_not_called()


@pytest.mark.parametrize(
    "function", [h.cmd_start, h.cmd_help, h.cmd_cards, h.cmd_cancel, h.cmd_status, h.on_free_message]
)
async def test_initial_commands_work_without_server_credentials(trail_env, function) -> None:
    # given
    event = update()

    # when
    await function(event, context())

    # then
    event.message.reply_text.assert_awaited()
