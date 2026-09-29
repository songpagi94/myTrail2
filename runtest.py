"""로그인 후 열차를 한 번 조회합니다. 예약과 결제는 하지 않습니다."""

from __future__ import annotations

from datetime import datetime
from getpass import getpass

from pykorail import AdultPassenger, Korail, NoResultsError, PykorailError

DEPARTURE_STATION = "울산"
ARRIVAL_STATION = "서울"
# 서버의 로컬 시간대와 관계없이 라이브러리가 한국시간으로 해석합니다.
DEPART_AFTER = datetime(2026, 10, 3, 9, 0)


def main() -> int:
    """자격증명을 저장하지 않고 입력받아 조회 결과만 출력합니다."""
    print(
        f"조회 조건: {DEPARTURE_STATION} → {ARRIVAL_STATION}, {DEPART_AFTER:%Y-%m-%d %H:%M} 이후 (한국시간), 성인 1명"
    )
    print("실제 코레일 서버에 로그인하고 열차를 조회합니다. 예약·결제와 자동 재시도는 하지 않습니다.")
    try:
        if input("진행할까요? [y/N]: ").strip().lower() != "y":
            print("취소했습니다. 서버에 요청하지 않았습니다.")
            return 0

        korail_id = input("코레일 회원번호 / 이메일 / 휴대폰 번호(하이픈 포함): ").strip()
        korail_pw = getpass("코레일 비밀번호 (화면에 표시되지 않습니다): ")
        if not korail_id or not korail_pw:
            print("아이디와 비밀번호를 모두 입력해야 합니다. 서버에 요청하지 않았습니다.")
            return 1

        with Korail.logged_in(korail_id, korail_pw) as korail:
            print("로그인 성공. 열차를 조회합니다.")
            trains = korail.trains.search(
                DEPARTURE_STATION,
                ARRIVAL_STATION,
                depart_after=DEPART_AFTER,
                passengers=[AdultPassenger(1)],
            )
            print(f"조회 결과: {len(trains)}건")
            for train in trains:
                print(train)
    except NoResultsError:
        print("조건에 맞는 예약 가능 열차가 없습니다. 매진 열차는 조회 결과에서 제외됩니다.")
    except PykorailError as exc:
        # 서버 응답이나 예외에 계정 정보가 섞일 수 있어 원문은 출력하지 않습니다.
        print(f"로그인 또는 조회 실패 ({type(exc).__name__}). 입력 정보와 조회 조건을 확인하세요.")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\n입력이 중단되어 종료합니다.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
