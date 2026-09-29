"""myTrail 호출이 기존 pykorail의 요청으로 그대로 연결되는지 검증합니다."""

from __future__ import annotations

import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from pykorail import NoResultsError, ReserveOption, SoldOutError, TrainType
from srtgo.rail.ktx import client as adapter
from srtgo.service import auth, journal, payment
from tests.mytrail_support import CARD, PARAMS, RESERVATION, TRAIN, operation


@pytest.fixture
def backend(trail_env, monkeypatch):
    native = Mock(membership_number="0000000000", logined=True)
    native.reservations.create.return_value = RESERVATION
    native.reservations.find.return_value = RESERVATION
    native.reservations.pay.return_value = None
    native.reservations.cancel.return_value = None
    monkeypatch.setattr(adapter, "Client", Mock(return_value=native))
    rail = adapter.Korail(111)
    rail.login("test@example.test", "test-only")
    yield rail, native
    rail.close()


def test_construction_has_no_network(monkeypatch) -> None:
    # given
    factory = Mock()
    monkeypatch.setattr(adapter, "Client", factory)

    # when
    rail = adapter.Korail(111)
    rail.close()

    # then
    factory.assert_not_called()


def test_login_and_calls_share_one_worker(backend) -> None:
    # given
    rail, native = backend
    worker_ids = []
    native.trains.search.side_effect = lambda *args, **kwargs: worker_ids.append(threading.get_ident()) or [TRAIN]

    # when
    result = rail.search_train(**PARAMS)

    # then
    assert result == [TRAIN]
    assert worker_ids[0] != threading.get_ident()
    assert rail.is_login
    assert native.trains.search.call_args.kwargs["train_type"] == TrainType.KTX
    assert native.trains.search.call_args.kwargs["include_no_seats"]


def test_search_preserves_public_resource_payload(korail, trail_env, monkeypatch) -> None:
    # given
    native, session = korail
    monkeypatch.setattr(adapter, "Client", lambda: native)
    rail = adapter.Korail(111)

    # when
    trains = rail.search_train(**PARAMS)
    rail.close()

    # then
    assert trains
    payload = session.kwargs_for("search_schedule")["params"]
    assert payload["selGoTrain"] == TrainType.KTX
    assert payload["txtGoAbrdDt"] == "20991003"
    assert payload["Sid"] == ""
    assert "Key" not in payload


def test_no_results_returns_empty_list(backend) -> None:
    # given
    rail, native = backend
    native.trains.search.side_effect = NoResultsError()

    # when
    result = rail.search_train(**PARAMS)

    # then
    assert result == []


def test_reservation_maps_models_and_records_id(backend) -> None:
    # given
    rail, native = backend

    # when
    result = rail.reserve(TRAIN, PARAMS["passengers"], ReserveOption.GENERAL_ONLY)

    # then
    assert result is RESERVATION
    native.reservations.create.assert_called_once_with(TRAIN, PARAMS["passengers"], ReserveOption.GENERAL_ONLY)
    assert journal.current(111) == ("RESERVED", RESERVATION.rsv_id)


@pytest.mark.parametrize("method", ["pay_with_card", "cancel"])
def test_none_return_is_translated_to_mytrail_success(backend, method: str) -> None:
    # given
    rail, _native = backend
    rail.reserve(TRAIN, PARAMS["passengers"])
    args = {"pay_with_card": (RESERVATION, CARD), "cancel": (RESERVATION,)}[method]

    # when
    result = getattr(rail, method)(*args)

    # then
    assert result is True
    assert journal.current(111) is None


def test_card_fields_match_pykorail_contract(backend) -> None:
    # given
    rail, native = backend
    rail.reserve(TRAIN, PARAMS["passengers"])

    # when
    rail.pay_with_card(RESERVATION, {**CARD, "birthday": "0" * 10})

    # then
    card = native.reservations.pay.call_args.args[1]
    assert card.verify_number == "0" * 10
    assert card.expire == CARD["expire"]
    assert card.is_corporate


