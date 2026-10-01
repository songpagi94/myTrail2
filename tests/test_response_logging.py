"""코레일 진단 로그가 응답 결과를 남기고 비밀과 요청은 보존하는지 검증합니다."""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

from pykorail.api import ApiClient, diagnostic_code
from pykorail.constants import API_ENDPOINTS
from pykorail.exceptions import LoginFailedError
from srtgo.bot.errors import safe_error
from tests.conftest import FakeResponse
from tests.payloads import LOGIN_FAIL, LOGIN_FORBIDDEN


@pytest.mark.parametrize("method", ["get", "post"])
def test_response_log_preserves_request_and_omits_private_body(caplog, method) -> None:
    # given
    payload = {"strResult": "FAIL", "h_msg_cd": "P058", "h_msg_txt": "private-message", "token": "private-token"}
    session = Mock()
    request = getattr(session, method)
    request.return_value = FakeResponse(payload)
    api = ApiClient(session, Mock())
    caplog.set_level(logging.INFO, logger="pykorail.responses")

    # when
    result = getattr(api, method)(API_ENDPOINTS["login"], params={"test": "unchanged"})

    # then
    assert result == payload
    request.assert_called_once_with(API_ENDPOINTS["login"], params={"test": "unchanged"})
    assert "endpoint=login strResult=FAIL h_msg_cd=P058" in caplog.text
    assert "private-message" not in caplog.text
    assert "private-token" not in caplog.text


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, "[없음]"),
        ("", "[없음]"),
        ("IRT010110", "IRT010110"),
        ("2000", "2000"),
        ("-2000", "-2000"),
        (2000, "2000"),
        (-2000, "-2000"),
        (True, "[비표준 코드 생략]"),
        (1234567890, "[비표준 코드 생략]"),
        ("1234567890", "[비표준 코드 생략]"),
        ("P058\nsecret", "[비표준 코드 생략]"),
        ({"secret": "value"}, "[비표준 코드 생략]"),
    ],
)
def test_diagnostic_code_rejects_private_or_malformed_values(code, expected) -> None:
    # when
    result = diagnostic_code(code)

    # then
    assert result == expected


def test_login_error_log_keeps_code_without_original_message(caplog) -> None:
    # given
    error = LoginFailedError("private-account-and-password", "WRT999999")

    # when
    message = safe_error(error, operation="login")

    # then
    assert "로그인에 실패" in message
    assert "operation=login error=LoginFailedError h_msg_cd=WRT999999" in caplog.text
    assert "private-account-and-password" not in caplog.text


def test_transport_error_log_omits_remote_body(caplog) -> None:
    # given
    from pykorail.exceptions import TransportError

    error = TransportError("private-html-body")

    # when
    message = safe_error(error, operation="search")

    # then
    assert "TransportError" in message
    assert "operation=search error=TransportError" in caplog.text
    assert "private-html-body" not in caplog.text


@pytest.mark.parametrize("code", ["-2000", -2000])
def test_numeric_server_code_is_logged_without_changing_response(caplog, code) -> None:
    # given
    payload = {**LOGIN_FAIL, "h_msg_cd": code}
    response = FakeResponse(payload)
    api = ApiClient(Mock(), Mock())
    caplog.set_level(logging.INFO, logger="pykorail.responses")

    # when
    result = api._parse(response, API_ENDPOINTS["login"])

    # then
    assert result == payload
    assert "h_msg_cd=-2000" in caplog.text
    assert LOGIN_FAIL["h_msg_txt"] not in caplog.text


@pytest.mark.parametrize("status", [200, 403, 500])
def test_unknown_login_response_logs_status_and_structure_without_values(caplog, status) -> None:
    # given
    response = Mock(status_code=status, text='{"strMbCrdNo":"private-member", "strResult":null}')
    api = ApiClient(Mock(), Mock())
    caplog.set_level(logging.INFO, logger="pykorail.responses")

    # when
    result = api._parse(response, API_ENDPOINTS["login"])

    # then
    assert result["strResult"] is None
    assert f"endpoint=login status={status}" in caplog.text
    assert "strResult_state=NoneType" in caplog.text
    assert "fields=strMbCrdNo:str,strResult:NoneType" in caplog.text
    assert "private-member" not in caplog.text


def test_empty_login_response_distinguishes_missing_result(caplog) -> None:
    # given
    api = ApiClient(Mock(), Mock())

    # when
    result = api._parse(FakeResponse({}), API_ENDPOINTS["login"])

    # then
    assert result == {}
    assert "strResult_state=MISSING fields=[없음]" in caplog.text


@pytest.mark.parametrize("code", [-2000, "-2000", 2000, 0])
def test_forbidden_response_logs_code_without_id_or_message(caplog, code) -> None:
    # given
    payload = {**LOGIN_FORBIDDEN, "code": code}
    response = FakeResponse(payload)
    api = ApiClient(Mock(), Mock())
    caplog.set_level(logging.INFO, logger="pykorail.responses")

    # when
    result = api._parse(response, API_ENDPOINTS["login"])

    # then
    assert result == payload
    assert f" code={code}" in caplog.text
    assert "h_msg_cd=[없음]" in caplog.text
    assert LOGIN_FORBIDDEN["id"] not in caplog.text
    assert LOGIN_FORBIDDEN["message"] not in caplog.text
