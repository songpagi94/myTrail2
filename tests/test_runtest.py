"""수동 조회 스크립트가 실제 통신 없이 조회만 하는지 검증합니다."""

from __future__ import annotations

import runpy
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
import runtest

from pykorail.constants import API_ENDPOINTS
from tests.payloads import CIPHER_PAYLOAD, LOGIN_FAIL, LOGIN_OK, SEARCH_PAYLOAD, STATION_PAYLOAD


@pytest.fixture
def script_session(make_korail, monkeypatch: pytest.MonkeyPatch):
    """기존 가짜 세션과 역 샘플로 로그인·조회 요청을 받습니다."""
    _, session = make_korail(
        {
            "code": CIPHER_PAYLOAD,
            "login": LOGIN_OK,
            "stationdata": STATION_PAYLOAD,
            "search_schedule": SEARCH_PAYLOAD,
        }
    )
    monkeypatch.setattr(runtest, "DEPARTURE_STATION", "서울")
    monkeypatch.setattr(runtest, "ARRIVAL_STATION", "부산")
    monkeypatch.setattr(runtest, "DEPART_AFTER", datetime(2099, 10, 3, 9))
    monkeypatch.setattr("builtins.input", Mock(side_effect=["y", "reader@example.com"]))
    monkeypatch.setattr(runtest, "getpass", Mock(return_value="test-only-password"))
    return session


def test_script_only_logs_in_and_searches(script_session, capsys: pytest.CaptureFixture[str]) -> None:
    # when
    result = runtest.main()

    # then
    assert result == 0
    assert script_session.urls() == [
        API_ENDPOINTS[name] for name in ("code", "login", "stationdata", "search_schedule")
    ]
    assert script_session.closed
    params = script_session.kwargs_for("search_schedule")["params"]
    assert (params["txtGoStart"], params["txtGoEnd"], params["txtGoAbrdDt"], params["txtGoHour"]) == (
        "서울",
        "부산",
        "20991003",
        "090000",
    )
    assert params["txtPsgFlg_1"] == 1
    output = capsys.readouterr().out
    assert "조회 결과: 1건" in output
    assert "KTX" in output
    assert "reader@example.com" not in output
    assert "test-only-password" not in output


@pytest.mark.parametrize("answer", ["", "n"])
def test_declining_does_not_connect(script_session, monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    # given
    monkeypatch.setattr("builtins.input", Mock(return_value=answer))

    # when
    result = runtest.main()

    # then
    assert result == 0
    assert script_session.calls == []


@pytest.mark.parametrize(("user_id", "password"), [("", "test-only-password"), ("reader@example.com", "")])
def test_empty_credentials_do_not_connect(
    script_session, monkeypatch: pytest.MonkeyPatch, user_id: str, password: str
) -> None:
    # given
    monkeypatch.setattr("builtins.input", Mock(side_effect=["y", user_id]))
    monkeypatch.setattr(runtest, "getpass", Mock(return_value=password))

    # when
    result = runtest.main()

    # then
    assert result == 1
    assert script_session.calls == []


def test_login_failure_stops_before_search(script_session, capsys: pytest.CaptureFixture[str]) -> None:
    # given
    script_session.routes["login"] = LOGIN_FAIL

    # when
    result = runtest.main()

    # then
    assert result == 1
    assert script_session.urls() == [API_ENDPOINTS[name] for name in ("code", "login")]
    assert script_session.closed
    assert "LoginFailedError" in capsys.readouterr().out


def test_no_results_is_reported_without_retry(script_session, capsys: pytest.CaptureFixture[str]) -> None:
    # given
    script_session.routes["search_schedule"] = {**SEARCH_PAYLOAD, "trn_infos": {"trn_info": []}}

    # when
    result = runtest.main()

    # then
    assert result == 0
    assert len(script_session.all_kwargs_for("search_schedule")) == 1
    assert script_session.closed
    assert "조건에 맞는 예약 가능 열차가 없습니다" in capsys.readouterr().out


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_interrupted_input_does_not_connect(
    script_session, monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    # given
    monkeypatch.setattr("builtins.input", Mock(side_effect=error))

    # when
    result = runtest.main()

    # then
    assert result == 130
    assert script_session.calls == []


def test_importing_script_does_not_prompt_or_connect(script_session, monkeypatch: pytest.MonkeyPatch) -> None:
    # given
    prompt = Mock(side_effect=AssertionError("가져오기 중에는 입력을 요청하면 안 됩니다"))
    monkeypatch.setattr("builtins.input", prompt)

    # when
    runpy.run_path(str(Path(runtest.__file__)), run_name="runtest_import_check")

    # then
    prompt.assert_not_called()
    assert script_session.calls == []
