"""등록 정보를 미리 받지 않는 시작 명령과 myTrail 입력 호환성."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from telegram.ext import CommandHandler, ConversationHandler

from srtgo.bot import auth_guard, handlers, main, notifier, parser, storage
from srtgo.service import journal
from tests.mytrail_support import RESERVATION


def test_application_uses_mytrail_conversations_before_global_cancel() -> None:
    # when
    app = main.build_application("12345:test-token")

    # then
    registered = app.handlers[0]
    conversations = [i for i, h in enumerate(registered) if isinstance(h, ConversationHandler)]
    cancel = next(i for i, h in enumerate(registered) if isinstance(h, CommandHandler) and "cancel" in h.commands)
    assert len(conversations) == 3
    assert max(conversations) < cancel
    assert app.concurrent_updates == 1
    setup = registered[conversations[0]]
    assert isinstance(setup, ConversationHandler)
    assert setup.entry_points[0].callback is handlers.setup_entry


def test_run_needs_no_user_credential(trail_env, monkeypatch) -> None:
    # given
    app = Mock()
    monkeypatch.setattr(main, "build_application", Mock(return_value=app))
    monkeypatch.setattr(main, "load_dotenv", Mock())

    # when
    main.main(["run"])

    # then
    app.run_polling.assert_called_once_with(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    assert storage.list_user_ids() == []
    journal.acquire_instance().close()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BOT_TOKEN", ""),
        ("BOT_DB_KEY", "invalid"),
        ("BOT_POLL_SECONDS", "1"),
        ("BOT_POLL_SECONDS", "nan"),
        ("BOT_ENABLE_PAYMENTS", "false"),
    ],
)
def test_invalid_admin_settings_prevent_polling(trail_env, monkeypatch, key, value) -> None:
    # given
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(main, "load_dotenv", Mock())
    build = Mock()
    monkeypatch.setattr(main, "build_application", build)

    # when & then
    with pytest.raises(SystemExit) as error:
        main.main(["run"])
    assert error.value.code == 1
    build.assert_not_called()


@pytest.mark.parametrize("allowed_ids", ["", "  ", "bad"])
def test_empty_allowlist_starts_bot_for_id_discovery(trail_env, monkeypatch, caplog, allowed_ids) -> None:
    # given
    app = Mock()
    monkeypatch.setenv("BOT_ALLOWED_IDS", allowed_ids)
    monkeypatch.setattr(main, "build_application", Mock(return_value=app))
    monkeypatch.setattr(main, "load_dotenv", Mock())

    # when
    main.main(["run"])

    # then
    app.run_polling.assert_called_once_with(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    assert "/setup" in caplog.text
    assert not auth_guard.is_allowed(111)


def test_missing_allowlist_starts_bot_for_id_discovery(trail_env, monkeypatch) -> None:
    # given
    app = Mock()
    monkeypatch.delenv("BOT_ALLOWED_IDS")
    monkeypatch.setattr(main, "build_application", Mock(return_value=app))
    monkeypatch.setattr(main, "load_dotenv", Mock())

    # when
    main.main(["run"])

    # then
    app.run_polling.assert_called_once_with(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    assert not auth_guard.is_allowed(111)


def test_old_bot_data_requires_explicit_migration(trail_env, monkeypatch) -> None:
    # given
    old = trail_env / "old-bot"
    old.mkdir()
    (old / "jobs.sqlite3").touch()
    monkeypatch.setattr(main, "load_dotenv", Mock())
    build = Mock()
    monkeypatch.setattr(main, "build_application", build)

    # when & then
    with pytest.raises(SystemExit):
        main.main(["run"])
    build.assert_not_called()


def test_startup_error_redacts_token_and_releases_lock(trail_env, monkeypatch, capsys) -> None:
    # given
    monkeypatch.setattr(main, "load_dotenv", Mock())
    monkeypatch.setattr(main, "build_application", Mock(side_effect=RuntimeError("private-token")))

    # when & then
    with pytest.raises(SystemExit):
        main.main(["run"])
    assert "private-token" not in capsys.readouterr().err
    journal.acquire_instance().close()


def test_keygen_does_not_start_bot(monkeypatch, capsys) -> None:
    # given
    monkeypatch.setattr(main.Fernet, "generate_key", lambda: b"test-generated-key")

    # when
    main.main(["keygen"])

    # then
    assert capsys.readouterr().out.strip() == "test-generated-key"


async def test_shutdown_closes_idle_searches(trail_env) -> None:
    # given
    rail = Mock()
    app = Mock(user_data={111: {"search": {"rail": rail}}, 222: {}})

    # when
    await main.shutdown(app)

    # then
    rail.close.assert_called_once()


async def test_error_handler_never_logs_update_or_exception_text(caplog) -> None:
    # given
    ctx = Mock(error=RuntimeError("private-response"))

    # when
    await main.error_handler("private-update", ctx)

    # then
    assert "RuntimeError" in caplog.text
    assert "private-response" not in caplog.text
    assert "private-update" not in caplog.text


@pytest.mark.parametrize(
    ("text", "date", "time"),
    [
        ("서울 부산 20991003 0900", "2099-10-03", "090000"),
        ("서울 부산 2099-10-03 09:00 KTX", "2099-10-03", "090000"),
        ("서울 부산 10/3 090000 코레일", "2099-10-03", "090000"),
    ],
)
def test_mytrail_date_formats_default_to_ktx(text, date, time) -> None:
    # when
    result = parser.parse(text, today="2099-01-01")

    # then
    assert result["rail"] == "KTX"
    assert result["date"] == date
    assert result["time"] == time


@pytest.mark.parametrize(
    ("word", "passenger"),
    [
        ("어린이", "child"),
        ("유아", "toddler"),
        ("경로", "senior"),
        ("중증장애인", "disability1to3"),
        ("경증장애인", "disability4to6"),
    ],
)
def test_mytrail_passenger_types_use_pykorail_models(word, passenger) -> None:
    # given
    result = parser.parse(f"서울 부산 20991003 0900 특실우선 {word}")

    # when
    passengers = handlers._passengers_to_list("KTX", result["passengers"])

    # then
    assert result["seat_pref"] == "SPECIAL_FIRST"
    assert result["passengers"][passenger] == 1
    assert result["passengers"]["adult"] == 0
    assert len(passengers) == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "서울 부산 bad 0900",
        "서울 부산 20991303 0900",
        "서울 부산 20991003 bad",
        "서울 부산 20991003 2560",
        "서울 서울 20991003 0900",
        "서울 부산 20991003 0900 SRT",
    ],
)
def test_invalid_or_srt_query_is_rejected(text: str) -> None:
    # when & then
    with pytest.raises(parser.ParseError):
        parser.parse(text)


@pytest.mark.parametrize(("raw", "expected"), [("111, 222,,bad", {111, 222}), ("", set())])
def test_allowlist_preserves_mytrail_policy(monkeypatch, raw, expected) -> None:
    # given
    monkeypatch.setenv("BOT_ALLOWED_IDS", raw)

    # when
    result = auth_guard.get_allowed_ids()

    # then
    assert result == expected


async def test_notification_failure_does_not_expose_payload(caplog) -> None:
    # given
    bot = Mock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("private"))

    # when
    await notifier.send_text(bot, 111, "test")

    # then
    assert "private" not in caplog.text


def test_waiting_reservation_is_not_described_as_secured_seat() -> None:
    # given
    from dataclasses import replace

    reservation = replace(RESERVATION, buy_limit_date="00000000")

    # when
    text = notifier.format_seat_secured_message(reservation)

    # then
    assert "예약대기" in text
    assert "좌석 확보!" not in text
