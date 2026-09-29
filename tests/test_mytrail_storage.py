"""myTrail의 기존 암호화 저장·카드 호환 회귀 테스트."""

from __future__ import annotations

import pytest


def test_save_and_load_round_trip(tmp_user_dir, fernet_key) -> None:
    # when & then
    from srtgo.bot import storage

    data = {
        "srt": {"id": "u", "pw": "p"},
        "ktx": None,
        "cards": [
            {"id": "ab12", "label": "신한", "number": "1", "password": "2", "birthday": "3", "expire": "4"},
        ],
    }
    storage.save(123456, data)
    assert storage.exists(123456)
    assert storage.load(123456) == data


def test_load_missing_returns_none(tmp_user_dir, fernet_key) -> None:
    # when & then
    from srtgo.bot import storage

    assert storage.load(999) is None
    assert not storage.exists(999)


def test_load_with_wrong_key_raises(tmp_user_dir, fernet_key, monkeypatch) -> None:
    # when & then
    from cryptography.fernet import Fernet

    from srtgo.bot import storage

    storage.save(1, {"a": 1})

    # 키 교체 후 읽기
    monkeypatch.setenv("BOT_DB_KEY", Fernet.generate_key().decode())
    storage._reset_cipher_for_tests()
    with pytest.raises(storage.StorageDecryptError):
        storage.load(1)


def test_delete_removes_file(tmp_user_dir, fernet_key) -> None:
    # when & then
    from srtgo.bot import storage

    storage.save(7, {"x": 1})
    storage.delete(7)
    assert not storage.exists(7)


def test_list_user_ids(tmp_user_dir, fernet_key) -> None:
    # when & then
    from srtgo.bot import storage

    storage.save(1, {"x": 1})
    storage.save(2, {"x": 2})
    assert sorted(storage.list_user_ids()) == [1, 2]


def test_load_migrates_legacy_card_to_cards(tmp_user_dir, fernet_key, monkeypatch) -> None:
    # when & then
    """legacy 단수 card 키가 cards 리스트로 자동 변환된다."""
    from srtgo.bot import storage

    legacy = {
        "srt": {"id": "u", "pw": "p"},
        "ktx": None,
        "card": {"number": "1111", "password": "12", "birthday": "900101", "expire": "1230"},
    }
    storage.save(1, legacy)

    # token_hex를 결정적으로 만들어 검증 단순화
    monkeypatch.setattr(storage.secrets, "token_hex", lambda n: "ab12")

    loaded = storage.load(1)

    assert loaded is not None
    assert "card" not in loaded
    assert loaded["cards"] == [
        {
            "id": "ab12",
            "label": None,
            "number": "1111",
            "password": "12",
            "birthday": "900101",
            "expire": "1230",
        }
    ]
    assert loaded["srt"] == {"id": "u", "pw": "p"}
    assert loaded["ktx"] is None


def test_load_persists_migration_to_disk(tmp_user_dir, fernet_key, monkeypatch) -> None:
    # when & then
    """마이그레이션 후 디스크도 새 포맷으로 갱신되어 두 번째 load는 변환 없이 동일."""
    from srtgo.bot import storage

    legacy = {"srt": None, "ktx": None, "card": {"number": "n", "password": "p", "birthday": "b", "expire": "e"}}
    storage.save(1, legacy)

    monkeypatch.setattr(storage.secrets, "token_hex", lambda n: "ab12")
    first = storage.load(1)

    # 두 번째 load는 마이그레이션 안 일어남 (token_hex가 다른 값을 줘도 영향 없음)
    monkeypatch.setattr(storage.secrets, "token_hex", lambda n: "ffff")
    second = storage.load(1)

    assert first == second
    assert second is not None
    assert second["cards"][0]["id"] == "ab12"


def test_load_idempotent_for_already_new_format(tmp_user_dir, fernet_key) -> None:
    # when & then
    """이미 cards 키만 있으면 변환·저장 없이 그대로 반환."""
    from srtgo.bot import storage

    data = {
        "srt": None,
        "ktx": None,
        "cards": [{"id": "x1y2", "label": None, "number": "n", "password": "p", "birthday": "b", "expire": "e"}],
    }
    storage.save(1, data)
    assert storage.load(1) == data


def test_load_prefers_cards_when_both_present(tmp_user_dir, fernet_key, caplog) -> None:
    # when & then
    """legacy card와 cards가 동시 존재할 때 cards를 우선하고 card를 무시한다."""
    import logging

    from srtgo.bot import storage

    mixed = {
        "srt": None,
        "ktx": None,
        "card": {"number": "ignored", "password": "x", "birthday": "x", "expire": "x"},
        "cards": [
            {"id": "aa11", "label": "kept", "number": "kept_num", "password": "p", "birthday": "b", "expire": "e"}
        ],
    }
    storage.save(1, mixed)

    with caplog.at_level(logging.WARNING):
        loaded = storage.load(1)

    assert loaded is not None
    assert "card" not in loaded
    assert loaded["cards"][0]["number"] == "kept_num"
    assert any(r.levelno == logging.WARNING and "card" in r.message for r in caplog.records)


