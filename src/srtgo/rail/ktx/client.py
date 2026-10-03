"""myTrail 인터페이스와 pykorail 사이의 호환 계층. HTTP 구현은 복제하지 않습니다."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, TypeVar

from pykorail import Card, NoResultsError, ReserveOption, SoldOutError, TrainType
from pykorail import Korail as Client
from srtgo.bot.storage import android_id_for
from srtgo.service import journal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pykorail import Passenger, Reservation, Ticket, Train
    from pykorail.options import ReserveOptionCode

T = TypeVar("T")
KST = timezone(timedelta(hours=9))


class Korail:
    """동기 myTrail 호출을 전용 스레드의 한 pykorail 세션으로 연결합니다."""

    def __init__(self, owner: int) -> None:
        self.owner = owner
        self._client: Client | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._account = ""

    def _call(self, operation: Callable[[Client], T]) -> T:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mytrail-korail")

        def invoke() -> T:
            if self._client is None:
                self._client = Client(android_id=android_id_for(self.owner))
            return operation(self._client)

        return self._executor.submit(invoke).result()

    @property
    def is_login(self) -> bool:
        return self._call(lambda client: client.logined)

    def login(self, user_id: str, password: str) -> bool:
        def operation(client: Client) -> bool:
            client.login(user_id, password)
            # 이메일/전화번호가 달라도 같은 회원번호는 같은 계정으로 잠급니다.
            if not client.membership_number:
                raise ValueError("로그인 회원번호를 확인할 수 없습니다.")
            self._account = journal.account_key(client.membership_number)
            return True

        return self._call(operation)

    def logout(self) -> bool:
        self._call(lambda client: client.logout())
        return True

    def search_train(
        self, dep: str, arr: str, date: str, time: str, passengers: Sequence[Passenger], include_no_seats: bool = False
    ) -> list[Train]:
        departure = datetime.strptime(date + time, "%Y%m%d%H%M%S").replace(tzinfo=KST)

        def operation(client: Client) -> list[Train]:
            try:
                return client.trains.search(
                    dep,
                    arr,
                    depart_after=departure,
                    train_type=TrainType.KTX,
                    passengers=passengers,
                    include_no_seats=include_no_seats,
                )
            except NoResultsError:
                return []

        return self._call(operation)

    def reserve(
        self, train: Train, passengers: Sequence[Passenger], option: ReserveOptionCode = ReserveOption.GENERAL_FIRST
    ) -> Reservation:
        def operation(client: Client) -> Reservation:
            journal.begin(self.owner, self._account, "RESERVING")
            try:
                reservation = client.reservations.create(train, passengers, option)
            except SoldOutError:
                journal.finish(self.owner)
                raise
            except Exception:
                journal.uncertain(self.owner)
                raise
            if not reservation.rsv_id:
                journal.uncertain(self.owner)
                raise journal.UncertainOperationError("예약 식별번호를 받지 못했습니다.")
            journal.reserved(self.owner, reservation.rsv_id)
            return reservation

        return self._call(operation)

    def get_reservations(self) -> list[Reservation]:
        return self._call(lambda client: client.reservations.all())

    def get_tickets(self) -> list[Ticket]:
        return self._call(lambda client: client.tickets.all())

    def pay_with_card(self, reservation: Reservation, card_info: dict[str, str]) -> bool:
        card = Card(
            number=card_info["number"],
            password=card_info["password"],
            verify_number=card_info["birthday"],
            expire=card_info["expire"],
            is_corporate=len(card_info["birthday"]) == 10,
        )

        def operation(client: Client) -> bool:
            current = client.reservations.find(reservation.rsv_id)
            if (
                current is None
                or current.rsv_id != reservation.rsv_id
                or current.is_waiting
                or not current.wct_no
                or current.price <= 0
            ):
                raise ValueError("결제 가능한 예약을 확인하지 못했습니다.")
            if current.price != reservation.price:
                raise ValueError("예약 금액이 변경되었습니다. 코레일 앱에서 확인하세요.")
            deadline = datetime.strptime(current.buy_limit_date + current.buy_limit_time, "%Y%m%d%H%M%S").replace(
                tzinfo=KST
            )
            if deadline <= datetime.now(KST):
                raise ValueError("예약 결제 기한이 지났습니다.")
            journal.begin(self.owner, self._account, "PAYING", reservation.rsv_id)
            try:
                client.reservations.pay(current, card)
            except Exception:
                journal.uncertain(self.owner)
                raise
            journal.finish(self.owner)
            # pykorail은 성공 시 None, myTrail은 성공 시 True를 기대합니다.
            return True

        return self._call(operation)

    def cancel(self, reservation: Reservation) -> bool:
        def operation(client: Client) -> bool:
            journal.begin(self.owner, self._account, "CANCELLING", reservation.rsv_id)
            try:
                client.reservations.cancel(reservation)
            except Exception:
                journal.uncertain(self.owner)
                raise
            journal.finish(self.owner)
            return True

        return self._call(operation)

    def close(self) -> None:
        if self._executor is not None:
            try:
                # ID 저장 실패로 생성되지 않은 클라이언트를 종료 중에 만들지 않습니다.
                if self._client is not None:
                    self._call(lambda client: client.close())
            finally:
                self._executor.shutdown(wait=True)
                self._executor = None
                self._client = None
