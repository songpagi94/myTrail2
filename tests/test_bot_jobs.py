"""재조회·중단·중복 예약 방지·수동 결제의 상태 전이."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from pykorail import LoginFailedError, NeedToLoginError, SoldOutError, StationNotFoundError, TransportError
from pykorail_bot.domain import BotError, now_kst
from pykorail_bot.jobs import safe_error
from tests.bot_support import QUERY, RESERVATION, TRAIN, Harness


async def test_search_is_read_only_and_sold_out_is_visible(harness: Harness) -> None:
    # given
    harness.backend.search.return_value = [replace(TRAIN, general_seat="00", special_seat="00")]

    # when
    result = await harness.search()

    # then
    assert len(result.trains) == 1
    harness.backend.reserve.assert_not_awaited()
    assert harness.store.records() == []
    await harness.controller.close()


async def test_reservation_success_stops_and_never_auto_pays(harness: Harness) -> None:
    # when
    result = await harness.run_job()

    # then
    assert result.state == "RESERVED"
    assert result.reservation_id == RESERVATION.rsv_id
    assert "서울" in result.details
    assert "부산" in result.details
    assert TRAIN.train_no in result.details
    harness.backend.reserve.assert_awaited_once_with(TRAIN, QUERY)
    harness.backend.pay.assert_not_awaited()
    harness.backend.close.assert_awaited_once()
    assert harness.controller.tasks == {}


async def test_reordered_search_reserves_same_train(harness: Harness) -> None:
    # given
    other = replace(TRAIN, train_no="other")
    harness.backend.search.side_effect = [[TRAIN, other], [other, TRAIN]]

    # when
    result = await harness.run_job()

    # then
    assert result.state == "RESERVED"
    harness.backend.reserve.assert_awaited_once_with(TRAIN, QUERY)


async def test_reservation_timeout_is_not_retried(harness: Harness) -> None:
    # given
    harness.backend.reserve.side_effect = TimeoutError("private response")

    # when
    result = await harness.run_job()

    # then
    assert result.state == "UNKNOWN"
    harness.backend.reserve.assert_awaited_once()
    assert "private response" not in result.note
    assert harness.store.active(harness.credential.account) == result


async def test_successful_reserve_with_missing_id_requires_review(harness: Harness) -> None:
    # given
    harness.backend.reserve.return_value = replace(RESERVATION, rsv_id="")

    # when
    result = await harness.run_job()

    # then
    assert result.state == "UNKNOWN"


async def test_unexpected_waitlist_is_not_announced_as_seat(harness: Harness) -> None:
    # given
    harness.backend.reserve.return_value = replace(RESERVATION, buy_limit_date="00000000")

    # when
    result = await harness.run_job()

    # then
    assert result.state == "WAITLIST"
    assert "좌석 확보" not in result.note
    harness.backend.pay.assert_not_awaited()


async def test_notification_failure_does_not_repeat_reservation(harness: Harness) -> None:
    # given
    harness.notify.side_effect = RuntimeError("notification failed")

    # when
    result = await harness.run_job()

    # then
    assert result.state == "RESERVED"
    harness.backend.reserve.assert_awaited_once()


async def test_sold_out_retries_search(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    harness.backend.reserve.side_effect = [SoldOutError(), RESERVATION]
    monkeypatch.setattr(harness.controller, "_wait", AsyncMock())

    # when
    result = await harness.run_job()

    # then
    assert result.state == "RESERVED"
    assert harness.backend.reserve.await_count == 2


async def test_read_failures_back_off_and_stop(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    harness.backend.search.side_effect = [[TRAIN], TimeoutError(), TransportError("private"), TimeoutError()]
    wait = AsyncMock()
    monkeypatch.setattr(harness.controller, "_wait", wait)

    # when
    result = await harness.run_job()

    # then
    assert result.state == "FAILED"
    assert wait.await_count == 2
    assert wait.await_args_list[1].args[1] > wait.await_args_list[0].args[1]
    harness.backend.reserve.assert_not_awaited()


async def test_session_expiry_relogs_in_once(harness: Harness) -> None:
    # given
    harness.backend.search.side_effect = [[TRAIN], NeedToLoginError(), [TRAIN]]

    # when
    result = await harness.run_job()

    # then
    assert result.state == "RESERVED"
    harness.backend.relogin.assert_awaited_once()


async def test_repeated_session_expiry_stops(harness: Harness) -> None:
    # given
    harness.backend.search.side_effect = [[TRAIN], NeedToLoginError(), NeedToLoginError()]

    # when
    result = await harness.run_job()

    # then
    assert result.state == "FAILED"
    harness.backend.relogin.assert_awaited_once()


async def test_permanent_search_error_stops(harness: Harness) -> None:
    # given
    harness.backend.search.side_effect = [[TRAIN], StationNotFoundError(["unknown"])]

    # when
    result = await harness.run_job()

    # then
    assert result.state == "FAILED"
    harness.backend.reserve.assert_not_awaited()


async def test_stop_before_worker_starts_does_not_reserve(harness: Harness) -> None:
    # given
    search = await harness.search()
    selected = harness.controller.prepare(1, search.id, [0])
    record = harness.controller.start(1, selected.id, selected.confirmation)
    task = harness.controller.tasks[1][1]

    # when
    harness.controller.stop(1)
    await task

    # then
    assert harness.store.get(1, record.id).state == "STOPPED"
    harness.backend.reserve.assert_not_awaited()


async def test_stop_during_search_does_not_reserve(harness: Harness) -> None:
    # given
    search = await harness.search()
    selected = harness.controller.prepare(1, search.id, [0])

    async def finish_search(query):
        harness.controller.stop(1)
        return [TRAIN]

    harness.backend.search.side_effect = finish_search

    # when
    record = harness.controller.start(1, selected.id, selected.confirmation)
    await harness.controller.tasks[1][1]

    # then
    assert harness.store.get(1, record.id).state == "STOPPED"
    harness.backend.reserve.assert_not_awaited()


async def test_stop_during_reserve_keeps_successful_reservation(harness: Harness) -> None:
    # given
    async def finish_reserve(train, query):
        harness.controller.stop(1)
        return RESERVATION

    harness.backend.reserve.side_effect = finish_reserve

    # when
    result = await harness.run_job()

    # then
    assert result.state == "RESERVED"
    assert result.reservation_id


async def test_no_matching_train_waits_until_stop(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    harness.backend.search.side_effect = [[TRAIN], [replace(TRAIN, train_no="other")]]

    async def stop_wait(event, seconds):
        event.set()

    monkeypatch.setattr(harness.controller, "_wait", stop_wait)

    # when
    result = await harness.run_job()

    # then
    assert result.state == "STOPPED"
    harness.backend.reserve.assert_not_awaited()


async def test_double_start_is_rejected(harness: Harness) -> None:
    # given
    search = await harness.search()
    selected = harness.controller.prepare(1, search.id, [0])
    harness.controller.start(1, selected.id, selected.confirmation)

    # when & then
    with pytest.raises(BotError):
        harness.controller.start(1, selected.id, selected.confirmation)
    harness.controller.stop(1)
    await harness.controller.tasks[1][1]


async def test_same_account_under_second_user_is_blocked(harness: Harness) -> None:
    # given
    harness.store.save_credential(2, harness.credential)
    await harness.search()

    # when & then
    with pytest.raises(BotError):
        await harness.controller.search(2, QUERY)
    await harness.controller.close()


async def test_unresolved_record_blocks_new_search(harness: Harness) -> None:
    # given
    harness.reserved()

    # when & then
    with pytest.raises(BotError):
        await harness.search()
    harness.backend.search.assert_not_awaited()


async def test_search_failure_closes_session(harness: Harness) -> None:
    # given
    harness.backend.search.side_effect = LoginFailedError()

    # when & then
    with pytest.raises(LoginFailedError):
        await harness.search()
    harness.backend.close.assert_awaited_once()


async def test_empty_search_closes_session(harness: Harness) -> None:
    # given
    harness.backend.search.return_value = []

    # when & then
    with pytest.raises(BotError):
        await harness.search()
    harness.backend.close.assert_awaited_once()


async def test_new_search_replaces_old_buttons(harness: Harness) -> None:
    # given
    first = await harness.search()
    second = await harness.search()

    # when & then
    with pytest.raises(BotError):
        harness.controller.result(1, first.id)
    assert harness.controller.result(1, second.id) == second
    await harness.controller.close()


@pytest.mark.parametrize("indices", [[], [-1], [100]])
async def test_invalid_selection_is_rejected(harness: Harness, indices: list[int]) -> None:
    # given
    search = await harness.search()

    # when & then
    with pytest.raises(BotError):
        harness.controller.prepare(1, search.id, indices)
    await harness.controller.close()


async def test_expired_search_is_rejected(harness: Harness) -> None:
    # given
    search = await harness.search()
    harness.controller.searches[1] = replace(search, expires=now_kst() - timedelta(seconds=1))

    # when & then
    with pytest.raises(BotError):
        harness.controller.result(1, search.id)
    await harness.controller.close()


async def test_old_start_confirmation_is_rejected(harness: Harness) -> None:
    # given
    search = await harness.search()
    old = harness.controller.prepare(1, search.id, [0])
    harness.controller.prepare(1, search.id, [0])

    # when & then
    with pytest.raises(BotError):
        harness.controller.start(1, old.id, old.confirmation)
    await harness.controller.close()


async def test_payment_requires_enabling(harness: Harness) -> None:
    # given
    harness.reserved()
    harness.controller.settings = replace(harness.settings, payments=False)

    # when & then
    with pytest.raises(BotError):
        await harness.controller.quote(1, "job")
    harness.backend.pay.assert_not_awaited()


async def test_payment_none_return_is_success_when_ticket_confirmed(harness: Harness) -> None:
    # given
    harness.reserved()
    quote = await harness.controller.quote(1, "job")
    from unittest.mock import Mock

    harness.backend.tickets.return_value = [Mock(pnr_no=RESERVATION.rsv_id)]

    # when
    await harness.controller.pay(1, "job", quote.nonce)

    # then
    assert harness.store.get(1, "job").state == "PAID"
    harness.backend.pay.assert_awaited_once_with(RESERVATION, harness.credential.card)


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError()])
async def test_payment_exception_blocks_duplicate_charge(harness: Harness, error: Exception) -> None:
    # given
    harness.reserved()
    quote = await harness.controller.quote(1, "job")
    harness.backend.pay.side_effect = error

    # when
    await harness.controller.pay(1, "job", quote.nonce)

    # then
    assert harness.store.get(1, "job").state == "UNKNOWN"
    with pytest.raises(BotError):
        await harness.controller.pay(1, "job", quote.nonce)
    harness.backend.pay.assert_awaited_once()


async def test_unconfirmed_issuance_is_unknown_not_payment_failure(harness: Harness) -> None:
    # given
    harness.reserved()
    quote = await harness.controller.quote(1, "job")

    # when
    await harness.controller.pay(1, "job", quote.nonce)

    # then
    assert harness.store.get(1, "job").state == "UNKNOWN"


async def test_concurrent_payment_click_sends_only_one_request(harness: Harness) -> None:
    # given
    harness.reserved()
    quote = await harness.controller.quote(1, "job")
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_payment(*args: object) -> None:
        entered.set()
        await release.wait()

    harness.backend.pay.side_effect = delayed_payment
    first = asyncio.create_task(harness.controller.pay(1, "job", quote.nonce))
    await entered.wait()

    # when & then
    try:
        with pytest.raises(BotError):
            await harness.controller.pay(1, "job", quote.nonce)
    finally:
        release.set()
        await first
    harness.backend.pay.assert_awaited_once()


async def test_price_change_requires_new_confirmation(harness: Harness) -> None:
    # given
    harness.reserved()
    quote = await harness.controller.quote(1, "job")
    harness.backend.reservation.return_value = replace(RESERVATION, price=1500)

    # when & then
    with pytest.raises(BotError):
        await harness.controller.pay(1, "job", quote.nonce)
    harness.backend.pay.assert_not_awaited()
    assert harness.store.get(1, "job").state == "RESERVED"


async def test_expired_quote_cannot_pay(harness: Harness) -> None:
    # given
    harness.reserved()
    quote = await harness.controller.quote(1, "job")
    harness.controller.quotes[1] = replace(quote, expires=now_kst() - timedelta(seconds=1))

    # when & then
    with pytest.raises(BotError):
        await harness.controller.pay(1, "job", quote.nonce)
    harness.backend.pay.assert_not_awaited()


async def test_card_change_invalidates_confirmation(harness: Harness) -> None:
    # given
    harness.reserved()
    quote = await harness.controller.quote(1, "job")
    harness.store.save_credential(1, replace(harness.credential, card=None))

    # when & then
    with pytest.raises(BotError):
        await harness.controller.pay(1, "job", quote.nonce)
    harness.backend.pay.assert_not_awaited()


@pytest.mark.parametrize(
    "reservation",
    [
        None,
        replace(RESERVATION, rsv_id="different-reservation"),
        replace(RESERVATION, wct_no=None),
        replace(RESERVATION, price=0),
        replace(RESERVATION, price=9999),
        replace(RESERVATION, buy_limit_date="00000000"),
        replace(RESERVATION, buy_limit_date="20000101"),
        replace(RESERVATION, buy_limit_time="bad"),
    ],
)
async def test_unsafe_payment_reservations_are_rejected(harness: Harness, reservation) -> None:
    # given
    harness.reserved()
    harness.backend.reservation.return_value = reservation

    # when & then
    with pytest.raises(BotError):
        await harness.controller.quote(1, "job")
    harness.backend.pay.assert_not_awaited()


async def test_check_recovers_confirmed_ticket(harness: Harness) -> None:
    # given
    from unittest.mock import Mock

    record = replace(harness.reserved(), state="UNKNOWN")
    harness.store.save(record)
    harness.backend.tickets.return_value = [Mock(pnr_no=RESERVATION.rsv_id)]

    # when
    result = await harness.controller.check(1, "job")

    # then
    assert result.state == "PAID"
    harness.backend.pay.assert_not_awaited()


async def test_check_does_not_enable_retry_of_uncertain_payment(harness: Harness) -> None:
    # given
    harness.store.save(replace(harness.reserved(), state="UNKNOWN"))

    # when
    result = await harness.controller.check(1, "job")

    # then
    assert result.state == "UNKNOWN"
    with pytest.raises(BotError):
        await harness.controller.quote(1, "job")


async def test_missing_reservation_requires_manual_review(harness: Harness) -> None:
    # given
    harness.reserved()
    harness.backend.reservation.return_value = None

    # when
    result = await harness.controller.check(1, "job")

    # then
    assert result.state == "UNKNOWN"


def test_acknowledgment_only_releases_local_hold(harness: Harness) -> None:
    # given
    harness.reserved()

    # when
    result = harness.controller.acknowledge(1, "job")

    # then
    assert result.state == "CLOSED"
    assert harness.store.active(harness.credential.account) is None
    harness.backend.pay.assert_not_awaited()


@pytest.mark.parametrize("error", [RuntimeError("secret"), TransportError("secret"), LoginFailedError("secret")])
def test_errors_do_not_disclose_server_response(error: Exception) -> None:
    # when
    result = safe_error(error)

    # then
    assert "secret" not in result


async def test_wait_handles_timeout_and_stop(harness: Harness) -> None:
    # given
    event = asyncio.Event()

    # when
    await harness.controller._wait(event, 0.001)
    event.set()
    await harness.controller._wait(event, 30)

    # then
    assert event.is_set()


def test_unallowed_user_cannot_access_controller(harness: Harness) -> None:
    # when & then
    with pytest.raises(BotError):
        harness.controller.status(99)


def test_stop_without_job_does_not_cancel_reservation(harness: Harness) -> None:
    # given
    harness.reserved()

    # when
    message = harness.controller.stop(1)

    # then
    assert "취소하지 않습니다" in message
    assert harness.store.get(1, "job").state == "RESERVED"
