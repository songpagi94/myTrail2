# KTX 전용 텔레그램 봇

코레일 통신은 기존 `pykorail`만 사용하고, myTrail의 고정 형식 입력·열차 선택·페이지·알림 UX를
별도 `pykorail_bot` 패키지로 구현했습니다. SRT API, 프록시 우회 코드, LLM은 포함하지 않습니다.

## 제공하는 기능과 범위

- 매진 포함 KTX 조회, 좌석 등급, 성인·어린이·경로 인원 지정.
- 열차 한 편 또는 현재 페이지 중 한 편을 선택하고 **시작 확인** 후 반복 조회·예약.
- 날짜·번호·구간·시각으로 열차를 식별하여 결과 순서가 바뀌어도 다른 열차를 예약하지 않음.
- 기본 30초 간격, 기본 최대 120분. 선택 열차의 출발 시각이 지나면 종료.
- 예약 성공 알림과 결제 기한 안내. **자동 결제와 코레일 예약대기 자동 등록은 하지 않음.**
- 선택적으로 등록 카드 한 장으로 **금액 확인 후 수동 결제**. 기본 비활성화.
- `/stop`은 반복 시도만 중단. 이미 전송한 요청은 결과를 확인하며 확보한 예약을 취소하지 않음.
- 재시작하면 반복 시도는 중단 상태, 진행 중이던 예약·결제는 확인 대기 상태로 복구.
- 예약/결제 결과가 불확실하면 자동 재전송하지 않음. `/check`와 코레일 앱에서 확인.

현재 버전에는 다중 카드 선택, 자동결제, 환불, 봇에서의 실제 예약 취소가 없습니다.
예약 취소·환불은 코레일 앱에서 처리하세요. 먼저 본인 한 명의 계정으로 검증하세요.
요청 간격은 보장된 허용 한도가 아닙니다. 서비스 약관을 지키고 차단·오류가 지속되면 중단하세요.

## 1. 설치 — 서버의 songpagi 계정

PC의 변경을 GitHub에 push한 뒤 서버에서 실행합니다. 이미 봇이 실행 중이면 **먼저 중단**하세요.

```bash
cd ~/apps/myTrail2
git status --short
git pull --ff-only origin main
uv sync --locked --all-extras
uv run --locked --all-extras ruff format --check
uv run --locked --all-extras ruff check
uv run --locked --all-extras ty check
uv run --locked --all-extras pytest
```

`git status`에 서버에서 수정한 파일이 나오면 강제로 덮어쓰지 말고 PC의 변경과 먼저 정리하세요.
라이브러리만 설치하는 사용자는 `bot` extra가 필요 없습니다. 봇만 운영하는 환경은
`uv sync --locked --extra bot --no-dev`로 설치할 수 있습니다.

## 2. 비밀 설정

BotFather에서 봇 토큰을 발급받고 본인의 텔레그램 숫자 사용자 ID를 준비합니다.
본인 토큰과 암호화 키를 채팅·Git·로그에 올리지 마세요.

```bash
umask 077
cp -n .env.example .env
uv run --locked --extra bot pykorail-bot keygen
nano .env
chmod 600 .env
```

- `BOT_TOKEN`: BotFather 토큰.
- `BOT_DB_KEY`: 위에서 생성한 Fernet 키. 데이터가 생긴 뒤 새 키로 바꾸면 복호화할 수 없습니다.
- `BOT_ALLOWED_IDS`: 본인 숫자 ID. 여러 명이면 쉼표 구분. 비어 있으면 시작하지 않습니다.
- `BOT_DATA_DIR`: 전용 데이터 디렉터리. 기본 `bot-data`. 다른 파일이 있는 공용 폴더는 지정하지 마세요.
- `BOT_POLL_SECONDS`: 10~3600초, 기본 30. 조회 오류 시 간격을 늘리고 연속 3회면 중단합니다.
- `BOT_MAX_MINUTES`: 1~1440분, 기본 120.
- `BOT_ENABLE_PAYMENTS`: `false` 유지 권장. `true`일 때도 자동 결제는 하지 않습니다.
- `BOT_MAX_PAYMENT_WON`: 봇 수동결제를 활성화할 때 양수 한도를 반드시 지정합니다.

`.env`는 실행 시 읽으며 systemd 등에서 이미 설정한 환경변수를 덮어쓰지 않습니다.
별도 경로는 `pykorail-bot --env-file /경로/설정파일 run`으로 지정합니다.

## 3. 코레일 계정 등록

봇이 중단된 상태에서 숫자 ID를 실제 본인 ID로 바꿔 실행합니다.

```bash
uv run --locked --extra bot pykorail-bot setup --user 123456789
```

서버 터미널에서 **회원번호 10자리**, 비밀번호를 입력합니다. 입력 내용은 보이지 않습니다.
같은 계정을 이메일/휴대폰 번호로 중복 등록하지 않도록 회원번호만 사용합니다.
카드 등록은 건너뛸 수 있습니다. 등록 시 카드 한 장을 암호화 저장하며 재등록은 기존 정보를 교체합니다.
조회·로그인 확인이나 카드 승인 요청은 이 명령에서 보내지 않습니다.

