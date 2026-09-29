"""설정 CLI는 자격증명을 입력받아 저장할 뿐 실제 로그인을 하지 않습니다."""

from __future__ import annotations

import importlib
from dataclasses import replace
from getpass import GetPassWarning
from unittest.mock import Mock

import pytest

from pykorail_bot.domain import BotError
from tests.bot_support import Harness

cli = importlib.import_module("pykorail_bot.main")


def env_for(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", harness.settings.token)
    monkeypatch.setenv("BOT_DB_KEY", harness.settings.key)
    monkeypatch.setenv("BOT_ALLOWED_IDS", "1,2")
    monkeypatch.setenv("BOT_DATA_DIR", str(harness.store.directory))
    monkeypatch.setenv("BOT_ENABLE_PAYMENTS", "false")
    monkeypatch.setattr(cli, "load_dotenv", Mock())


def test_hidden_input_refuses_echo_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    monkeypatch.setattr(cli, "getpass", Mock(side_effect=GetPassWarning))

    # when & then
    with pytest.raises(BotError):
        cli.hidden("secret:")


def test_hidden_input_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    monkeypatch.setattr(cli, "getpass", Mock(return_value="test-only"))

    # when
    value = cli.hidden("secret:")

    # then
    assert value == "test-only"


def test_setup_saves_without_contacting_korail(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    monkeypatch.setattr(cli, "hidden", Mock(side_effect=["1" * 10, "new-test-only"]))
    monkeypatch.setattr("builtins.input", Mock(side_effect=["n", "y"]))

    # when
    cli.setup(harness.store, harness.settings, 2)

    # then
    assert harness.store.credential(2).password == "new-test-only"
    assert harness.store.credential(2).profile == "default"
    assert harness.store.credential(2).card is None
    harness.backend.search.assert_not_awaited()


def test_setup_can_save_card(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    monkeypatch.setattr(cli, "hidden", Mock(side_effect=["1" * 10, "test-only", "0" * 16, "00", "000000", "9912"]))
    monkeypatch.setattr("builtins.input", Mock(side_effect=["y", "n", "y"]))

    # when
    cli.setup(harness.store, harness.settings, 2)

    # then
    assert harness.store.credential(2).card is not None


def test_invalid_card_is_not_saved(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    monkeypatch.setattr(cli, "hidden", Mock(side_effect=["1" * 10, "test-only", "not-card", "00", "000000", "9912"]))
    monkeypatch.setattr("builtins.input", Mock(side_effect=["y", "n"]))

    # when & then
    with pytest.raises(BotError):
        cli.setup(harness.store, harness.settings, 2)
    assert not harness.store.has_credential(2)


def test_cancelled_setup_preserves_existing_record(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    monkeypatch.setattr(cli, "hidden", Mock(side_effect=["1" * 10, "new-test-only"]))
    monkeypatch.setattr("builtins.input", Mock(side_effect=["n", "n"]))

    # when
    cli.setup(harness.store, harness.settings, 1)

    # then
    assert harness.store.credential(1) == harness.credential


def test_setup_does_not_change_profile(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    monkeypatch.setattr(cli, "hidden", Mock(side_effect=["0" * 10, "new-test-only"]))
    monkeypatch.setattr("builtins.input", Mock(side_effect=["n", "y"]))

    # when
    cli.setup(harness.store, harness.settings, 1)

    # then
    assert harness.store.credential(1).profile == harness.credential.profile


def test_setup_requires_allowed_id(harness: Harness) -> None:
    # when & then
    with pytest.raises(BotError):
        cli.setup(harness.store, harness.settings, 99)


def test_pending_reservation_prevents_credential_replacement(harness: Harness) -> None:
    # given
    harness.reserved()

    # when & then
    with pytest.raises(BotError):
        cli.setup(harness.store, harness.settings, 1)


def test_new_owner_cannot_replace_pending_account(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    harness.reserved()
    monkeypatch.setattr(cli, "hidden", Mock(side_effect=[harness.credential.membership, "test-only"]))
    monkeypatch.setattr("builtins.input", Mock(return_value="n"))

    # when & then
    with pytest.raises(BotError):
        cli.setup(harness.store, harness.settings, 2)


def test_keygen_needs_no_credentials(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # given
    monkeypatch.setattr(cli.Fernet, "generate_key", Mock(return_value=b"test-key-placeholder"))

    # when
    cli.main(["keygen"])

    # then
    assert capsys.readouterr().out.strip() == "test-key-placeholder"


def test_cli_setup_releases_instance_lock(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    env_for(harness, monkeypatch)
    registration = Mock()
    monkeypatch.setattr(cli, "setup", registration)

    # when
    cli.main(["setup", "--user", "1"])

    # then
    registration.assert_called_once()
    harness.store.acquire()
    harness.store.close()


def test_cli_run_recovers_state_before_polling(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    env_for(harness, monkeypatch)
    harness.store.save(replace(harness.reserved(), state="PAYING"))
    application = Mock()
    monkeypatch.setattr(cli, "build_application", Mock(return_value=application))

    # when
    cli.main(["run"])

    # then
    assert harness.store.get(1, "job").state == "UNKNOWN"
    application.run_polling.assert_called_once_with(
        drop_pending_updates=True, allowed_updates=["message", "callback_query"]
    )


@pytest.mark.parametrize("error", [BotError("사용자 오류"), KeyboardInterrupt(), EOFError(), RuntimeError("secret")])
def test_cli_errors_do_not_leak_secrets(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: BaseException
) -> None:
    # given
    env_for(harness, monkeypatch)
    monkeypatch.setattr(cli, "setup", Mock(side_effect=error))

    # when & then
    with pytest.raises(SystemExit) as result:
        cli.main(["setup", "--user", "1"])
    assert result.value.code == 1
    assert "secret" not in capsys.readouterr().err
