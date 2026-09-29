"""예약 작업 수명과 결제 확인. 전송 결과가 불확실하면 절대 자동 재시도하지 않습니다."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING

from pykorail import (
    LoginFailedError,
    NeedToLoginError,
    PastDepartureError,
    PykorailError,
    SoldOutError,
    StationNotFoundError,
    TransportError,
)
from pykorail_bot.backend import Backend
from pykorail_bot.domain import KST, TERMINAL, BotError, JobRecord, TrainKey, now_kst, seat_available

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
    from datetime import datetime

    from pykorail import Reservation, Train
    from pykorail_bot.config import Settings
    from pykorail_bot.domain import Query, State
    from pykorail_bot.storage import Credential, Store

logger = logging.getLogger(__name__)


def safe_error(error: Exception) -> str:
    """HTTP 응답·자격증명·토큰이 예외 원문을 통해 노출되지 않게 합니다."""
    if isinstance(error, (BotError, StationNotFoundError, PastDepartureError)):
        return str(error)[:500]
    if isinstance(error, LoginFailedError):
        return "코레일 로그인에 실패했습니다. 서버에 등록한 회원정보를 확인하세요."
    return f"처리하지 못했습니다 ({type(error).__name__}). 민감정보 보호를 위해 원문은 표시하지 않습니다."


@dataclass(frozen=True)
class Search:
    """버튼이 참조하는 고정 검색 결과. 오래된 버튼을 새 요청에 적용하지 않습니다."""

    id: str
    owner: int
    query: Query
    trains: tuple[Train, ...]
    expires: datetime
    account: str
    backend: Backend
    targets: tuple[TrainKey, ...] = ()
    confirmation: str = ""


@dataclass(frozen=True)
class Quote:
    """금액·예약·사용자·카드에 묶인 일회성 수동 결제 확인."""

    nonce: str
    job: str
    owner: int
    price: int
    expires: datetime
    credential: Credential


class Controller:
    """동일 사용자·계정 작업을 직렬화하고 원격 쓰기 전에 상태를 기록합니다."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        notify: Callable[[JobRecord], Awaitable[None]],
        factory: Callable[[Credential], Backend] = Backend,
    ) -> None:
        self.settings, self.store, self.notify, self.factory = settings, store, notify, factory
        self.searches: dict[int, Search] = {}
        self.tasks: dict[int, tuple[str, asyncio.Task[None], asyncio.Event]] = {}
        self.quotes: dict[int, Quote] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._accounts: dict[str, int] = {}
        self._closing = False

    def _allowed(self, owner: int) -> None:
        if owner not in self.settings.allowed or self._closing:
            raise BotError("허용되지 않은 사용자이거나 봇이 종료 중입니다.")

    @asynccontextmanager
    async def _guard(self, owner: int) -> AsyncIterator[None]:
        self._allowed(owner)
        lock = self._locks.setdefault(owner, asyncio.Lock())
        if lock.locked():
            raise BotError("이전 요청을 처리 중입니다. /status 또는 /stop을 사용하세요.")
        async with lock:
            yield

    def _claim(self, owner: int, credential: Credential) -> None:
        holder = self._accounts.get(credential.account)
        if holder is not None and holder != owner:
            raise BotError("같은 코레일 계정을 사용하는 다른 작업이 진행 중입니다.")
        self._accounts[credential.account] = owner

    async def _release(self, owner: int, account: str, backend: Backend) -> None:
        try:
            await backend.close()
        finally:
            if self._accounts.get(account) == owner:
                self._accounts.pop(account, None)

    async def search(self, owner: int, query: Query) -> Search:
        async with self._guard(owner):
            credential = self.store.credential(owner)
            if owner in self.tasks or self.store.active(credential.account) is not None:
                raise BotError("진행 중이거나 확인이 필요한 예약이 있습니다. /status로 먼저 확인하세요.")
            old = self.searches.pop(owner, None)
            if old is not None:
                await self._release(owner, old.account, old.backend)
            self._claim(owner, credential)
            backend = self.factory(credential)
            try:
                trains = tuple(await backend.search(query))
                if not trains:
                    raise BotError("조건에 맞는 열차가 없습니다. 매진 열차까지 포함한 조회 결과입니다.")
                result = Search(
                    secrets.token_hex(6),
                    owner,
                    query,
                    trains,
                    now_kst() + timedelta(minutes=10),
                    credential.account,
                    backend,
                )
                self.searches[owner] = result
                return result
            except BaseException:
                await self._release(owner, credential.account, backend)
                raise

    def result(self, owner: int, token: str) -> Search:
        self._allowed(owner)
        result = self.searches.get(owner)
        if result is None or result.id != token or result.expires <= now_kst():
            raise BotError("검색 버튼이 만료됐습니다. 다시 조회하세요.")
        return result

    def prepare(self, owner: int, token: str, indices: Sequence[int]) -> Search:
        result = self.result(owner, token)
        if owner in self.tasks or not indices or any(i < 0 or i >= len(result.trains) for i in indices):
            raise BotError("선택한 열차가 없거나 이미 예약 시도 중입니다.")
        targets = tuple(dict.fromkeys(TrainKey.of(result.trains[i]) for i in indices))
        result = replace(result, targets=targets, confirmation=secrets.token_hex(6))
        self.searches[owner] = result
        return result

    def start(self, owner: int, token: str, confirmation: str) -> JobRecord:
        result = self.result(owner, token)
        if not result.targets or confirmation != result.confirmation:
            raise BotError("예약 시작 확인이 만료됐습니다. 열차를 다시 선택하세요.")
        if owner in self.tasks or self._locks.get(owner, asyncio.Lock()).locked() or self.store.active(result.account):
            raise BotError("이미 진행 중이거나 확인이 필요한 작업이 있습니다.")
        if max(key.departure_time() for key in result.targets) <= now_kst():
            raise BotError("선택한 열차의 출발 시각이 지났습니다.")
        selected_trains = (str(train) for train in result.trains if TrainKey.of(train) in result.targets)
        details = result.query.summary() + "\n" + "\n".join(selected_trains)
        record = JobRecord(
            result.id, owner, result.account, "WATCHING", "선택한 열차의 실좌석을 기다리는 중입니다.", details=details
        )
        self.store.save(record)
        stop = asyncio.Event()
        task = asyncio.create_task(self._watch(result, record, stop))
        self.tasks[owner] = (record.id, task, stop)
        return record

    def stop(self, owner: int) -> str:
        self._allowed(owner)
        running = self.tasks.get(owner)
        if running is None:
            return "진행 중인 반복 조회가 없습니다. 이미 생성된 예약은 취소하지 않습니다."
        running[2].set()
        return "중단을 요청했습니다. 이미 전송한 요청은 결과를 확인한 뒤 종료합니다. 예약·승차권은 취소하지 않습니다."

    def status(self, owner: int) -> list[JobRecord]:
        self._allowed(owner)
        return [r for r in self.store.records() if r.owner == owner][-5:]

    def _state(self, record: JobRecord, state: State, note: str, reservation_id: str | None = None) -> JobRecord:
        updated = replace(
            record,
            state=state,
            note=note,
            reservation_id=record.reservation_id if reservation_id is None else reservation_id,
        )
        self.store.save(updated)
        return updated

    async def _notice(self, record: JobRecord) -> None:
        try:
            await self.notify(record)
        except Exception as error:
            # 알림 실패는 예약 실패가 아닙니다. 절대로 예약 루프에 되돌리지 않습니다.
            logger.warning("작업 알림 실패: %s (%s)", record.id, type(error).__name__)

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0, seconds))

    async def _watch(self, search: Search, record: JobRecord, stop: asyncio.Event) -> None:
        deadline = min(
            now_kst() + timedelta(minutes=self.settings.max_minutes), max(k.departure_time() for k in search.targets)
        )
        errors, relogins = 0, 0
        try:
            async with self._guard(search.owner):
                while not stop.is_set() and now_kst() < deadline:
                    try:
                        query = replace(search.query, after=max(search.query.after, now_kst()))
                        trains = await search.backend.search(query)
                    except NeedToLoginError:
                        if relogins >= 1:
                            raise BotError("재로그인 후에도 인증이 만료되어 중단했습니다.") from None
                        relogins += 1
                        if not stop.is_set():
                            await search.backend.relogin()
                        continue
                    except Exception as error:
                        if isinstance(error, PykorailError) and not isinstance(error, TransportError):
                            raise
                        errors += 1
                        if errors >= 3:
                            raise BotError("조회 통신 오류가 반복되어 중단했습니다.") from None
                        await self._wait(
                            stop, min(self.settings.interval * 2**errors, (deadline - now_kst()).total_seconds())
                        )
                        continue
                    errors = 0
                    candidates = [
                        t
                        for t in trains
                        if TrainKey.of(t) in search.targets
                        and seat_available(t, search.query.seat)
                        and TrainKey.of(t).departure_time() > now_kst()
                    ]
                    if stop.is_set() or now_kst() >= deadline:
                        break
                    if candidates:
                        record = self._state(
                            record, "RESERVING", "예약 요청 중입니다. 결과 확인 전에는 다시 요청하지 않습니다."
                        )
                        try:
                            reservation = await search.backend.reserve(candidates[0], search.query)
                        except SoldOutError:
                            record = self._state(record, "WATCHING", "예약 직전 매진되어 다시 조회합니다.")
                        except Exception:
                            record = self._state(
                                record,
                                "UNKNOWN",
                                "예약 결과가 불확실합니다. 앱에서 확인하세요. 자동 재시도하지 않습니다.",
                            )
                            return
                        else:
                            if not reservation.rsv_id:
                                record = self._state(
                                    record, "UNKNOWN", "예약 식별번호를 받지 못했습니다. 앱에서 확인하세요."
                                )
                            else:
                                state: State = "WAITLIST" if reservation.is_waiting else "RESERVED"
                                note = (
                                    "코레일 예약대기입니다. 결제하지 않습니다."
                                    if reservation.is_waiting
                                    else f"좌석 확보! {reservation}\n"
                                    "코레일 앱에서 결제하거나 활성화된 수동 결제를 사용하세요."
                                )
                                record = self._state(record, state, note, reservation.rsv_id)
                            return
                    await self._wait(stop, min(self.settings.interval, (deadline - now_kst()).total_seconds()))
                record = self._state(
                    record, "STOPPED", "중단 요청 또는 종료 시각에 도달했습니다. 새 예약 요청은 보내지 않습니다."
                )
        except Exception as error:
            # RESERVING 이후 실패는 예약 생성 여부가 불확실할 수 있으므로 잠금을 유지합니다.
            state = "UNKNOWN" if record.state == "RESERVING" else "FAILED"
            record = self._state(record, state, safe_error(error))
        finally:
            self.searches.pop(search.owner, None)
            try:
                await self._release(search.owner, search.account, search.backend)
            finally:
                current = self.tasks.get(search.owner)
                if current is not None and current[0] == record.id:
                    self.tasks.pop(search.owner, None)
                await self._notice(record)

    async def _reservation(self, backend: Backend, record: JobRecord) -> Reservation:
        reservation = await backend.reservation(record.reservation_id)
        if (
            reservation is None
            or reservation.rsv_id != record.reservation_id
            or reservation.is_waiting
            or not reservation.wct_no
            or reservation.price <= 0
        ):
            raise BotError("결제 가능한 예약을 확인하지 못했습니다. /check 또는 코레일 앱에서 확인하세요.")
        from datetime import datetime

        try:
            deadline = datetime.strptime(
                reservation.buy_limit_date + reservation.buy_limit_time, "%Y%m%d%H%M%S"
            ).replace(tzinfo=KST)
        except ValueError:
            raise BotError("결제 기한을 확인할 수 없어 결제하지 않습니다.") from None
        if deadline <= now_kst():
            raise BotError("예약 결제 기한이 지났습니다.")
        return reservation

    async def quote(self, owner: int, job_id: str) -> Quote:
        async with self._guard(owner):
            if not self.settings.payments:
                raise BotError("봇 결제는 비활성화되어 있습니다. 코레일 앱에서 결제하세요.")
            record = self.store.get(owner, job_id)
            if record.state != "RESERVED" or owner in self.tasks:
                raise BotError("현재 상태에서는 결제를 시작할 수 없습니다.")
            credential = self.store.credential(owner)
            if credential.account != record.account or credential.card is None:
                raise BotError("예약 계정 또는 등록된 카드를 확인하세요.")
            self._claim(owner, credential)
            backend = self.factory(credential)
            try:
                reservation = await self._reservation(backend, record)
                if reservation.price > self.settings.max_payment:
                    raise BotError("설정한 결제 한도를 초과했습니다. 코레일 앱에서 확인하세요.")
                quote = Quote(
                    secrets.token_hex(6),
                    job_id,
                    owner,
                    reservation.price,
                    now_kst() + timedelta(seconds=60),
                    credential,
                )
                self.quotes[owner] = quote
                return quote
            finally:
                await self._release(owner, credential.account, backend)

    async def pay(self, owner: int, job_id: str, nonce: str) -> None:
        async with self._guard(owner):
            quote = self.quotes.get(owner)
            if (
                not self.settings.payments
                or quote is None
                or quote.job != job_id
                or quote.nonce != nonce
                or quote.expires <= now_kst()
            ):
                raise BotError("결제 확인이 만료됐습니다. 새로 확인하세요.")
            record = self.store.get(owner, job_id)
            if record.state != "RESERVED":
                raise BotError("이미 처리 중이거나 처리된 결제입니다.")
            credential = self.store.credential(owner)
            if credential != quote.credential or credential.card is None:
                raise BotError("결제 정보가 변경됐습니다. 다시 확인하세요.")
            self.quotes.pop(owner)
            self._claim(owner, credential)
            backend = self.factory(credential)
            try:
                reservation = await self._reservation(backend, record)
                if reservation.price != quote.price or reservation.price > self.settings.max_payment:
                    raise BotError("결제 금액이 변경됐습니다. 다시 확인하세요.")
                record = self._state(record, "PAYING", "결제 요청 중입니다. 추가 결제를 누르지 마세요.")
                try:
                    await backend.pay(reservation, credential.card)
                    tickets = await backend.tickets()
                    if not any(t.pnr_no == record.reservation_id for t in tickets):
                        raise BotError("발권 결과를 확인하지 못했습니다.")
                except Exception:
                    record = self._state(
                        record,
                        "UNKNOWN",
                        "결제·발권 결과가 불확실합니다. /check 또는 앱에서 확인하세요. 재결제하지 않습니다.",
                    )
                else:
                    record = self._state(
                        record, "PAID", "결제 후 승차권을 확인했습니다. 코레일 앱에서 최종 확인하세요."
                    )
                await self._notice(record)
            finally:
                await self._release(owner, credential.account, backend)

    async def check(self, owner: int, job_id: str) -> JobRecord:
        async with self._guard(owner):
            record = self.store.get(owner, job_id)
            if record.state in TERMINAL or not record.reservation_id:
                return record
            credential = self.store.credential(owner)
            if credential.account != record.account:
                raise BotError("예약 당시 계정과 현재 계정이 다릅니다.")
            self._claim(owner, credential)
            backend = self.factory(credential)
            try:
                if any(t.pnr_no == record.reservation_id for t in await backend.tickets()):
                    return self._state(record, "PAID", "발권된 승차권을 확인했습니다.")
                reservation = await backend.reservation(record.reservation_id)
                if reservation is None:
                    return self._state(
                        record,
                        "UNKNOWN",
                        "예약·승차권을 확인하지 못했습니다. 앱에서 확인한 후 처리완료 버튼을 사용하세요.",
                    )
                state: State = (
                    "UNKNOWN" if record.state == "UNKNOWN" else ("WAITLIST" if reservation.is_waiting else "RESERVED")
                )
                return self._state(
                    record, state, f"현재 예약: {reservation}\n불확실한 결제는 앱에서 확인하기 전 재시도하지 마세요."
                )
            finally:
                await self._release(owner, credential.account, backend)

    def acknowledge(self, owner: int, job_id: str) -> JobRecord:
        self._allowed(owner)
        if owner in self.tasks or self._locks.get(owner, asyncio.Lock()).locked():
            raise BotError("진행 중인 작업이 끝난 뒤 확인하세요.")
        record = self.store.get(owner, job_id)
        if record.state not in {"RESERVED", "WAITLIST", "UNKNOWN"}:
            raise BotError("확인 대기 상태인 작업만 처리완료로 표시할 수 있습니다.")
        self.quotes.pop(owner, None)
        return self._state(
            record, "CLOSED", "사용자가 코레일 앱에서 처리완료를 확인했습니다. 봇은 예약·승차권을 취소하지 않았습니다."
        )

    async def close(self) -> None:
        self._closing = True
        running = list(self.tasks.values())
        for _, _, event in running:
            event.set()
        await asyncio.gather(*(task for _, task, _ in running), return_exceptions=True)
        for owner, search in list(self.searches.items()):
            await self._release(owner, search.account, search.backend)
        self.searches.clear()
