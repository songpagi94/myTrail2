"""작업별 조회 횟수와 경과 시간. 폴링 스레드와 텔레그램 이벤트 루프가 공유합니다."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class PollSnapshot:
    """한 시점의 조회 진행 상태."""

    elapsed_seconds: int
    attempts: int
    searching: bool


class PollProgress:
    """시계 보정에 영향받지 않는 작업별 카운터."""

    def __init__(self) -> None:
        self._started = monotonic()
        self._attempts = 0
        self._searching = False
        self._lock = threading.Lock()

    def query_started(self) -> None:
        """실제 재조회 진입 시 증가합니다. 실패한 조회도 한 번으로 셉니다."""
        with self._lock:
            self._attempts += 1
            self._searching = True

    def query_finished(self) -> None:
        """조회 결과 처리·다음 조회 대기를 진행 중 조회와 구분합니다."""
        with self._lock:
            self._searching = False

    def snapshot(self) -> PollSnapshot:
        """일관된 카운터와 경과 초를 반환합니다."""
        with self._lock:
            return PollSnapshot(max(0, int(monotonic() - self._started)), self._attempts, self._searching)
