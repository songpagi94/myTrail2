"""KTX 입력, 안전한 설정, 암호화 저장과 재시작 상태."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from pykorail import ReserveOption
from pykorail.options import ReserveOptionCode
from pykorail_bot.config import Settings
from pykorail_bot.domain import BotError, JobRecord, State, TrainKey, parse_query, seat_available
from pykorail_bot.storage import Credential, Store
from tests.bot_support import FUTURE, TRAIN, Harness


def test_query_parses_kst_and_passengers() -> None:
    # when
    query = parse_query("서울 부산 20991003 0900 KTX 특실우선 성인2 어린이1 경로1")

    # then
    assert query.after == FUTURE
    assert query.seat == ReserveOption.SPECIAL_FIRST
    assert [p.count for p in query.passengers()] == [2, 1, 1]
    assert "KST" in query.summary()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "서울 서울 20991003 0900",
        "서울 부산 20991303 0900",
        "서울 부산 20991003 2560",
        "서울 부산 2099103 0900",
        "서울 부산 20000101 0900",
        "서울 부산 20991003 0900 SRT",
        "서울 부산 20991003 0900 성인0",
        "서울 부산 20991003 0900 성인9 어린이1",
        "서울 부산 20991003 0900 성인-1",
        "서울 부산 20991003 0900 성인1 성인2",
        "서울 부산 20991003 0900 일반만 특실만",
        "서울 부산 20991003 0900 KTX KTX",
        "서울 부산 20991003 0900 성인100",
        "서울 부산 20991003 0900 KTX 일반만 성인1 어린이0 경로0 추가",
    ],
)
def test_invalid_query_is_rejected(text: str) -> None:
    # when & then
    with pytest.raises(BotError):
        parse_query(text)


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        (ReserveOption.GENERAL_ONLY, False),
        (ReserveOption.SPECIAL_ONLY, True),
        (ReserveOption.GENERAL_FIRST, True),
        (ReserveOption.SPECIAL_FIRST, True),
    ],
)
def test_seat_preference_is_preserved(option: ReserveOptionCode, expected: bool) -> None:
    # given
    train = replace(TRAIN, general_seat="00")

    # when
    available = seat_available(train, option)

    # then
    assert available is expected


def test_waitlist_is_not_a_seat() -> None:
    # given
    train = replace(TRAIN, general_seat="00", special_seat="00", wait_reserve_flag=9)

    # when
    available = seat_available(train, ReserveOption.GENERAL_FIRST)

    # then
    assert not available


def test_train_key_uses_stable_identity() -> None:
    # when
    key = TrainKey.of(TRAIN)

    # then
    assert key == TrainKey.of(replace(TRAIN, general_seat="00"))
    assert key != TrainKey.of(replace(TRAIN, train_no="other"))
    assert key.departure_time() == FUTURE


def test_incomplete_train_identity_is_rejected() -> None:
    # when & then
    with pytest.raises(BotError):
        TrainKey.of(replace(TRAIN, dep_code=""))


def test_settings_default_to_manual_app_payment(tmp_path: Path) -> None:
    # when
    settings = Settings.from_env(
        {"BOT_TOKEN": "test", "BOT_DB_KEY": "secret", "BOT_ALLOWED_IDS": "1, 2", "BOT_DATA_DIR": str(tmp_path)}
    )

    # then
    assert not settings.payments
    assert settings.allowed == {1, 2}
    assert settings.interval == 30
    assert "secret" not in repr(settings)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BOT_TOKEN", ""),
        ("BOT_ALLOWED_IDS", ""),
        ("BOT_ALLOWED_IDS", "-1"),
        ("BOT_POLL_SECONDS", "nan"),
        ("BOT_POLL_SECONDS", "1"),
        ("BOT_POLL_SECONDS", "inf"),
        ("BOT_MAX_MINUTES", "0"),
        ("BOT_ENABLE_PAYMENTS", "yes"),
        ("BOT_ENABLE_PAYMENTS", "true"),
        ("BOT_MAX_PAYMENT_WON", "-1"),
    ],
)
def test_invalid_settings_fail_closed(key: str, value: str) -> None:
    # given
    env = {"BOT_TOKEN": "test", "BOT_DB_KEY": "secret", "BOT_ALLOWED_IDS": "1", key: value}

    # when & then
    with pytest.raises(BotError):
        Settings.from_env(env)


def test_missing_settings_are_rejected() -> None:
    # when & then
    with pytest.raises(BotError):
        Settings.from_env({})


def test_invalid_encryption_key_is_rejected(tmp_path: Path) -> None:
    # when & then
    with pytest.raises(BotError):
        Store(tmp_path, "not-a-fernet-key")


def test_credentials_roundtrip_without_plaintext(harness: Harness) -> None:
    # when
    credential = harness.store.credential(1)

    # then
    assert credential == harness.credential
    assert harness.credential.password.encode() not in (harness.store.directory / "users/1.enc").read_bytes()
    assert credential.password not in repr(credential)
    assert credential.membership not in repr(credential)
    assert len(credential.account) == 64


def test_credential_without_card_roundtrips(harness: Harness) -> None:
    # given
    harness.store.save_credential(2, replace(harness.credential, card=None))

    # when
    credential = harness.store.credential(2)

    # then
    assert credential.card is None
    assert harness.store.has_credential(2)


@pytest.mark.parametrize("owner", [0, -1, 999])
def test_invalid_or_unregistered_user_is_rejected(harness: Harness, owner: int) -> None:
    # when & then
    with pytest.raises(BotError):
        harness.store.credential(owner)


def test_wrong_key_does_not_overwrite_credentials(harness: Harness) -> None:
    # given
    other = Store(harness.store.directory, Fernet.generate_key().decode("ascii"))

    # when & then
    with pytest.raises(BotError):
        other.credential(1)
    assert harness.store.credential(1) == harness.credential


@pytest.mark.parametrize("membership", ["", "mail@example.com", "１２３４５６７８９０", "123"])
def test_only_membership_numbers_are_accepted(membership: str) -> None:
    # when & then
    with pytest.raises(BotError):
        Credential(membership, "test", "profile")


def test_instance_lock_blocks_another_process(harness: Harness) -> None:
    # given
    other = Store(harness.store.directory, harness.settings.key)
    harness.store.acquire()

    # when & then
    with pytest.raises(BotError):
        other.acquire()
    harness.store.close()
    other.acquire()
    other.close()


def test_same_instance_cannot_reacquire(harness: Harness) -> None:
    # given
    harness.store.acquire()

    # when & then
    with pytest.raises(BotError):
        harness.store.acquire()
    harness.store.close()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("WATCHING", "STOPPED"),
        ("RESERVING", "UNKNOWN"),
        ("PAYING", "UNKNOWN"),
        ("RESERVED", "RESERVED"),
        ("WAITLIST", "WAITLIST"),
        ("PAID", "PAID"),
    ],
)
def test_restart_never_replays_writes(harness: Harness, before: State, after: State) -> None:
    # given
    harness.store.save(JobRecord("job", 1, harness.credential.account, before, "test"))

    # when
    harness.store.recover()

    # then
    assert harness.store.get(1, "job").state == after


def test_active_account_is_unique(harness: Harness) -> None:
    # given
    harness.reserved()

    # when & then
    with pytest.raises(sqlite3.IntegrityError):
        harness.store.save(JobRecord("another", 2, harness.credential.account, "WATCHING", "test"))


def test_other_owner_cannot_read_job(harness: Harness) -> None:
    # given
    harness.reserved()

    # when & then
    with pytest.raises(BotError):
        harness.store.get(2, "job")


def test_restart_preserves_selected_train_details(harness: Harness) -> None:
    # given
    record = replace(harness.reserved(), details="서울 → 부산 KTX 001")
    harness.store.save(record)
    reopened = Store(harness.store.directory, harness.settings.key)

    # when
    reopened.initialize()
    reopened.recover()

    # then
    assert reopened.get(1, record.id) == record


def test_legacy_job_table_retains_existing_reservations(tmp_path: Path) -> None:
    # given
    with closing(sqlite3.connect(tmp_path / "jobs.sqlite3")) as conn, conn:
        conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, owner INTEGER, account TEXT, "
            "state TEXT, note TEXT, reservation_id TEXT)"
        )
        conn.execute("INSERT INTO jobs VALUES ('job', 1, 'account', 'RESERVED', 'test', 'reservation')")
    store = Store(tmp_path, Fernet.generate_key().decode("ascii"))

    # when
    store.initialize()

    # then
    assert store.get(1, "job") == JobRecord("job", 1, "account", "RESERVED", "test", "reservation")
