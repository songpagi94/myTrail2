"""사용자 설정이 요청·서명에 적용되고 기본 페이로드는 유지되는지 검증합니다."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qsl

import pytest
from telegram.ext import ConversationHandler

from pykorail.auth.signer import RequestSigner
from pykorail.client import Korail
from pykorail.constants import API_ENDPOINTS, APP_VERSION, DEFAULT_HEADERS
from pykorail.crypto import encrypt_sid
from pykorail.device.request_settings import RequestSettings
from srtgo.bot import device_settings as ui
from srtgo.bot import storage
from tests.conftest import FakeSession
from tests.dynapath_decoder import decode_token
from tests.mytrail_support import context, update
from tests.payloads import CIPHER_PAYLOAD, LOGIN_OK, SEARCH_PAYLOAD

CASES = [
    ("device_model", "SM-TEST"),
    ("os_version", "14"),
    ("os_type", "TestOS"),
    ("sdk_version", "v1.0.4"),
    ("app_version", "999999999"),
    ("sid_key", "0123456789abcdef"),
    ("device_id", "fedcba9876543210"),
]


@pytest.mark.parametrize(("field", "value"), CASES)
def test_field_override_is_persistent_and_user_specific(trail_env, field, value) -> None:
    # given
    original = storage.request_values_for(111)
    other = storage.request_values_for(222)
    # when
    storage.set_request_value(111, field, value)
    # then
    assert storage.request_values_for(111) == {**original, field: value}
    assert storage.request_values_for(222) == other


@pytest.mark.parametrize(("field", "value"), CASES)
def test_field_reset_restores_original_defaults(trail_env, field, value) -> None:
    # given
    original = storage.request_values_for(111)
    storage.set_request_value(111, field, value)
    # when
    storage.set_request_value(111, field, None)
    # then
    assert storage.request_values_for(111) == original


def test_regeneration_keeps_original_default(trail_env) -> None:
    # given
    original = storage.android_id_for(111)
    # when
    storage.set_request_value(111, "device_id", None, regenerate=True)
    # then
    data = storage.load(111)
    assert data is not None
    assert storage.android_id_for(111) != original
    assert data["default_android_id"] == original


def test_account_registration_preserves_all_overrides(trail_env) -> None:
    # given
    storage.set_request_value(111, "app_version", "999999999")
    # when
    storage.save(111, {"ktx": None, "cards": []})
    # then
    assert storage.request_values_for(111)["app_version"] == "999999999"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sid_key", "short"),
        ("sid_key", "한" * 16),
        ("sdk_version", "x" * 32),
        ("os_type", "bad\nvalue"),
        ("device_model", ""),
        ("app_version", "x" * 33),
    ],
)
def test_invalid_field_does_not_change_saved_settings(trail_env, field, value) -> None:
    # given
    before = storage.request_values_for(111)
    # when & then
    with pytest.raises((ValueError, UnicodeError)):
        storage.set_request_value(111, field, value)
    assert storage.request_values_for(111) == before


def test_sid_key_is_hidden_from_settings_repr() -> None:
    # when
    representation = repr(RequestSettings(sid_key=b"0123456789abcdef"))
    # then
    assert "0123456789abcdef" not in representation


def test_signature_uses_custom_fields_sdk_and_sid_key(monkeypatch) -> None:
    # given
    monkeypatch.setattr("pykorail.auth.dynapath.time.time", lambda: 1_700_000_000)
    settings = RequestSettings(
        device_model="SM-TEST",
        os_version="14",
        os_type="TestOS",
        sdk_version="v1.0.4",
        sid_key=b"0123456789abcdef",
    )
    signer = RequestSigner(device_id="0123456789abcdef", request_settings=settings)
    # when
    headers, sid = signer.sign(API_ENDPOINTS["login"])
    # then
    key, body = decode_token(headers["x-dynapath-m-token"])
    fields = dict(parse_qsl(body))
    assert key.startswith("v1.0.4+")
    assert {name: fields[name] for name in ("dm", "os", "st", "sv", "di")} == {
        "dm": "SM-TEST",
        "os": "14",
        "st": "TestOS",
        "sv": "v1.0.4",
        "di": "0123456789abcdef",
    }
    assert sid == encrypt_sid("AD", 1_700_000_000_000, b"0123456789abcdef")


def test_custom_app_version_reaches_login_and_search(make_korail) -> None:
    # given
    client, session = make_korail(
        {"code": CIPHER_PAYLOAD, "login": LOGIN_OK, "search_schedule": SEARCH_PAYLOAD},
        validate_stations=False,
        request_settings=RequestSettings(app_version="999999999"),
    )
    # when
    client.login("test@example.test", "test-only")
    client.trains.search("서울", "부산", depart_after=datetime(2099, 10, 3, 9))
    # then
    assert session.kwargs_for("login")["data"]["Version"] == "999999999"
    params = session.kwargs_for("search_schedule")["params"]
    assert params["Version"] == "999999999"
    assert params["Sid"] == ""
    assert "Key" not in params


def test_default_settings_preserve_full_login_request(monkeypatch, trail_env) -> None:
    # given
    monkeypatch.setattr("pykorail.auth.dynapath.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("pykorail.auth.signer.random.choices", lambda *args, **kwargs: list("AB12"))
    original = FakeSession({"code": CIPHER_PAYLOAD, "login": LOGIN_OK})
    configured = FakeSession({"code": CIPHER_PAYLOAD, "login": LOGIN_OK})
    sessions = iter([original, configured])

    def session_factory(headers):
        session = next(sessions)
        session.headers.update(headers)
        return session

    monkeypatch.setattr("pykorail.client.create_session", session_factory)
    identity = storage.android_id_for(111)
    first = Korail(android_id=identity)
    second = Korail(android_id=identity, request_settings=storage.request_settings_for(111))
    # when
    first.login("test@example.test", "test-only")
    second.login("test@example.test", "test-only")
    # then
    assert original.headers == configured.headers == DEFAULT_HEADERS
    assert original.calls == configured.calls
    assert original.kwargs_for("login")["data"]["Version"] == APP_VERSION


def test_custom_model_and_os_are_consistent_in_headers_and_signature(monkeypatch) -> None:
    # given
    captured = {}
    monkeypatch.setattr("pykorail.client.create_session", lambda headers: captured.update(headers) or FakeSession({}))
    settings = RequestSettings(device_model="SM-TEST", os_version="14", os_type="TestOS")
    # when
    client = Korail(request_settings=settings)
    # then
    assert "TestOS 14; SM-TEST Build/" in captured["User-Agent"]
    assert client._api._signer._engine.device_model == "SM-TEST"
    assert client._api._signer._engine.os_version == "14"


@pytest.mark.parametrize(("field", "value"), CASES)
async def test_manual_edit_displays_new_values_and_keeps_field_menu(trail_env, field, value) -> None:
    # given
    ctx = context()
    ctx.user_data["device_settings_field"] = field
    event = update(value)
    # when
    result = await ui.receive(event, ctx)
    # then
    assert result == ui.FIELD
    text = event.message.reply_text.call_args.args[0]
    assert f"{ui.LABELS[field]}: {value}" in text
    assert all(label in text for label in ui.LABELS.values())


@pytest.mark.parametrize(
    ("callback", "state"),
    [
        ("dev:fields", ui.FIELDS),
        ("dev:menu", ui.MENU),
        ("dev:field:os_type", ui.FIELD),
        ("dev:manual:os_type", ui.INPUT),
        ("dev:cancel", ConversationHandler.END),
    ],
)
async def test_navigation_and_cancel_preserve_values(trail_env, callback, state) -> None:
    # given
    original = storage.request_values_for(111)
    ctx = context()
    ctx.user_data["device_settings_message"] = 10
    # when
    result = await ui.choose(update(data=callback), ctx)
    # then
    assert result == state
    assert storage.request_values_for(111) == original


def test_device_id_field_has_regeneration_and_default_restore() -> None:
    # when
    buttons = ui._keyboard(ui.FIELD, "device_id").inline_keyboard
    # then
    assert [button.text for row in buttons for button in row] == [
        "수동 편집",
        "자동 재생성",
        "기본값으로 복원",
        "돌아가기",
    ]
