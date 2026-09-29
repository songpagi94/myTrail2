"""사용자별 실조회 횟수·경과 시간과 작업 수명 검증."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import Mock

import pytest

from pykorail import ReserveOption
from srtgo.bot import handlers
from srtgo.bot.session import Session
from srtgo.service import progress as tracking
from srtgo.service import reservation as service
from tests.mytrail_support import PARAMS, RESERVATION, TRAIN, context, update


def test_elapsed_seconds_use_monotonic_clock(monkeypatch) -> None:
    # given
    monkeypatch.setattr(tracking, "monotonic", Mock(side_effect=[100.0, 142.9]))
    progress = tracking.PollProgress()
    progress.query_started()

    # when
    snapshot = progress.snapshot()

    # then
    assert snapshot == tracking.PollSnapshot(elapsed_seconds=42, attempts=1, searching=True)


def test_query_completion_does_not_increment_count() -> None:
    # given
    progress = tracking.PollProgress()
    progress.query_started()

    # when
    progress.query_finished()

    # then
    assert progress.snapshot().attempts == 1
    assert not progress.snapshot().searching


@pytest.mark.parametrize("first_result", [[], TimeoutError()])
def test_polling_counts_empty_and_failed_queries(monkeypatch, first_result) -> None:
    # given
    progress = tracking.PollProgress()
    rail = Mock()
    rail.search_train.side_effect = [first_result, [TRAIN]]
    rail.reserve.return_value = RESERVATION
    monkeypatch.setattr(service, "_sleep", Mock())

    # when
    service.poll_and_reserve(
        rail,
        PARAMS,
        [0],
        ReserveOption.GENERAL_ONLY,
        Mock(),
        Mock(return_value=True),
        threading.Event(),
        PARAMS["passengers"],
        [TRAIN],
        progress,
    )

    # then
    assert progress.snapshot().attempts == 2
    assert not progress.snapshot().searching


def test_count_is_visible_while_query_is_in_flight() -> None:
    # given
    progress = tracking.PollProgress()
    rail = Mock()
    snapshots = []
    rail.search_train.side_effect = lambda **kwargs: snapshots.append(progress.snapshot()) or [TRAIN]
    rail.reserve.return_value = RESERVATION

    # when
    service.poll_and_reserve(
        rail,
        PARAMS,
        [0],
        ReserveOption.GENERAL_ONLY,
        Mock(),
        Mock(),
        threading.Event(),
        PARAMS["passengers"],
        [TRAIN],
        progress,
    )

    # then
    assert snapshots[0].attempts == 1
    assert snapshots[0].searching
    assert not progress.snapshot().searching


def test_permanent_query_error_finishes_progress() -> None:
    # given
    progress = tracking.PollProgress()
    rail = Mock()
    rail.search_train.side_effect = RuntimeError()

    # when
    service.poll_and_reserve(
        rail,
        PARAMS,
        [0],
        ReserveOption.GENERAL_ONLY,
        Mock(),
        Mock(return_value=False),
        threading.Event(),
        PARAMS["passengers"],
        [TRAIN],
        progress,
    )

    # then
    assert progress.snapshot().attempts == 1
    assert not progress.snapshot().searching


@pytest.mark.parametrize("searching", [True, False])
async def test_status_shows_elapsed_seconds_and_attempts(trail_env, monkeypatch, searching) -> None:
    # given
    monkeypatch.setattr(handlers._SESSION, "is_polling", Mock(return_value=True))
    monkeypatch.setattr(handlers._SESSION, "get_progress", Mock(return_value=tracking.PollSnapshot(42, 7, searching)))
    event = update()

    # when
    await handlers.cmd_status(event, context())

    # then
    text = event.message.reply_text.call_args.args[0]
    assert "42초" in text
    assert "조회 시도: 7회" in text
    assert ("7번째 조회 응답" in text) is searching


async def test_progress_is_isolated_and_removed_when_finished() -> None:
    # given
    session = Session()
    done = asyncio.Event()
    task = asyncio.create_task(done.wait())
    progress = tracking.PollProgress()
    session.start_poll(111, task, threading.Event(), progress)
    progress.query_started()

    # when
    own = session.get_progress(111)
    other = session.get_progress(222)
    done.set()
    await task
    await asyncio.sleep(0)

    # then
    assert own is not None
    assert own.attempts == 1
    assert other is None
    assert session.get_progress(111) is None
    assert 111 not in session._progress


async def test_new_job_starts_with_zero_count() -> None:
    # given
    session = Session()
    done = asyncio.Event()
    previous = asyncio.create_task(done.wait())
    old_progress = tracking.PollProgress()
    session.start_poll(111, previous, threading.Event(), old_progress)
    old_progress.query_started()
    done.set()
    await previous
    task = asyncio.create_task(asyncio.sleep(0))

    # when
    session.start_poll(111, task, threading.Event())
    snapshot = session.get_progress(111)
    await task

    # then
    assert snapshot is not None
    assert snapshot.attempts == 0
    assert not snapshot.searching
