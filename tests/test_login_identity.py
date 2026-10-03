"""기기 신원 유지와 HTTP 차단 표시를 네트워크 없이 검증합니다."""

from __future__ import annotations

import json
from unittest.mock import Mock
from urllib.parse import parse_qsl

import pytest

from pykorail import HttpStatusError, SoldOutError
from pykorail.api import ApiClient
from pykorail.auth.signer import RequestSigner
from pykorail.constants import API_ENDPOINTS
from pykorail.device.android_id import generate_android_id, validate_android_id
from srtgo.bot import storage
from srtgo.bot.errors import safe_error
from srtgo.rail.ktx import client as adapter
from tests.dynapath_decoder import decode_token
from tests.payloads import LOGIN_FORBIDDEN


def test_generated_ids_are_distinct_and_valid() -> None:
    # when
    ids = [generate_android_id(), generate_android_id()]
    # then
    assert ids[0] != ids[1]
    assert [validate_android_id(value) for value in ids] == ids


@pytest.mark.parametrize("value", ["", "ABCDEF0123456789", "bad", None, 123])
def test_invalid_saved_id_is_rejected(value) -> None:
    # when & then
    with pytest.raises(ValueError, match="android_id"):
        validate_android_id(value)


def test_signer_keeps_identity_across_requests() -> None:
    # given
    signer = RequestSigner()
    # when
    headers = [signer.sign(API_ENDPOINTS["login"])[0], signer.sign(API_ENDPOINTS["login"])[0]]
    # then
    ids = [dict(parse_qsl(decode_token(item["x-dynapath-m-token"])[1]))["di"] for item in headers]
    assert ids == [signer._device_id, signer._device_id]


def test_identity_change_preserves_other_signature_fields_and_sid(monkeypatch) -> None:
    # given
    monkeypatch.setattr("pykorail.auth.dynapath.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("pykorail.auth.signer.random.choices", lambda *args, **kwargs: list("AB12"))
    old = RequestSigner(device_id="558a4f02041657ea")
    new = RequestSigner(device_id=generate_android_id())
    # when
    results = [old.sign(API_ENDPOINTS["login"]), new.sign(API_ENDPOINTS["login"])]
    # then
    fields = [dict(parse_qsl(decode_token(headers["x-dynapath-m-token"])[1])) for headers, _ in results]
    assert fields[0]["di"] != fields[1]["di"]
    assert [{key: value for key, value in item.items() if key != "di"} for item in fields] == [
        {key: value for key, value in fields[0].items() if key != "di"}
    ] * 2
    assert results[0][1] == results[1][1]


def test_user_identity_survives_reload_and_account_registration(trail_env) -> None:
    # given
    storage.save(111, {"ktx": {"id": "test@example.test"}, "cards": []})
    original = storage.android_id_for(111)
    # when
    storage.save(111, {"ktx": {"id": "new@example.test"}, "cards": []})
    # then
    assert storage.android_id_for(111) == original
    assert storage.android_id_for(222) != original
    saved = storage.load(111)
    assert saved is not None
    assert saved["ktx"]["id"] == "new@example.test"


def test_adapter_restores_identity_in_new_client(trail_env, monkeypatch) -> None:
    # given
    factory = Mock(return_value=Mock(membership_number="0000000000"))
    monkeypatch.setattr(adapter, "Client", factory)
    original = storage.android_id_for(111)
    first, second = adapter.Korail(111), adapter.Korail(111)
    # when
    first.login("test@example.test", "test-only")
    first.close()
    second.login("test@example.test", "test-only")
    second.close()
    # then
    assert [call.kwargs for call in factory.call_args_list] == [{"android_id": original}] * 2


def test_explicit_client_identity_reaches_signer(make_korail) -> None:
    # given
    identity = generate_android_id()
    # when
    client, _ = make_korail({}, android_id=identity)
    # then
    assert client.android_id == identity
    assert client._api._signer._device_id == identity


def test_invalid_identity_is_rejected_before_session_creation(monkeypatch) -> None:
    # given
    from pykorail import Korail

    factory = Mock()
    monkeypatch.setattr("pykorail.client.create_session", factory)
    # when & then
    with pytest.raises(ValueError, match="android_id"):
        Korail(android_id="invalid")
    factory.assert_not_called()


def test_storage_failure_prevents_login(trail_env, monkeypatch) -> None:
    # given
    factory = Mock()
    monkeypatch.setattr(adapter, "Client", factory)
    monkeypatch.setattr(storage, "save", Mock(side_effect=OSError("disk unavailable")))
    rail = adapter.Korail(111)
    # when & then
    with pytest.raises(OSError, match="disk unavailable"):
        rail.login("test@example.test", "test-only")
    rail.close()
    factory.assert_not_called()


def test_saved_identity_is_encrypted(trail_env) -> None:
    # when
    identity = storage.android_id_for(111)
    # then
    assert identity.encode("ascii") not in storage._path(111).read_bytes()


@pytest.mark.parametrize("body", [LOGIN_FORBIDDEN, [], "private-html"])
def test_http_rejection_does_not_become_password_error(body) -> None:
    # given
    response = Mock(status_code=403, text=json.dumps(body) if not isinstance(body, str) else body)
    api = ApiClient(Mock(), Mock())
    # when & then
    with pytest.raises(HttpStatusError) as caught:
        api._parse(response, API_ENDPOINTS["login"])
    assert caught.value.status_code == 403
    assert "private-html" not in str(caught.value)


def test_forbidden_code_is_preserved_and_shown_safely(caplog) -> None:
    # given
    api = ApiClient(Mock(), Mock())
    response = Mock(status_code=403, text=json.dumps(LOGIN_FORBIDDEN))
    # when
    with pytest.raises(HttpStatusError) as caught:
        api._parse(response)
    message = safe_error(caught.value, operation="login")
    # then
    assert caught.value.code == "-2000"
    assert "HTTP 403, code=-2000" in message
    assert "/setup" not in message
    assert LOGIN_FORBIDDEN["id"] not in message + caplog.text


def test_standard_failure_keeps_korail_error_mapping() -> None:
    # given
    api = ApiClient(Mock(), Mock())
    response = Mock(status_code=403, text=json.dumps({"strResult": "FAIL", "h_msg_cd": "IRT010110"}))
    # when & then
    with pytest.raises(SoldOutError):
        api.check(api._parse(response))
