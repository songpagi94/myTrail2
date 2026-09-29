# Python 라이브러리 빠른 시작

파이썬 3.10 이상이 필요합니다. 저장소에서 환경을 구성합니다.

```bash
uv sync --locked --all-extras
```

아래는 코레일 서버에 실제 로그인·조회하는 예시입니다. 실행 날짜와 자격증명은
본인 환경에 맞게 준비하고 Git이나 로그에 비밀번호를 남기지 마세요.

```python
from datetime import datetime
from getpass import getpass
from pykorail import Korail

with Korail.logged_in(input("코레일 ID: "), getpass("비밀번호: ")) as korail:
    trains = korail.trains.search("서울", "부산", depart_after=datetime(2026, 10, 3, 9))
    print(trains)
```

전체 API는 [API 레퍼런스](reference.md), myTrail 기반 봇의 실행과 사용자 등록은
[텔레그램 봇 가이드](telegram-bot.md)를 참고하세요.

코레일과 무관한 비공식 클라이언트이며 실제 좌석 확보를 보장하지 않습니다.
