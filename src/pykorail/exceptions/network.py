"""전송 계층·대기열(NetFunnel) 관련 예외."""

from __future__ import annotations

from pykorail.exceptions.base import PykorailError


class NetFunnelError(PykorailError):
    """NetFunnel 대기열 티켓을 얻지 못했습니다.

    코레일 응답이 아니라 대기열 게이트(``nf.letskorail.com``) 유래이므로
    :class:`~pykorail.exceptions.base.KorailError` 가 아닌 형제 타입입니다.
    """

    def __init__(self, msg: str) -> None:
        self.msg = msg
        super().__init__(msg)

    def __str__(self) -> str:
        return self.msg


class TransportError(PykorailError):
    """HTTP 세션을 만들 수 없거나 응답이 JSON 이 아닙니다."""


class HttpStatusError(TransportError):
    """표준 코레일 응답이 아닌 HTTP 거절을 로그인 정보 오류와 구분합니다."""

    def __init__(self, status_code: int, code: str | None = None) -> None:
        self.status_code = status_code
        self.code = code
        # 서버 본문에는 식별정보가 섞일 수 있으므로 예외에 담지 않습니다.
        super().__init__(f"HTTP {status_code} (code={code})")
