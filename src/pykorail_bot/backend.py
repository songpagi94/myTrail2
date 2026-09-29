"""봇의 동기 통신 경계. HTTP·서명·폼 필드는 기존 pykorail만 사용합니다."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, TypeVar

from pykorail import Korail, NoResultsError, TrainType, profile_by_id
from pykorail_bot.domain import BotError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pykorail import Card, Reservation, Ticket, Train
    from pykorail_bot.domain import Query
    from pykorail_bot.storage import Credential

T = TypeVar("T")


class Backend:
    """한 세션의 생성·사용·종료를 같은 전용 스레드에서 직렬 실행합니다."""

    def __init__(self, credential: Credential) -> None:
        self.credential = credential
        self._client: Korail | None = None
        self._executor: ThreadPoolExecutor | None = None

    async def call(self, operation: Callable[[], T]) -> T:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="korail-bot")
        # Future 취소가 이미 전송한 예약을 취소하지는 않습니다. 상위 계층은 Event로 중단합니다.
        return await asyncio.shield(asyncio.get_running_loop().run_in_executor(self._executor, operation))

    def _connected(self) -> Korail:
        if self._client is None:
            profile = None if self.credential.profile == "default" else profile_by_id(self.credential.profile)
            if profile is None and self.credential.profile != "default":
                raise BotError("저장된 기기 프로파일이 없습니다. 서버 설정을 다시 확인하세요.")
            self._client = Korail.logged_in(
                self.credential.membership, self.credential.password, device_profile=profile
            )
        return self._client

    async def search(self, query: Query) -> list[Train]:
        def operation() -> list[Train]:
            try:
                return self._connected().trains.search(
                    query.departure,
                    query.arrival,
                    depart_after=query.after,
                    train_type=TrainType.KTX,
                    passengers=query.passengers(),
                    include_no_seats=True,
                )
            except NoResultsError:
                return []

        return await self.call(operation)

    async def reserve(self, train: Train, query: Query) -> Reservation:
        return await self.call(lambda: self._connected().reservations.create(train, query.passengers(), query.seat))

    async def reservation(self, reservation_id: str) -> Reservation | None:
        return await self.call(lambda: self._connected().reservations.find(reservation_id))

    async def tickets(self) -> list[Ticket]:
        return await self.call(lambda: self._connected().tickets.all())

    async def pay(self, reservation: Reservation, card: Card) -> None:
        await self.call(lambda: self._connected().reservations.pay(reservation, card))

    async def relogin(self) -> None:
        def operation() -> None:
            if self._client is not None:
                self._client.close()
                self._client = None
            self._connected()

        await self.call(operation)

    async def close(self) -> None:
        def operation() -> None:
            if self._client is not None:
                self._client.close()
                self._client = None

        if self._executor is not None:
            try:
                await self.call(operation)
            finally:
                self._executor.shutdown(wait=False)
                self._executor = None
