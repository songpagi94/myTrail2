"""설정 등록과 봇 실행 CLI. import만으로 파일·네트워크를 사용하지 않습니다."""

from __future__ import annotations

import argparse
import logging
import warnings
from getpass import GetPassWarning, getpass
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet
from dotenv import load_dotenv

from pykorail import Card
from pykorail_bot.config import Settings
from pykorail_bot.domain import BotError
from pykorail_bot.handlers import build_application
from pykorail_bot.storage import Credential, Store

if TYPE_CHECKING:
    from collections.abc import Sequence


def hidden(prompt: str) -> str:
    """입력 숨김이 불가능한 터미널에서는 평문 입력으로 폴백하지 않습니다."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass(prompt)
        except GetPassWarning:
            raise BotError("입력 숨김을 지원하는 서버 터미널에서 실행하세요.") from None


def setup(store: Store, settings: Settings, owner: int) -> None:
    if owner not in settings.allowed:
        raise BotError("BOT_ALLOWED_IDS에 등록된 사용자만 설정할 수 있습니다.")
    previous = store.credential(owner) if store.has_credential(owner) else None
    if previous is not None and store.active(previous.account) is not None:
        raise BotError("확인 대기 중인 예약이 있습니다. 먼저 앱과 봇에서 처리완료를 확인하세요.")
    membership = hidden("코레일 회원번호 10자리: ").strip()
    password = hidden("코레일 비밀번호: ")
    card = None
    if input("수동 결제용 카드 한 장을 등록할까요? [y/N]: ").strip().lower() == "y":
        card = Card(
            number=hidden("카드번호: ").replace(" ", "").replace("-", ""),
            password=hidden("카드 비밀번호 앞 2자리: "),
            verify_number=hidden("생년월일 6자리 또는 사업자등록번호 10자리: "),
            expire=hidden("유효기간 YYMM: "),
            is_corporate=input("법인카드인가요? [y/N]: ").strip().lower() == "y",
        )
        if (
            not card.number.isascii()
            or not card.number.isdigit()
            or not 13 <= len(card.number) <= 19
            or len(card.password) != 2
            or not card.password.isdigit()
            or len(card.verify_number) != (10 if card.is_corporate else 6)
            or not card.verify_number.isdigit()
            or len(card.expire) != 4
            or not card.expire.isdigit()
            or not 1 <= int(card.expire[-2:]) <= 12
        ):
            raise BotError("카드 정보 형식을 확인하세요. 저장하지 않았습니다.")
    # 이미 검증한 라이브러리 기본 UA·서명을 바꾸지 않고 유지합니다.
    profile = previous.profile if previous is not None else "default"
    credential = Credential(membership, password, profile, card)
    if store.active(credential.account) is not None:
        raise BotError("해당 코레일 계정에 확인 대기 작업이 있습니다.")
    if input("입력한 정보로 등록/덮어쓸까요? [y/N]: ").strip().lower() != "y":
        print("저장하지 않았습니다.")
        return
    store.save_credential(owner, credential)
    print("암호화 저장 완료. 로그인·카드 유효성은 실제 요청으로 검사하지 않았습니다.")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="KTX 전용 텔레그램 봇")
    parser.add_argument("--env-file", default=".env", help="서버 환경변수 파일 (기본 .env)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="텔레그램 polling 시작")
    commands.add_parser("keygen", help="새 암호화 키 생성 (기존 키를 교체하지 마세요)")
    registration = commands.add_parser("setup", help="서버 터미널에서 자격증명 등록")
    registration.add_argument("--user", type=int, required=True, help="허용된 텔레그램 사용자 ID")
    args = parser.parse_args(argv)
    if args.command == "keygen":
        print(Fernet.generate_key().decode("ascii"))
        return
    load_dotenv(args.env_file, override=False)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # httpx INFO 로그에는 봇 토큰을 포함하는 URL이 들어갈 수 있습니다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = None
    try:
        settings = Settings.from_env()
        store = Store(settings.directory, settings.key)
        store.initialize()
        store.acquire()
        if args.command == "setup":
            setup(store, settings, args.user)
        else:
            store.recover()
            application = build_application(settings, store)
            # 서버 재시작 전 남은 결제/예약 버튼 이벤트를 자동 재실행하지 않습니다.
            application.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    except BotError as error:
        parser.exit(1, f"{error}\n")
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "입력을 취소했습니다.\n")
    except Exception as error:
        parser.exit(
            1, f"시작 또는 실행 실패 ({type(error).__name__}). 비밀정보 보호를 위해 원문은 출력하지 않습니다.\n"
        )
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
