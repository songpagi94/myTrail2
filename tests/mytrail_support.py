"""myTrail 봇의 오프라인 테스트 경계와 사용자별 임시 저장소."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet
from telegram import Message

from pykorail import AdultPassenger, Reservation, Train
from srtgo.bot import handlers, session, storage
from srtgo.service import journal
from tests.payloads import TRAIN_INFO

TRAIN = replace(Train.from_response(TRAIN_INFO), dep_date="20991003", run_date="20991003", arr_date="20991003")
RESERVATION = replace(
    Reservation.from_response(TRAIN_INFO),
    train=TRAIN,
    rsv_id="test-reservation",
    price=1000,
    wct_no="test-window",
    buy_limit_date="20991003",
    buy_limit_time="085000",
)
CARD = {"number": "0" * 16, "password": "00", "birthday": "000000", "expire": "9912"}
PARAMS: dict[str, Any] = {
    "dep": "서울",
    "arr": "부산",
    "date": "20991003",
    "time": "090000",
    "passengers": [AdultPassenger()],
    "include_no_seats": True,
}


@pytest.fixture
def trail_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("BOT_ENABLE_PAYMENTS", raising=False)
    monkeypatch.setenv("BOT_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("BOT_DB_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("BOT_ALLOWED_IDS", "111,222")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "old-bot"))
    monkeypatch.setenv("BOT_TOKEN", "12345:test-token")
    storage._reset_cipher_for_tests()
    monkeypatch.setattr(handlers, "_SESSION", session.Session())
    journal.initialize()
    monkeypatch.setattr(handlers.svc_auth, "create_rail", Mock(side_effect=AssertionError("실제 로그인 금지")))
    return tmp_path


@pytest.fixture
def tmp_user_dir(trail_env: Path) -> Path:
    return storage._users_dir()


@pytest.fixture
def fernet_key(trail_env: Path) -> str:
    return os.environ["BOT_DB_KEY"]


def update(text: str = "", *, owner: int = 111, data: str | None = None, message_id: int = 10) -> Mock:
    result = Mock()
    result.effective_user.id = owner
    result.effective_chat.id = owner
    result.effective_chat.type = "private"
    result.message = Mock(spec=Message)
    result.message.text = text
    result.message.delete = AsyncMock()
    sent = Mock(spec=Message, message_id=message_id, chat_id=owner)
    result.message.reply_text = AsyncMock(return_value=sent)
    result.effective_message = result.message
    result.callback_query = None
    if data is not None:
        result.callback_query = Mock(data=data)
        result.callback_query.answer = AsyncMock()
        result.callback_query.edit_message_text = AsyncMock(return_value=sent)
        result.callback_query.message.message_id = message_id
    return result


def context() -> Mock:
    result = Mock()
    result.user_data = {}
    result.bot.send_message = AsyncMock(return_value=Mock(message_id=10))
    result.bot.edit_message_text = AsyncMock()
    result.application.bot = result.bot
    return result


def registered(owner: int = 111, *, cards: bool = False) -> dict:
    data = {"ktx": {"id": f"user{owner}@example.test", "pw": "test-only"}, "cards": []}
    storage.save(owner, data)
    if cards:
        storage.add_card(owner, CARD, "테스트")
    return data


def search_context() -> Mock:
    ctx = context()
    ctx.user_data["search"] = {
        "rail": Mock(),
        "rail_type": "KTX",
        "trains": [TRAIN],
        "search_params": PARAMS,
        "passengers": PARAMS["passengers"],
        "seat_option": "GENERAL_ONLY",
        "page": 0,
        "message_id": 10,
    }
    return ctx


def pending(owner: int = 111) -> Mock:
    rail = Mock()
    journal.begin(owner, f"account-{owner}", "RESERVING")
    journal.reserved(owner, RESERVATION.rsv_id)
    handlers._SESSION.set_pending(owner, {"rail": rail, "reservation": RESERVATION, "message_id": 10})
    return rail


def saved(owner: int = 111) -> dict:
    result = storage.load(owner)
    assert result is not None
    return result


def saved_card(owner: int, card_id: str) -> dict:
    result = storage.get_card(owner, card_id)
    assert result is not None
    return result


def operation(owner: int = 111) -> tuple[str, str]:
    result = journal.current(owner)
    assert result is not None
    return result
