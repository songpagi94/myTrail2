"""봇의 가짜 비동기 경계. 실제 HTTP 테스트는 기존 FakeSession으로 따로 검증합니다."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet

from pykorail import Card, Reservation, Train
from pykorail.device import DEVICE_PROFILES
from pykorail_bot.backend import Backend
from pykorail_bot.config import Settings
from pykorail_bot.domain import KST, JobRecord, Query
from pykorail_bot.jobs import Controller
from pykorail_bot.storage import Credential, Store
from tests.payloads import TRAIN_INFO

if TYPE_CHECKING:
    from pathlib import Path

    from pykorail_bot.jobs import Search

FUTURE = datetime(2099, 10, 3, 9, tzinfo=KST)
QUERY = Query("서울", "부산", FUTURE)
TRAIN = replace(Train.from_response(TRAIN_INFO), dep_date="20991003", run_date="20991003", arr_date="20991003")
CARD = Card("0" * 16, "00", "000000", "9912")
RESERVATION = replace(
    Reservation.from_response(TRAIN_INFO),
    train=TRAIN,
    rsv_id="test-reservation",
    price=1000,
    wct_no="test-window",
    buy_limit_date="20991003",
    buy_limit_time="085000",
)


@dataclass
class Harness:
    settings: Settings
    store: Store
    credential: Credential
    backend: Mock
    factory: Mock
    notify: AsyncMock
    controller: Controller

    async def search(self) -> Search:
        return await self.controller.search(1, QUERY)

    async def run_job(self) -> JobRecord:
        search = await self.search()
        selected = self.controller.prepare(1, search.id, [0])
        record = self.controller.start(1, selected.id, selected.confirmation)
        await self.controller.tasks[1][1]
        return self.store.get(1, record.id)

    def reserved(self) -> JobRecord:
        record = JobRecord("job", 1, self.credential.account, "RESERVED", "좌석 확보", RESERVATION.rsv_id)
        self.store.save(record)
        return record


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    settings = Settings(
        "test-token",
        Fernet.generate_key().decode("ascii"),
        frozenset({1, 2}),
        tmp_path,
        payments=True,
        max_payment=2000,
    )
    store = Store(tmp_path, settings.key)
    store.initialize()
    credential = Credential("0" * 10, "test-only", DEVICE_PROFILES[0].id, CARD)
    store.save_credential(1, credential)
    backend = Mock(spec=Backend)
    backend.search = AsyncMock(return_value=[TRAIN])
    backend.reserve = AsyncMock(return_value=RESERVATION)
    backend.reservation = AsyncMock(return_value=RESERVATION)
    backend.tickets = AsyncMock(return_value=[])
    backend.pay = AsyncMock(return_value=None)
    backend.close = AsyncMock()
    backend.relogin = AsyncMock()
    factory = Mock(return_value=cast("Backend", backend))
    notify = AsyncMock()
    controller = Controller(settings, store, notify, factory)
    return Harness(settings, store, credential, backend, factory, notify, controller)
