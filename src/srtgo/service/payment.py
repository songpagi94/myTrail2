"""myTrail의 저장 카드 결제 서비스."""

from __future__ import annotations

from datetime import datetime, timedelta
from random import gammavariate
from typing import TYPE_CHECKING, Final

from srtgo.rail.ktx.client import KST

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from pykorail import Reservation
    from srtgo.rail.ktx.client import Korail


# 평균 180초, 표준편차 30초. 조회 간격과 별개로 예약마다 한 번만 추출합니다.
AUTO_PAYMENT_DELAY_SHAPE: Final = 36.0
AUTO_PAYMENT_DELAY_SCALE: Final = 5.0
PAYMENT_DEADLINE_MARGIN: Final = 30.0


def automatic_payment_delay(reservation: Reservation, *, now: datetime) -> float:
    """실제 결제기한의 30초 전을 넘지 않는 무작위 대기 시간을 정합니다."""
    deadline = datetime.strptime(reservation.buy_limit_date + reservation.buy_limit_time, "%Y%m%d%H%M%S").replace(
        tzinfo=KST
    )
    available = (deadline - now).total_seconds() - PAYMENT_DEADLINE_MARGIN
    if reservation.is_waiting or available <= 0:
        raise ValueError("자동결제 대기 시간을 확보할 수 없습니다. 코레일 앱에서 결제기한을 확인하세요.")
    return min(gammavariate(AUTO_PAYMENT_DELAY_SHAPE, AUTO_PAYMENT_DELAY_SCALE), available)


def wait_for_auto_payment(
    reservation: Reservation,
    cancel_event: threading.Event,
    on_scheduled: Callable[[datetime], None],
) -> bool:
    """예정 시각을 알리고 대기합니다. 취소·종료 신호가 오면 결제를 진행하지 않습니다."""
    if cancel_event.is_set():
        return False
    now = datetime.now(KST)
    delay = automatic_payment_delay(reservation, now=now)
    on_scheduled(now + timedelta(seconds=delay))
    return not cancel_event.wait(timeout=delay) and not cancel_event.is_set()


def pay_with_saved_card(rail: Korail, reservation: Reservation, card_info: dict[str, str] | None = None) -> bool:
    """사용자가 선택한 카드만 사용합니다. 관리자 카드로 대체하지 않습니다."""
    if not card_info:
        return False
    return rail.pay_with_card(reservation, card_info)
