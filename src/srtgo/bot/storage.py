"""사용자별 자격증명을 Fernet로 암호화하여 파일에 저장."""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from pykorail.device.android_id import generate_android_id, validate_android_id

logger = logging.getLogger(__name__)


class StorageDecryptError(Exception):
    """복호화 실패 (마스터 키 변경·파일 손상)."""


_cipher: Fernet | None = None


def _get_cipher() -> Fernet:
    global _cipher
    if _cipher is None:
        key = os.environ.get("BOT_DB_KEY")
        if not key:
            raise RuntimeError("BOT_DB_KEY 환경변수 미설정")
        _cipher = Fernet(key.encode() if isinstance(key, str) else key)
    return _cipher


def _reset_cipher_for_tests() -> None:
    """테스트 전용 — env 바뀐 후 cipher 재생성."""
    global _cipher
    _cipher = None


def _users_dir() -> Path:
    d = Path(os.environ.get("BOT_USERS_DIR", "data/users"))
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _path(telegram_id: int) -> Path:
    if telegram_id <= 0:
        raise ValueError("올바른 사용자 ID가 필요합니다.")
    return _users_dir() / f"{telegram_id}.json.enc"


def exists(telegram_id: int) -> bool:
    return _path(telegram_id).exists()


def save(telegram_id: int, data: dict) -> None:
    # 계정 재등록 때도 기존 기기 신원을 유지합니다.
    data = dict(data)
    previous = (
        json.loads(_get_cipher().decrypt(_path(telegram_id).read_bytes()).decode("utf-8"))
        if "android_id" not in data and exists(telegram_id)
        else None
    )
    if previous is not None and "android_id" in previous:
        data["android_id"] = previous["android_id"]
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    token = _get_cipher().encrypt(plaintext)
    target = _path(telegram_id)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(token)
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info("자격증명 저장: tid=%d", telegram_id)


def load(telegram_id: int) -> dict | None:
    p = _path(telegram_id)
    if not p.exists():
        return None
    token = p.read_bytes()
    try:
        plaintext = _get_cipher().decrypt(token)
    except InvalidToken as e:
        raise StorageDecryptError(str(e)) from e
    data = json.loads(plaintext.decode("utf-8"))

    if _migrate_in_place(data):
        try:
            save(telegram_id, data)
        except Exception as e:
            logger.warning("마이그레이션 디스크 저장 실패 tid=%d: %s", telegram_id, type(e).__name__)

    return data


def _migrate_in_place(data: dict) -> bool:
    """legacy `card` 키를 `cards` 리스트로 변환. 변경되었으면 True."""
    has_legacy = "card" in data
    has_new = "cards" in data and data["cards"] is not None

    if has_legacy and has_new:
        logger.warning("legacy card와 cards 동시 존재 — card 무시")
        del data["card"]
        return True

    if has_legacy and not has_new:
        legacy = data.pop("card")
        if legacy:
            new_card = {"id": _fresh_card_id(set()), "label": None, **legacy}
            data["cards"] = [new_card]
        else:
            data["cards"] = []
        return True

    return False


def _fresh_card_id(existing_ids: set[str]) -> str:
    """4 hex id 생성 — 충돌 시 재추첨 (최대 10회). 실패 시 RuntimeError."""
    for _ in range(10):
        candidate = secrets.token_hex(2)
        if candidate not in existing_ids:
            return candidate
    raise RuntimeError("카드 ID 생성 충돌 한도 초과")


def android_id_for(telegram_id: int) -> str:
    """사용자별 ID를 한 번 저장하고 재로그인 때 복원합니다."""
    data = load(telegram_id) or {"ktx": None, "cards": []}
    if "android_id" in data:
        return validate_android_id(data["android_id"])
    android_id = generate_android_id()
    data["android_id"] = android_id
    # 저장 실패 시 로그인하지 않아 다음 실행에서 신원이 바뀌는 것을 막습니다.
    save(telegram_id, data)
    return android_id


def delete(telegram_id: int) -> None:
    p = _path(telegram_id)
    if p.exists():
        p.unlink()
        logger.info("자격증명 삭제: tid=%d", telegram_id)


def list_user_ids() -> list[int]:
    out = []
    for p in _users_dir().iterdir():
        if p.suffix == ".enc" and p.stem.endswith(".json"):
            try:
                out.append(int(p.stem.removesuffix(".json")))
            except ValueError:
                continue
    return out


def list_cards(telegram_id: int) -> list[dict]:
    data = load(telegram_id)
    if data is None:
        return []
    return list(data.get("cards", []))


def get_card(telegram_id: int, card_id: str) -> dict | None:
    for card in list_cards(telegram_id):
        if card["id"] == card_id:
            return card
    return None


def add_card(telegram_id: int, fields: dict, label: str | None) -> str:
    # Seed default record if user has no file yet — must stay in sync with /setup writer
    data = load(telegram_id) or {"ktx": None, "cards": []}
    existing_ids = {c["id"] for c in data["cards"]}
    new_id = _fresh_card_id(existing_ids)
    data["cards"].append({"id": new_id, "label": label, **fields})
    save(telegram_id, data)
    return new_id


def remove_card(telegram_id: int, card_id: str) -> bool:
    data = load(telegram_id)
    if data is None:
        return False
    cards = data.get("cards", [])
    new_cards = [c for c in cards if c["id"] != card_id]
    if len(new_cards) == len(cards):
        return False
    data["cards"] = new_cards
    save(telegram_id, data)
    return True
