"""myTrail의 저장 카드 결제 서비스."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pykorail import Reservation
    from srtgo.rail.ktx.client import Korail


def pay_with_saved_card(rail: Korail, reservation: Reservation, card_info: dict[str, str] | None = None) -> bool:
    """사용자가 선택한 카드만 사용합니다. 관리자 카드로 대체하지 않습니다."""
    if not card_info:
        return False
    return rail.pay_with_card(reservation, card_info)
