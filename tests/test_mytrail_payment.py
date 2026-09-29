"""자동결제 전 감마분포 대기와 결제기한·중단 경계."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock, call

import pytest

from srtgo.rail.ktx.client import KST
from srtgo.service import payment
from tests.mytrail_support import RESERVATION

NOW = datetime(2099, 10, 3, 8, tzinfo=KST)


def test_auto_payment_draws_gamma_with_three_minute_mean(monkeypatch) -> None:
    # given
    gamma = Mock(side_effect=[120.0, 240.0])
    monkeypatch.setattr(payment, "gammavariate", gamma)

    # when
    delays = (
        payment.automatic_payment_delay(RESERVATION, now=NOW),
        payment.automatic_payment_delay(RESERVATION, now=NOW),
    )

    # then
    assert delays == (120.0, 240.0)
    assert gamma.call_args_list == [call(36.0, 5.0), call(36.0, 5.0)]
    assert payment.AUTO_PAYMENT_DELAY_SHAPE * payment.AUTO_PAYMENT_DELAY_SCALE == 180


@pytest.mark.parametrize(("remaining", "expected"), [(600, 180), (120, 90), (31, 1)])
def test_auto_payment_keeps_thirty_seconds_before_deadline(monkeypatch, remaining, expected) -> None:
    # given
    monkeypatch.setattr(payment, "gammavariate", Mock(return_value=180.0))
    deadline = NOW + timedelta(seconds=remaining)
    reservation = replace(RESERVATION, buy_limit_time=deadline.strftime("%H%M%S"))

    # when
    delay = payment.automatic_payment_delay(reservation, now=NOW)

    # then
    assert delay == expected


@pytest.mark.parametrize("remaining", [-1, 0, 29, 30])
def test_no_safe_payment_window_is_rejected(remaining) -> None:
    # given
    deadline = NOW + timedelta(seconds=remaining)
    reservation = replace(RESERVATION, buy_limit_time=deadline.strftime("%H%M%S"))

    # when & then
    with pytest.raises(ValueError):
        payment.automatic_payment_delay(reservation, now=NOW)


@pytest.mark.parametrize(
    "reservation",
    [replace(RESERVATION, buy_limit_time="bad"), replace(RESERVATION, buy_limit_time="235959")],
)
def test_invalid_deadline_or_waitlist_cannot_schedule_payment(reservation) -> None:
    # when & then
    with pytest.raises(ValueError):
        payment.automatic_payment_delay(reservation, now=NOW)


@pytest.mark.parametrize("cancelled", [True, False])
def test_payment_wait_announces_schedule_and_obeys_cancellation(monkeypatch, cancelled) -> None:
    # given
    monkeypatch.setattr(payment, "gammavariate", Mock(return_value=180.0))
    stop = Mock(spec=threading.Event)
    stop.is_set.return_value = False
    stop.wait.return_value = cancelled
    scheduled = Mock()

    # when
    ready = payment.wait_for_auto_payment(RESERVATION, stop, scheduled)

    # then
    assert ready is not cancelled
    stop.wait.assert_called_once_with(timeout=180.0)
    scheduled.assert_called_once()
    assert scheduled.call_args.args[0].tzinfo == KST


def test_already_cancelled_payment_never_schedules() -> None:
    # given
    stop = threading.Event()
    stop.set()
    scheduled = Mock()

    # when
    ready = payment.wait_for_auto_payment(RESERVATION, stop, scheduled)

    # then
    assert not ready
    scheduled.assert_not_called()


def test_cancellation_at_end_of_wait_prevents_payment(monkeypatch) -> None:
    # given
    monkeypatch.setattr(payment, "gammavariate", Mock(return_value=180.0))
    stop = Mock(spec=threading.Event)
    stop.is_set.side_effect = [False, True]
    stop.wait.return_value = False

    # when
    ready = payment.wait_for_auto_payment(RESERVATION, stop, Mock())

    # then
    assert not ready
