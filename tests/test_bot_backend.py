"""봇 연결 계층은 기존 FakeSession으로만 검증합니다."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pykorail.constants import API_ENDPOINTS
from pykorail_bot.backend import Backend
from pykorail_bot.domain import BotError
from tests.bot_support import CARD, QUERY, Harness
from tests.payloads import CIPHER_PAYLOAD, LOGIN_OK, NO_RESULTS, SEARCH_PAYLOAD, STATION_PAYLOAD


async def test_backend_delegates_to_pykorail(make_korail, harness: Harness) -> None:
    # given
    _, session = make_korail(
        {"code": CIPHER_PAYLOAD, "login": LOGIN_OK, "stationdata": STATION_PAYLOAD, "search_schedule": SEARCH_PAYLOAD}
    )
    backend = Backend(harness.credential)

    # when
    trains = await backend.search(QUERY)
    await backend.close()

    # then
    assert len(trains) == 2
    params = session.kwargs_for("search_schedule")["params"]
    assert params["selGoTrain"] == "100"
    assert params["txtGoStart"] == "서울"
    assert params["txtGoAbrdDt"] == "20991003"
    assert params["txtPsgFlg_1"] == 1
    assert params["Sid"] == ""
    assert "Key" not in params
    assert session.closed


async def test_no_results_is_an_empty_list(make_korail, harness: Harness) -> None:
    # given
    _, session = make_korail(
        {"code": CIPHER_PAYLOAD, "login": LOGIN_OK, "stationdata": STATION_PAYLOAD, "search_schedule": NO_RESULTS}
    )
    backend = Backend(harness.credential)

    # when
    trains = await backend.search(QUERY)
    await backend.close()

    # then
    assert trains == []
    assert session.closed


async def test_backend_reserve_pay_and_read_use_existing_resources(korail, harness: Harness) -> None:
    # given
    client, session = korail
    backend = Backend(harness.credential)
    backend._client = client
    train = client.trains.search("서울", "부산", depart_after=QUERY.after)[0]

    # when
    reservation = await backend.reserve(train, QUERY)
    found = await backend.reservation(reservation.rsv_id)
    result = await backend.pay(reservation, CARD)
    tickets = await backend.tickets()
    await backend.close()

    # then
    assert result is None
    assert found is not None
    assert tickets
    assert session.kwargs_for("pay")["data"]["hidPnrNo"] == reservation.rsv_id
    assert session.closed


async def test_relogin_reuses_stored_profile(make_korail, harness: Harness) -> None:
    # given
    _, session = make_korail(
        {"code": CIPHER_PAYLOAD, "login": LOGIN_OK, "stationdata": STATION_PAYLOAD, "search_schedule": SEARCH_PAYLOAD}
    )
    backend = Backend(harness.credential)

    # when
    await backend.search(QUERY)
    await backend.relogin()
    await backend.close()

    # then
    assert session.urls().count(API_ENDPOINTS["login"]) == 2


async def test_missing_profile_fails_before_http(harness: Harness) -> None:
    # given
    backend = Backend(replace(harness.credential, profile="no-such-profile"))

    # when & then
    with pytest.raises(BotError):
        await backend.search(QUERY)
    await backend.close()


async def test_unused_backend_can_close_without_io(harness: Harness) -> None:
    # given
    backend = Backend(harness.credential)

    # when
    await backend.close()

    # then
    assert backend._executor is None
