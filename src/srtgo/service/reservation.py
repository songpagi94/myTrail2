"""myTrail의 폴링·콜백 흐름. 선택 당시 열차를 식별자로 추적합니다."""

from __future__ import annotations

import threading
from datetime import datetime
from random import gammavariate
from typing import TYPE_CHECKING, Final

from pykorail import ReserveOption, SoldOutError
from srtgo.rail.ktx.client import KST
from srtgo.service.journal import UncertainOperationError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pykorail import Passenger, Reservation, Train
    from pykorail.options import ReserveOptionCode
    from srtgo.rail.ktx.client import Korail


# myTrail과 같은 분포입니다. 평균은 shape * scale + min = 6.5초입니다.
RESERVE_INTERVAL_SHAPE: Final = 21.08
RESERVE_INTERVAL_SCALE: Final = 0.25
RESERVE_INTERVAL_MIN: Final = 1.23


def train_key(train: Train) -> tuple[str, str, str, str, str]:
    return train.dep_date, train.train_no, train.dep_code, train.arr_code, train.dep_time


def is_seat_available(train: Train, seat_option: ReserveOptionCode) -> bool:
    """myTrail의 KTX 좌석·예약대기 선택 규칙을 유지합니다."""
    if not train.has_seat():
        return train.has_waiting_list()
    if seat_option in (ReserveOption.GENERAL_FIRST, ReserveOption.SPECIAL_FIRST):
        return train.has_seat()
    if seat_option == ReserveOption.GENERAL_ONLY:
        return train.has_general_seat()
    return train.has_special_seat()


def _sleep(cancel_event: threading.Event) -> None:
    interval = gammavariate(RESERVE_INTERVAL_SHAPE, RESERVE_INTERVAL_SCALE) + RESERVE_INTERVAL_MIN
    cancel_event.wait(timeout=interval)


def poll_and_reserve(
    rail: Korail,
    search_params: dict,
    train_indices: list[int],
    seat_option: ReserveOptionCode,
    on_success: Callable[[Reservation], None],
    on_error: Callable[[Exception], bool],
    cancel_event: threading.Event,
    passengers: Sequence[Passenger],
    selected_trains: Sequence[Train],
) -> None:
    """열차 목록 순서 변경이나 알림 실패가 다른 열차/중복 예약으로 이어지지 않게 합니다."""
    targets = {train_key(selected_trains[index]) for index in train_indices}
    deadline = max(datetime.strptime(key[0] + key[4], "%Y%m%d%H%M%S").replace(tzinfo=KST) for key in targets)
    while not cancel_event.is_set() and datetime.now(KST) < deadline:
        params = dict(search_params)
        now = datetime.now(KST)
        if params["date"] == now.strftime("%Y%m%d"):
            params["time"] = max(params["time"], now.strftime("%H%M%S"))
        try:
            trains = rail.search_train(**params)
        except Exception as error:
            if not on_error(error):
                return
            _sleep(cancel_event)
            continue
        for train in trains:
            if cancel_event.is_set():
                return
            departure = datetime.strptime(train.dep_date + train.dep_time, "%Y%m%d%H%M%S").replace(tzinfo=KST)
            if (
                departure <= datetime.now(KST)
                or train_key(train) not in targets
                or not is_seat_available(train, seat_option)
            ):
                continue
            try:
                reservation = rail.reserve(train, passengers=passengers, option=seat_option)
            except SoldOutError:
                continue
            except Exception:
                on_error(UncertainOperationError("예약 결과가 불확실합니다. 코레일 앱과 /status에서 확인하세요."))
                return
            # 성공 콜백/알림 실패는 예약 실패가 아니므로 위의 재시도 경계 밖에서 실행합니다.
            on_success(reservation)
            return
        _sleep(cancel_event)
