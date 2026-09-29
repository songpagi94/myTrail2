"""개인정보를 포함할 수 있는 원격 예외는 원문을 표시하지 않습니다."""

from __future__ import annotations

from pykorail import LoginFailedError, PastDepartureError, StationNotFoundError
from srtgo.service.journal import UncertainOperationError


def safe_error(error: Exception) -> str:
    if isinstance(error, (UncertainOperationError, StationNotFoundError, PastDepartureError)):
        return str(error)[:500]
    if isinstance(error, LoginFailedError):
        return "로그인에 실패했습니다. /setup에서 코레일 계정을 확인하세요."
    return f"처리 오류 ({type(error).__name__}). 민감정보 보호를 위해 상세 응답은 표시하지 않습니다."
