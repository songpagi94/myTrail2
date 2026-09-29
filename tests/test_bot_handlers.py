"""텔레그램 객체는 가짜로 두고 개인 채팅·버튼·알림을 검증합니다."""

from __future__ import annotations

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Bot, Update
from telegram.ext import ContextTypes

from pykorail_bot.domain import BotError
from pykorail_bot.handlers import HELP, BotUI, build_application, job_buttons, train_page
from tests.bot_support import TRAIN, Harness


def fake_update(owner: int = 1, chat_type: str = "private", data: str | None = None) -> Update:
    message = Mock(text="서울 부산 20991003 0900", reply_text=AsyncMock())
    callback = Mock(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()) if data is not None else None
    return cast(
        "Update",
        Mock(
            effective_user=Mock(id=owner),
            effective_chat=Mock(id=owner, type=chat_type),
            effective_message=message,
            callback_query=callback,
        ),
    )


@pytest.fixture
def ui(harness: Harness) -> BotUI:
    interface = BotUI(harness.settings, harness.store)
    interface.controller = harness.controller
    interface.bot = cast("Bot", Mock(send_message=AsyncMock()))
    return interface


@pytest.fixture
def context() -> ContextTypes.DEFAULT_TYPE:
    return cast("ContextTypes.DEFAULT_TYPE", Mock(args=[], error=RuntimeError("secret")))


@pytest.mark.parametrize(("owner", "chat_type"), [(99, "private"), (1, "group"), (1, "supergroup")])
async def test_disallowed_messages_do_not_query(
    ui: BotUI, harness: Harness, context, owner: int, chat_type: str
) -> None:
    # given
    update = fake_update(owner, chat_type)

    # when
    await ui.text(update, context)

    # then
    harness.backend.search.assert_not_awaited()


@pytest.mark.parametrize("data", ["start:old:nonce", "pay:job:nonce", "ackdone:job", "check:job", "pick:old:0"])
async def test_every_callback_checks_authorization(ui: BotUI, harness: Harness, context, data: str) -> None:
    # given
    update = fake_update(99, data=data)

    # when
    await ui.callback(update, context)

    # then
    harness.backend.pay.assert_not_awaited()
    harness.backend.reserve.assert_not_awaited()
    assert harness.store.records() == []


async def test_query_renders_read_only_results(ui: BotUI, harness: Harness, context) -> None:
    # given
    update = fake_update()

    # when
    await ui.text(update, context)

    # then
    assert ui.controller.searches[1].trains == (TRAIN,)
    harness.backend.reserve.assert_not_awaited()
    await ui.controller.close()


async def test_query_error_is_reported_without_raw_response(ui: BotUI, harness: Harness, context) -> None:
    # given
    harness.backend.search.side_effect = RuntimeError("secret response")
    update = fake_update()

    # when
    await ui.text(update, context)

    # then
    assert "secret response" not in str(cast("Mock", update.effective_message).reply_text.call_args_list)


@pytest.mark.parametrize("method", ["help", "setup", "stop", "status"])
async def test_basic_commands_work(ui: BotUI, context, method: str) -> None:
    # given
    update = fake_update()

    # when
    await getattr(ui, method)(update, context)

    # then
    cast("Mock", update.effective_message).reply_text.assert_awaited_once()


async def test_status_displays_persisted_reservation(ui: BotUI, harness: Harness, context) -> None:
    # given
    harness.reserved()
    update = fake_update()

    # when
    await ui.status(update, context)

    # then
    assert "RESERVED" in str(cast("Mock", update.effective_message).reply_text.call_args)


@pytest.mark.parametrize("args", [[], ["job"], ["missing"]])
async def test_check_command_handles_arguments(ui: BotUI, harness: Harness, context, args: list[str]) -> None:
    # given
    harness.reserved()
    context.args = args
    update = fake_update()

    # when
    await ui.check(update, context)

    # then
    cast("Mock", update.effective_message).reply_text.assert_awaited_once()


@pytest.mark.parametrize("action", ["pick", "pickall", "page"])
async def test_selection_and_page_buttons_do_not_reserve(ui: BotUI, harness: Harness, context, action: str) -> None:
    # given
    search = await harness.search()
    update = fake_update(data=f"{action}:{search.id}:0")

    # when
    await ui.callback(update, context)

    # then
    cast("Mock", update.callback_query).edit_message_text.assert_awaited_once()
    harness.backend.reserve.assert_not_awaited()
    await ui.controller.close()


async def test_start_button_launches_confirmed_job(ui: BotUI, harness: Harness, context) -> None:
    # given
    search = await harness.search()
    selected = harness.controller.prepare(1, search.id, [0])
    update = fake_update(data=f"start:{search.id}:{selected.confirmation}")

    # when
    await ui.callback(update, context)
    await harness.controller.tasks[1][1]

    # then
    assert harness.store.get(1, search.id).state == "RESERVED"


@pytest.mark.parametrize("data", ["bad", "unknown:id", "pick:old:bad", "start:old:token", "ackdone:missing"])
async def test_invalid_callbacks_are_safe(ui: BotUI, context, data: str) -> None:
    # given
    update = fake_update(data=data)

    # when
    await ui.callback(update, context)

    # then
    cast("Mock", update.callback_query).edit_message_text.assert_awaited_once()


@pytest.mark.parametrize("action", ["ack", "ackdone", "check", "quote"])
async def test_reservation_buttons_are_bound_to_job(ui: BotUI, harness: Harness, context, action: str) -> None:
    # given
    harness.reserved()
    update = fake_update(data=f"{action}:job")

    # when
    await ui.callback(update, context)

    # then
    cast("Mock", update.callback_query).edit_message_text.assert_awaited_once()
    harness.backend.pay.assert_not_awaited()


async def test_payment_button_uses_one_time_quote(ui: BotUI, harness: Harness, context) -> None:
    # given
    harness.reserved()
    quote = await harness.controller.quote(1, "job")
    update = fake_update(data=f"pay:job:{quote.nonce}")

    # when
    await ui.callback(update, context)

    # then
    harness.backend.pay.assert_awaited_once()


async def test_notification_and_shutdown(ui: BotUI, harness: Harness) -> None:
    # given
    record = harness.reserved()

    # when
    await ui.notify(record)
    await ui.shutdown(Mock())

    # then
    cast("Mock", ui.bot).send_message.assert_awaited_once()


async def test_error_handler_does_not_log_secrets(ui: BotUI, context, caplog: pytest.LogCaptureFixture) -> None:
    # when
    await ui.error(Mock(), context)

    # then
    assert "secret" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_pages_have_boundaries(harness: Harness) -> None:
    # given
    search = replace(await harness.search(), trains=(TRAIN,) * 12)

    # when
    text, keyboard = train_page(search, 1)

    # then
    assert "2/3" in text
    assert len(keyboard.inline_keyboard[1]) == 2
    with pytest.raises(BotError):
        train_page(search, -1)
    with pytest.raises(BotError):
        train_page(search, 3)
    await harness.controller.close()


def test_buttons_exclude_payment_for_unknown_result(harness: Harness) -> None:
    # given
    record = replace(harness.reserved(), state="UNKNOWN")

    # when
    keyboard = job_buttons(record, True)

    # then
    assert "quote:" not in str(keyboard)
    assert job_buttons(replace(record, state="PAID"), True) is None
    assert "비밀번호" in HELP


def test_application_builds_without_network(harness: Harness) -> None:
    # when
    app = build_application(harness.settings, harness.store)

    # then
    assert len(app.handlers[0]) == 7
    assert app.post_stop is not None