def test_add_card_appends_with_id(tmp_user_dir, fernet_key, monkeypatch) -> None:
    # when & then
    from srtgo.bot import storage

    storage.save(1, {"srt": None, "ktx": None, "cards": []})
    monkeypatch.setattr(storage.secrets, "token_hex", lambda n: "ab12")

    fields = {"number": "1111", "password": "12", "birthday": "900101", "expire": "1230"}
    new_id = storage.add_card(1, fields, label="신한")

    assert new_id == "ab12"
    cards = storage.list_cards(1)
    assert cards == [
        {"id": "ab12", "label": "신한", "number": "1111", "password": "12", "birthday": "900101", "expire": "1230"}
    ]


def test_add_card_id_collision_retries(tmp_user_dir, fernet_key, monkeypatch) -> None:
    # when & then
    from srtgo.bot import storage

    storage.save(
        1,
        {
            "srt": None,
            "ktx": None,
            "cards": [{"id": "ab12", "label": None, "number": "x", "password": "x", "birthday": "x", "expire": "x"}],
        },
    )

    seq = iter(["ab12", "ab12", "cd34"])  # 충돌 두 번 후 통과
    monkeypatch.setattr(storage.secrets, "token_hex", lambda n: next(seq))

    new_id = storage.add_card(1, {"number": "n", "password": "p", "birthday": "b", "expire": "e"}, label=None)
    assert new_id == "cd34"
    assert {c["id"] for c in storage.list_cards(1)} == {"ab12", "cd34"}


def test_remove_card_returns_true_when_present(tmp_user_dir, fernet_key) -> None:
    # when & then
    from srtgo.bot import storage

    storage.save(
        1,
        {
            "srt": None,
            "ktx": None,
            "cards": [{"id": "ab12", "label": None, "number": "n", "password": "p", "birthday": "b", "expire": "e"}],
        },
    )

    assert storage.remove_card(1, "ab12") is True
    assert storage.list_cards(1) == []


def test_remove_card_returns_false_when_absent(tmp_user_dir, fernet_key) -> None:
    # when & then
    from srtgo.bot import storage

    storage.save(1, {"srt": None, "ktx": None, "cards": []})
    assert storage.remove_card(1, "nope") is False


def test_get_card_returns_card_or_none(tmp_user_dir, fernet_key) -> None:
    # when & then
    from srtgo.bot import storage

    card = {"id": "ab12", "label": None, "number": "n", "password": "p", "birthday": "b", "expire": "e"}
    storage.save(1, {"srt": None, "ktx": None, "cards": [card]})

    assert storage.get_card(1, "ab12") == card
    assert storage.get_card(1, "nope") is None


def test_list_cards_on_user_without_file_returns_empty(tmp_user_dir, fernet_key) -> None:
    # when & then
    from srtgo.bot import storage

    assert storage.list_cards(999) == []


def test_legacy_user_first_load_writes_new_format_to_disk(tmp_user_dir, fernet_key, monkeypatch) -> None:
    # when & then
    """디스크에 legacy 파일이 있던 사용자가 첫 load 후 디스크가 새 포맷으로 갱신된다."""
    from srtgo.bot import storage

    # 다른 테스트에서 캐시된 cipher가 남아있을 수 있으므로 현재 env 키로 재생성
    storage._reset_cipher_for_tests()

    legacy = {
        "srt": {"id": "u", "pw": "p"},
        "ktx": None,
        "card": {"number": "1111222233334444", "password": "12", "birthday": "900101", "expire": "1230"},
    }
    storage.save(42, legacy)

    monkeypatch.setattr(storage.secrets, "token_hex", lambda n: "ab12")
    storage.load(42)  # 첫 load — 마이그레이션 발동

    # 디스크 파일을 raw로 다시 디크립트해 검증 (load 호출 없이)
    import json
    import os

    from cryptography.fernet import Fernet

    cipher = Fernet(os.environ["BOT_DB_KEY"].encode())
    raw = (tmp_user_dir / "42.json.enc").read_bytes()
    on_disk = json.loads(cipher.decrypt(raw).decode())

    assert "card" not in on_disk
    assert on_disk["cards"][0]["id"] == "ab12"
    assert on_disk["cards"][0]["label"] is None
    assert on_disk["cards"][0]["number"] == "1111222233334444"
