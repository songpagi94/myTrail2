"""사용자별 진행 중 폴링 Task와 결제 대기 reservation 추적."""

from __future__ import annotations

import asyncio
import threading


class AlreadyPollingError(Exception):
    """동일 사용자에게 진행 중 폴링이 이미 있음."""


class Session:
    def __init__(self) -> None:
        self._polls: dict[int, tuple[asyncio.Task, threading.Event]] = {}
        self._pending: dict[int, dict] = {}

    def start_poll(
        self,
        telegram_id: int,
        task: asyncio.Task,
        cancel_event: threading.Event,
    ) -> None:
        if self.is_polling(telegram_id):
            raise AlreadyPollingError(f"tid={telegram_id} 이미 폴링 중")
        self._polls[telegram_id] = (task, cancel_event)
        task.add_done_callback(lambda completed: self._finished(telegram_id, completed))

    def _finished(self, telegram_id: int, task: asyncio.Task) -> None:
        entry = self._polls.get(telegram_id)
        if entry is not None and entry[0] is task:
            self._polls.pop(telegram_id, None)

    def is_polling(self, telegram_id: int) -> bool:
        entry = self._polls.get(telegram_id)
        return entry is not None and not entry[0].done()

    def cancel_poll(self, telegram_id: int) -> bool:
        entry = self._polls.get(telegram_id)
        if entry is None:
            return False
        _task, event = entry
        event.set()
        # task 자체는 to_thread 종료 후 자연 완료 — 강제 cancel은 불필요
        return True

    async def wait_poll(self, telegram_id: int) -> None:
        entry = self._polls.get(telegram_id)
        if entry is not None:
            await asyncio.shield(entry[0])

    async def shutdown(self) -> None:
        entries = list(self._polls.values())
        for _, event in entries:
            event.set()
        await asyncio.gather(*(task for task, _ in entries), return_exceptions=True)
        for pending in list(self._pending.values()):
            await asyncio.to_thread(pending["rail"].close)
        self._pending.clear()

    def set_pending(self, telegram_id: int, payload: dict) -> None:
        """payload 키: {reservation, rail}"""
        self._pending[telegram_id] = payload

    def get_pending(self, telegram_id: int) -> dict | None:
        return self._pending.get(telegram_id)

    def clear_pending(self, telegram_id: int) -> None:
        pending = self._pending.pop(telegram_id, None)
        if pending is not None:
            pending["rail"].close()
