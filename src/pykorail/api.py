"""요청/응답 계층 — 리소스들이 공유하는 저수준 클라이언트.

:class:`~pykorail.client.Korail` 과 각 리소스가 이 객체 하나를 나눠 씁니다.
HTTP 왕복·서명·에러 변환처럼 "어느 리소스에서나 똑같은 일"만 담고, 엔드포인트별
폼 필드는 리소스 쪽에 둡니다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pykorail.constants import API_ENDPOINTS, API_KEY, APP_VERSION, DEVICE
from pykorail.exceptions import TransportError, error_for_code

if TYPE_CHECKING:
    from pykorail.auth.signer import RequestSigner
    from pykorail.transport import HttpSession, Response

logger = logging.getLogger(__name__)
response_logger = logging.getLogger("pykorail.responses")


@dataclass
class Account:
    """로그인 세션 상태.

    여러 리소스가 읽고(``mbCrdNo``) 로그인만 쓰기 때문에, 클라이언트와 리소스가
    같은 인스턴스를 공유합니다.
    """

    logined: bool = False
    membership_number: str | None = None
    name: str | None = None
    email: str | None = None
    phone_number: str | None = None

    def clear(self) -> None:
        self.logined = False
        self.membership_number = None
        self.name = None
        self.email = None
        self.phone_number = None


class ApiClient:
    """서명·전송·응답 해석을 담당합니다."""

    def __init__(self, session: HttpSession, signer: RequestSigner, verbose: bool = False) -> None:
        self._session = session
        self._signer = signer
        self.verbose = verbose
        self.account = Account()

    # ------------------------------------------------------------------- 전송
    def sign(self, url: str) -> tuple[dict[str, str], str | None]:
        """``url`` 에 필요한 ``(헤더, Sid)``. 서명 대상이 아니면 ``({}, None)``."""
        return self._signer.sign(url)

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._parse(self._session.get(url, **_kwargs(params=params, headers=headers)), url)

    def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._parse(self._session.post(url, **_kwargs(data=data, params=params, headers=headers)), url)

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------- 해석
    def base_payload(self) -> dict[str, Any]:
        """거의 모든 요청에 실리는 앱 신원 필드."""
        return {"Device": DEVICE, "Version": APP_VERSION, "Key": API_KEY}

    @staticmethod
    def check(payload: dict[str, Any]) -> None:
        """``strResult=FAIL`` 이면 코드에 맞는 예외를 던집니다."""
        if payload.get("strResult") == "FAIL":
            raise error_for_code(payload.get("h_msg_cd"), payload.get("h_msg_txt"))

    def _parse(self, response: Response, url: str = "") -> dict[str, Any]:
        if self.verbose:
            logger.debug("%s", response.text)
        try:
            parsed = json.loads(response.text)
        except json.JSONDecodeError as exc:
            response_logger.warning("코레일 응답 endpoint=%s: JSON 해석 실패", _endpoint_name(url))
            raise TransportError(f"코레일 응답을 JSON 으로 읽지 못했습니다: {response.text[:200]!r}") from exc
        if not isinstance(parsed, dict):
            response_logger.warning("코레일 응답 endpoint=%s: 객체가 아닌 JSON", _endpoint_name(url))
            raise TransportError(f"코레일 응답이 객체가 아닙니다: {type(parsed).__name__}")
        # 자유 형식 메시지와 응답 본문에는 회원정보가 섞일 수 있어 허용한 메타데이터만 남깁니다.
        result = parsed.get("strResult")
        response_logger.info(
            "코레일 응답 endpoint=%s strResult=%s h_msg_cd=%s h_msg_txt=%s",
            _endpoint_name(url),
            result if result in ("SUCC", "FAIL") else "UNKNOWN",
            diagnostic_code(parsed.get("h_msg_cd")),
            "[본문 생략]" if parsed.get("h_msg_txt") else "[없음]",
        )
        return parsed


def _kwargs(**candidates: Any) -> dict[str, Any]:
    """``None`` 인 인자를 빼고 넘깁니다.

    ``post(url)`` 과 ``post(url, data=None)`` 은 라이브러리에 따라 다르게 처리될 수
    있어(빈 바디 vs 바디 없음), 앱이 보내는 모양을 유지하려면 아예 안 넘겨야 합니다.
    """
    return {key: value for key, value in candidates.items() if value is not None}


def diagnostic_code(value: object) -> str:
    """응답 코드만 허용하여 개인정보와 로그 개행 삽입을 차단합니다."""
    if value is None or value == "":
        return "[없음]"
    if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", value):
        return value
    return "[비표준 코드 생략]"


def _endpoint_name(url: str) -> str:
    return next((name for name, endpoint in API_ENDPOINTS.items() if endpoint == url), "unknown")