텔레그램에서는 비밀번호·카드번호를 받지 않습니다. `/setup`도 서버 등록 방법만 안내합니다.
저장소와 키가 함께 유출되면 복호화가 가능하므로 암호화만으로 충분한 보호가 되지는 않습니다.
`.env`와 `bot-data/`는 버전 관리에서 제외되며, 접근 권한과 암호화된 별도 백업을 관리하세요.

## 4. 처음 실행

```bash
uv run --locked --extra bot pykorail-bot run
```

이 명령은 실제 텔레그램에 연결합니다. 실행·로그인·예약·결제를 대신 수행해 검증한 것이 아닙니다.
본인의 봇과 개인 대화를 열고 `/start`를 보냅니다. **그룹 채팅에서는 동작하지 않습니다.**

```text
부산 서울 20261003 0900 일반우선
서울 부산 20261003 1400 특실만 성인2 어린이1
```

예시 날짜는 실제 미래 날짜로 바꾸세요. 모든 시각은 한국시간입니다.
역 이름은 서버 역 목록과 정확히 일치해야 하며, 임의의 별칭을 추측해 다른 역으로 변경하지 않습니다.
열차 목록 → 열차/페이지 선택 → **예약 시도 시작**을 누르면 실제 예약을 시도합니다.
동일 계정의 미결제·불확실한 예약이 있으면 새 작업을 막습니다.

- `/status`: 최근 작업과 처리 버튼. 알림 전송이 실패했어도 이곳에 결과가 남습니다.
- `/check 작업ID`: 예약 번호가 있는 건의 예약·승차권을 읽어서 확인합니다. 새 예약/결제를 보내지 않습니다.
- `/stop`, `/cancel`: 진행 중인 반복 시도만 중단합니다. **예약 취소나 환불이 아닙니다.**
- **앱에서 처리완료 확인**: 코레일 앱에서 해당 예약·결제 상태를 직접 확인하고 필요한 처리를 마친 뒤 누릅니다.
  이 버튼은 봇의 확인 대기를 해제할 뿐입니다. 예약 식별번호를 못 받은 불확실한 요청도 앱에서 확인해야 합니다.

봇 수동 결제를 켠 경우 **수동 결제 확인 → 카드 끝 4자리·금액 확인 → 실제 결제**의 두 단계를 거칩니다.
확인은 60초 동안 한 번만 유효하며 금액·카드가 바뀌면 거부합니다.
응답이 끊기거나 발권이 확인되지 않으면 `UNKNOWN`으로 남고 자동 재결제하지 않습니다.
코레일 앱의 상태가 최종 기준입니다.

## 5. systemd 상시 실행 — 수동 검증 후

먼저 PuTTY에서 실행한 봇을 Ctrl+C로 종료합니다. 같은 데이터 폴더에서는 이중 실행을 막습니다.
`docs/pykorail-bot.service`를 검토하여 경로를 맞춘 뒤 설치합니다.

```bash
sudo cp docs/pykorail-bot.service /etc/systemd/system/pykorail-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now pykorail-bot
sudo systemctl status pykorail-bot
journalctl -u pykorail-bot -n 50 --no-pager
```

텔레그램 long polling을 사용하므로 새 인바운드 포트는 필요 없습니다.
재시작 시 과거 텔레그램 이벤트는 버리고 예약·결제 쓰기를 자동 재개하지 않습니다.
`/status`로 상태를 확인하고 필요할 때 새 요청을 보내세요.

## 6. 이후 업데이트

```bash
sudo systemctl stop pykorail-bot
cd ~/apps/myTrail2
git status --short
git pull --ff-only origin main
uv sync --locked --all-extras
uv run --locked --all-extras ruff format --check
uv run --locked --all-extras ruff check
uv run --locked --all-extras ty check
uv run --locked --all-extras pytest
sudo systemctl start pykorail-bot
```

중간에 오류가 나면 다음 명령으로 넘어가지 마세요. 업데이트 때문에 `.env`나 `bot-data`를 삭제하지 마세요.
PC와 서버에서 같은 파일을 각각 수정하지 말고 PC에서 수정·검증·push하는 흐름을 유지하세요.

## 테스트와 알려진 한계

자동 테스트는 가짜 HTTP 세션·텔레그램 객체를 사용합니다. 실제 예약/결제 성공을 보장하지 않습니다.
재시도·결제·알림의 실패를 분리하며 동일 세션은 한 전용 스레드에서 사용합니다.
네트워크 요청을 보낸 뒤 Ctrl+C나 /stop을 눌러도 원격 요청 자체를 되돌릴 수 없습니다.
요청 반환까지 기다리고, 프로세스가 강제 종료되면 저장된 중간 상태를 다음 시작에서 확인 대기로 바꿉니다.
예약 번호 없이 끊긴 요청은 안전하게 자동 확정할 근거가 없으므로 앱 확인이 필수입니다.
