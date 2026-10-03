"""사용자별 기본 ID 복원과 텔레그램 설정 대화를 검증합니다."""

from __future__ import annotations

import pytest
from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import ConversationHandler

from srtgo.bot import device_settings as settings
from srtgo.bot import main, storage
from srtgo.service import journal
from tests.mytrail_support import context, search_context, update

CUSTOM = "0123456789abcdef"


def test_custom_identity_keeps_original_and_other_users(trail_env) -> None:
    # given
    original = storage.android_id_for(111)
    other = storage.android_id_for(222)
    # when
    storage.set_android_id(111, CUSTOM)
    # then
    data = storage.load(111)
    assert data is not None
    assert data["default_android_id"] == original
    assert storage.android_id_for(111) == CUSTOM
    assert storage.android_id_for(222) == other


def test_reset_restores_first_identity_after_multiple_overrides(trail_env) -> None:
    # given
    original = storage.android_id_for(111)
    storage.set_android_id(111, CUSTOM)
    storage.set_android_id(111, "fedcba9876543210")
    # when
    storage.set_android_id(111, None)
    # then
    assert storage.android_id_for(111) == original


def test_old_record_uses_existing_id_as_default(trail_env) -> None:
    # given
    storage.save(111, {"ktx": None, "cards": [], "android_id": CUSTOM})
    # when
    result = storage.android_id_for(111)
    # then
    data = storage.load(111)
    assert data is not None
    assert result == CUSTOM
    assert data["default_android_id"] == CUSTOM


def test_registration_preserves_default_and_override(trail_env) -> None:
    # given
    original = storage.android_id_for(111)
    storage.set_android_id(111, CUSTOM)
    # when
    storage.save(111, {"ktx": {"id": "test@example.test"}, "cards": []})
    # then
    data = storage.load(111)
    assert data is not None
    assert data["android_id"] == CUSTOM
    assert data["default_android_id"] == original


@pytest.mark.parametrize("value", ["", "bad", "ABCDEF0123456789"])
def test_invalid_override_does_not_change_saved_identity(trail_env, value) -> None:
    # given
    original = storage.android_id_for(111)
    # when & then
    with pytest.raises(ValueError):
        storage.set_android_id(111, value)
    assert storage.android_id_for(111) == original


async def test_menu_has_two_choices(trail_env) -> None:
    # given
    event, ctx = update("/dev-set"), context()
    # when
    result = await settings.entry(event, ctx)
    # then
    assert result == settings.MENU
    buttons = event.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert [button.text for row in buttons for button in row] == ["기본값 사용", "dev_id 임의 입력"]
    assert ctx.user_data["device_settings_message"] == 10


async def test_custom_choice_prompts_for_input(trail_env) -> None:
    # given
    ctx = context()
    ctx.user_data["device_settings_message"] = 10
    event = update(data="dev:custom")
    # when
    result = await settings.choose(event, ctx)
    # then
    assert result == settings.INPUT
    assert "16자리" in event.callback_query.edit_message_text.call_args.args[0]


async def test_default_choice_closes_old_search_and_resets(trail_env) -> None:
    # given
    original = storage.android_id_for(111)
    storage.set_android_id(111, CUSTOM)
    ctx = search_context()
    rail = ctx.user_data["search"]["rail"]
    ctx.user_data.update(device_settings_message=10, pending_indices=[0])
    event = update(data="dev:default")
    # when
    result = await settings.choose(event, ctx)
    # then
    assert result == ConversationHandler.END
    assert storage.android_id_for(111) == original
    assert "search" not in ctx.user_data
    assert "pending_indices" not in ctx.user_data
    rail.close.assert_called_once()


async def test_input_is_saved_without_echo_and_old_session_is_closed(trail_env) -> None:
    # given
    ctx = search_context()
    rail = ctx.user_data["search"]["rail"]
    event = update("  " + CUSTOM + "  ")
    # when
    result = await settings.receive(event, ctx)
    # then
    assert result == ConversationHandler.END
    assert storage.android_id_for(111) == CUSTOM
    event.message.delete.assert_awaited_once()
    assert CUSTOM not in event.message.reply_text.call_args.args[0]
    rail.close.assert_called_once()


async def test_invalid_input_keeps_waiting_and_identity(trail_env) -> None:
    # given
    original = storage.android_id_for(111)
    event = update("wrong")
    # when
    result = await settings.receive(event, context())
    # then
    assert result == settings.INPUT
    assert storage.android_id_for(111) == original


async def test_cancel_keeps_current_identity(trail_env) -> None:
    # given
    original = storage.android_id_for(111)
    ctx = context()
    ctx.user_data["device_settings_message"] = 10
    # when
    result = await settings.cancel(update("/cancel"), ctx)
    # then
    assert result == ConversationHandler.END
    assert "device_settings_message" not in ctx.user_data
    assert storage.android_id_for(111) == original


async def test_stale_menu_cannot_reset_override(trail_env) -> None:
    # given
    storage.set_android_id(111, CUSTOM)
    ctx = context()
    ctx.user_data["device_settings_message"] = 11
    event = update(data="dev:default", message_id=10)
    # when
    result = await settings.choose(event, ctx)
    # then
    assert result == settings.MENU
    assert storage.android_id_for(111) == CUSTOM
    event.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize("step", ["entry", "choose", "receive"])
async def test_busy_account_cannot_change_identity(trail_env, step) -> None:
    # given
    original = storage.android_id_for(111)
    journal.begin(111, "test-account", "RESERVING")
    journal.uncertain(111)
    ctx = context()
    ctx.user_data["device_settings_message"] = 10
    event = update(CUSTOM, data="dev:default")
    # when
    result = await getattr(settings, step)(event, ctx)
    # then
    assert result == ConversationHandler.END
    assert storage.android_id_for(111) == original


@pytest.mark.parametrize("step", ["entry", "choose", "receive", "cancel"])
async def test_unallowed_user_cannot_access_settings(trail_env, step) -> None:
    # given
    event = update(CUSTOM, owner=333, data="dev:default")
    # when
    result = await getattr(settings, step)(event, context())
    # then
    assert result == ConversationHandler.END
    assert not storage.exists(333)


def test_hyphen_alias_matches_real_text_without_command_entity() -> None:
    # given
    import datetime as dt

    message = Message(
        message_id=10,
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(111, "private"),
        from_user=User(111, "test", False),
        text="/dev-set",
        entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, 4)],
    )
    handler = main._build_device_conversation().entry_points[1]
    # when
    match = handler.check_update(Update(1, message=message))
    # then
    assert match
