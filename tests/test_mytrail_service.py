"""매진 재조회, 중단, 사용자 격리와 불확실한 쓰기 보호."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from pykorail import ReserveOption, SoldOutError
from srtgo.bot.session import AlreadyPollingError, Session
from srtgo.service import journal
from srtgo.service import reservation as service
from tests.mytrail_support import PARAMS, RESERVATION, TRAIN, operation


@pytest.mark.parametrize(
    ("option", "general", "special", "waiting", "expected"),
    [
        (ReserveOption.GENERAL_ONLY, "11", "00", -1, True),
        (ReserveOption.GENERAL_ONLY, "00", "11", -1, False),
        (ReserveOption.SPECIAL_ONLY, "11", "00", -1, False),
        (ReserveOption.SPECIAL_ONLY, "00", "11", -1, True),
        (ReserveOption.GENERAL_FIRST, "00", "11", -1, True),
        (ReserveOption.SPECIAL_FIRST, "11", "00", -1, True),
        (ReserveOption.GENERAL_FIRST, "00", "00", 9, True),
        (ReserveOption.GENERAL_FIRST, "00", "00", -1, False),
    ],
)
def test_mytrail_seat_preferences(option, general, special, waiting, expected) -> None:
    # given
    train = replace(TRAIN, general_seat=general, special_seat=special, wait_reserve_flag=waiting)

    # when
    result = service.is_seat_available(train, option)

    # then
    assert result is expected


def test_search_reordering_does_not_change_selected_train() -> None:
    # given
    other = replace(TRAIN, train_no="other")
    rail = Mock()
    rail.search_train.return_value = [other, TRAIN]
    rail.reserve.return_value = RESERVATION
    success, error = Mock(), Mock(return_value=False)

    # when
    service.poll_and_reserve(
        rail,
        PARAMS,
        [0],
        ReserveOption.GENERAL_ONLY,
        success,
        error,
        threading.Event(),
        PARAMS["passengers"],
        [TRAIN, other],
    )

    # then
    rail.reserve.assert_called_once_with(TRAIN, passengers=PARAMS["passengers"], option=ReserveOption.GENERAL_ONLY)
    success.assert_called_once_with(RESERVATION)
    error.assert_not_called()


def test_notification_failure_does_not_retry_reservation() -> None:
    # given
    rail = Mock()
    rail.search_train.return_value = [TRAIN]
    rail.reserve.return_value = RESERVATION
    success = Mock(side_effect=RuntimeError("notification"))

    # when & then
    with pytest.raises(RuntimeError):
        service.poll_and_reserve(
            rail,
            PARAMS,
            [0],
            ReserveOption.GENERAL_ONLY,
            success,
            Mock(),
            threading.Event(),
            PARAMS["passengers"],
            [TRAIN],
        )
    rail.reserve.assert_called_once()


def test_cancel_during_search_prevents_reserve() -> None:
    # given
    stop = threading.Event()
    rail = Mock()
    rail.search_train.side_effect = lambda **kwargs: stop.set() or [TRAIN]

    # when
    service.poll_and_reserve(
        rail, PARAMS, [0], ReserveOption.GENERAL_ONLY, Mock(), Mock(), stop, PARAMS["passengers"], [TRAIN]
    )

    # then
    rail.reserve.assert_not_called()


def test_sold_out_retries_without_sleeping_in_test(monkeypatch) -> None:
    # given
    rail = Mock()
    rail.search_train.return_value = [TRAIN]
    rail.reserve.side_effect = [SoldOutError(), RESERVATION]
    monkeypatch.setattr(service, "_sleep", Mock())

    # when
    service.poll_and_reserve(
        rail, PARAMS, [0], ReserveOption.GENERAL_ONLY, Mock(), Mock(), threading.Event(), PARAMS["passengers"], [TRAIN]
    )

    # then
    assert rail.reserve.call_count == 2


def test_uncertain_reserve_never_retries() -> None:
    # given
    rail = Mock()
    rail.search_train.return_value = [TRAIN]
    rail.reserve.side_effect = TimeoutError()
    error = Mock(return_value=True)

    # when
    service.poll_and_reserve(
        rail, PARAMS, [0], ReserveOption.GENERAL_ONLY, Mock(), error, threading.Event(), PARAMS["passengers"], [TRAIN]
    )

    # then
    rail.reserve.assert_called_once()
    assert isinstance(error.call_args.args[0], journal.UncertainOperationError)


@pytest.mark.parametrize("retry", [True, False])
def test_read_errors_follow_mytrail_callback(monkeypatch, retry: bool) -> None:
    # given
    stop = threading.Event()
    rail = Mock()
    rail.search_train.side_effect = TimeoutError()
    monkeypatch.setattr(service, "_sleep", lambda _: stop.set())
    error = Mock(return_value=retry)

    # when
    service.poll_and_reserve(
        rail, PARAMS, [0], ReserveOption.GENERAL_ONLY, Mock(), error, stop, PARAMS["passengers"], [TRAIN]
    )

    # then
    error.assert_called_once()
    rail.reserve.assert_not_called()


def test_no_matching_train_waits_until_cancel(monkeypatch) -> None:
    # given
    stop = threading.Event()
    rail = Mock()
    rail.search_train.return_value = []
    monkeypatch.setattr(service, "_sleep", lambda _: stop.set())

    # when
    service.poll_and_reserve(
        rail, PARAMS, [0], ReserveOption.GENERAL_ONLY, Mock(), Mock(), stop, PARAMS["passengers"], [TRAIN]
    )

    # then
    rail.reserve.assert_not_called()


def test_sleep_is_interruptible() -> None:
    # given
    stop = threading.Event()
    stop.set()

    # when
    service._sleep(stop)

    # then
    assert stop.is_set()


@pytest.mark.parametrize("state", ["RESERVING", "PAYING", "CANCELLING"])
def test_restart_keeps_uncertain_operation(trail_env, state: str) -> None:
    # given
    journal.begin(111, "account", "RESERVING")
    transition = {
        "RESERVING": lambda: None,
        "PAYING": lambda: (journal.reserved(111, "rsv"), journal.begin(111, "account", "PAYING", "rsv")),
        "CANCELLING": lambda: (journal.reserved(111, "rsv"), journal.begin(111, "account", "CANCELLING", "rsv")),
    }
    transition[state]()

    # when
    journal.recover()

    # then
    assert journal.current(111) == ("UNKNOWN", "" if state == "RESERVING" else "rsv")


def test_restart_keeps_unpaid_reservation(trail_env) -> None:
    # given
    journal.begin(111, "account", "RESERVING")
    journal.reserved(111, "rsv")

    # when
    journal.recover()

    # then
    assert journal.current(111) == ("RESERVED", "rsv")


@pytest.mark.parametrize(
    ("owner", "account", "reservation"), [(222, "account", "rsv"), (111, "other", "rsv"), (111, "account", "other")]
)
def test_journal_checks_owner_account_and_reservation(trail_env, owner, account, reservation) -> None:
    # given
    journal.begin(111, "account", "RESERVING")
    journal.reserved(111, "rsv")

    # when & then
    with pytest.raises(journal.UncertainOperationError):
        journal.begin(owner, account, "PAYING", reservation)


def test_cannot_pay_untracked_reservation(trail_env) -> None:
    # when & then
    with pytest.raises(journal.UncertainOperationError):
        journal.begin(111, "account", "PAYING", "rsv")


def test_two_users_can_reserve_different_accounts(trail_env) -> None:
    # given
    journal.begin(111, "account1", "RESERVING")

    # when
    journal.begin(222, "account2", "RESERVING")

    # then
    assert operation(111)[0] == "RESERVING"
    assert operation(222)[0] == "RESERVING"


def test_single_instance_lock(trail_env) -> None:
    # given
    lease = journal.acquire_instance()

    # when & then
    try:
        with pytest.raises(RuntimeError):
            journal.acquire_instance()
    finally:
        lease.close()
    journal.acquire_instance().close()


async def test_cancel_keeps_slot_until_worker_finishes() -> None:
    # given
    session = Session()
    done = asyncio.Event()
    stop = threading.Event()
    task = asyncio.create_task(done.wait())
    session.start_poll(111, task, stop)

    # when
    session.cancel_poll(111)

    # then
    assert stop.is_set()
    assert session.is_polling(111)
    with pytest.raises(AlreadyPollingError):
        session.start_poll(111, task, stop)
    done.set()
    await session.wait_poll(111)
    assert not session.is_polling(111)


async def test_shutdown_closes_pending_sessions() -> None:
    # given
    session = Session()
    rail = Mock()
    session.set_pending(111, {"rail": rail})

    # when
    await session.shutdown()

    # then
    rail.close.assert_called_once()
    assert session.get_pending(111) is None
