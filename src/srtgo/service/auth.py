"""myTrail의 팩토리 경로를 유지하는 KTX 로그인."""

from __future__ import annotations

from srtgo.rail.ktx.client import Korail


def create_rail(rail_type: str, credentials: dict[str, str], owner: int) -> Korail:
    """텔레그램에서 등록한 계정으로 로그인합니다. 서버 keyring은 사용하지 않습니다."""
    if rail_type != "KTX":
        raise ValueError("KTX만 지원합니다.")
    rail = Korail(owner)
    try:
        rail.login(credentials["id"], credentials["pw"])
    except BaseException:
        rail.close()
        raise
    return rail