@pytest.mark.parametrize("method", ["reserve", "pay_with_card", "cancel"])
def test_uncertain_writes_block_replay(backend, method: str) -> None:
    # given
    rail, native = backend
    native_method = {"reserve": "create", "pay_with_card": "pay", "cancel": "cancel"}[method]
    journal.begin(111, rail._account, "RESERVING")
    journal.reserved(111, RESERVATION.rsv_id)
    setup = {"reserve": journal.finish, "pay_with_card": lambda _: None, "cancel": lambda _: None}
    setup[method](111)
    getattr(native.reservations, native_method).side_effect = TimeoutError("private")
    args = {"reserve": (TRAIN, PARAMS["passengers"]), "pay_with_card": (RESERVATION, CARD), "cancel": (RESERVATION,)}[
        method
    ]

    # when & then
    with pytest.raises(TimeoutError):
        getattr(rail, method)(*args)
    assert operation(111)[0] == "UNKNOWN"
    with pytest.raises(journal.UncertainOperationError):
        getattr(rail, method)(*args)
    getattr(native.reservations, native_method).assert_called_once()


def test_sold_out_allows_next_poll(backend) -> None:
    # given
    rail, native = backend
    native.reservations.create.side_effect = SoldOutError()

    # when & then
    with pytest.raises(SoldOutError):
        rail.reserve(TRAIN, PARAMS["passengers"])
    assert journal.current(111) is None


def test_missing_reservation_id_is_unknown(backend) -> None:
    # given
    rail, native = backend
    native.reservations.create.return_value = replace(RESERVATION, rsv_id="")

    # when & then
    with pytest.raises(journal.UncertainOperationError):
        rail.reserve(TRAIN, PARAMS["passengers"])
    assert operation(111)[0] == "UNKNOWN"


@pytest.mark.parametrize(
    "reservation",
    [
        None,
        replace(RESERVATION, rsv_id="other"),
        replace(RESERVATION, price=0),
        replace(RESERVATION, price=999),
        replace(RESERVATION, wct_no=None),
        replace(RESERVATION, buy_limit_date="20000101"),
        replace(RESERVATION, buy_limit_date="00000000"),
    ],
)
def test_invalid_payment_target_is_not_charged(backend, reservation) -> None:
    # given
    rail, native = backend
    rail.reserve(TRAIN, PARAMS["passengers"])
    native.reservations.find.return_value = reservation

    # when & then
    with pytest.raises(ValueError):
        rail.pay_with_card(RESERVATION, CARD)
    native.reservations.pay.assert_not_called()
    assert operation(111)[0] == "RESERVED"


def test_account_cannot_be_paid_from_different_user(backend) -> None:
    # given
    rail, native = backend
    journal.begin(222, rail._account, "RESERVING")

    # when & then
    with pytest.raises(journal.UncertainOperationError):
        rail.reserve(TRAIN, PARAMS["passengers"])
    native.reservations.create.assert_not_called()


def test_read_and_logout_methods_are_forwarded(backend) -> None:
    # given
    rail, native = backend
    native.reservations.all.return_value = [RESERVATION]
    native.tickets.all.return_value = []

    # when
    results = (rail.get_reservations(), rail.get_tickets(), rail.logout())

    # then
    assert results == ([RESERVATION], [], True)
    native.logout.assert_called_once()


def test_auth_factory_uses_user_supplied_credentials(monkeypatch) -> None:
    # given
    rail = Mock()
    monkeypatch.setattr(auth, "Korail", Mock(return_value=rail))

    # when
    result = auth.create_rail("KTX", {"id": "user", "pw": "test-only"}, 111)

    # then
    assert result is rail
    rail.login.assert_called_once_with("user", "test-only")


def test_auth_failure_closes_session(monkeypatch) -> None:
    # given
    rail = Mock()
    rail.login.side_effect = ValueError()
    monkeypatch.setattr(auth, "Korail", Mock(return_value=rail))

    # when & then
    with pytest.raises(ValueError):
        auth.create_rail("KTX", {"id": "u", "pw": "p"}, 111)
    rail.close.assert_called_once()


def test_srt_factory_is_not_available() -> None:
    # when & then
    with pytest.raises(ValueError, match="KTX"):
        auth.create_rail("SRT", {}, 111)


def test_payment_without_selected_card_does_not_use_admin_card() -> None:
    # given
    rail = Mock()

    # when
    result = payment.pay_with_saved_card(rail, RESERVATION)

    # then
    assert result is False
    rail.pay_with_card.assert_not_called()
