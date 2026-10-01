"""개인정보를 포함할 수 있는 원격 예외는 원문을 표시하지 않습니다."""

from __future__ import annotations

import logging

from pykorail import LoginFailedError, PastDepartureError, StationNotFoundError
from pykorail.api import diagnostic_code
from pykorail.exceptions import KorailError
from srtgo.service.journal import UncertainOperationError


def safe_error(error: Exception, *, operation: str = "bot") -> str:
    """사용자 안내를 만들고 원문 대신 오류 종류와 코레일 코드를 기록합니다."""
    logging.getLogger(__name__).error(
        "봇 작업 실패 operation=%s error=%s h_msg_cd=%s reason=%s",
        operation,
        type(error).__name__,
        diagnostic_code(error.code) if isinstance(error, KorailError) else "[없음]",
        error.default_msg if isinstance(error, KorailError) else "[상세 생략]",
    )
    if isinstance(error, (UncertainOperationError, StationNotFoundError, PastDepartureError)):
        return str(error)[:500]
    if isinstance(error, LoginFailedError):
        return "로그인에 실패했습니다. /setup에서 코레일 계정을 확인하세요."
    return f"처리 오류 ({type(error).__name__}). 민감정보 보호를 위해 상세 응답은 표시하지 않습니다."
