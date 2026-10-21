# Minecraft → KMB 자동 기록 연결

## 전달받은 심판 스크립트 적용본

`minecraft-skript/시스템_심판_웹연동.sk`는 전달받은 `시스템_심판.sk`에 실제 연동 위치를 적용한 사본입니다. `/fin`에서 `{game.away.team}`, `{game.home.team}`, `{game.away.score}`, `{game.home.score}`를 백업한 직후 `/minecraft/league/result`로 전송합니다. 원본은 변경하지 않았습니다.

적용 전 파일 상단 `options`의 두 값만 바꾸세요.

```vb
kmb-api: https://Railway에서-생성한-주소.up.railway.app
kmb-key: Railway의 MINECRAFT_WEBHOOK_SECRET와 같은 값
```

서버에는 Skript와 함께 `skript-io` 애드온이 필요합니다. 전달된 파일에는 일정 생성 명령 자체가 없고 이미 만들어진 `{schedule.data::*}`를 읽는 코드만 있으므로, 이번 적용본은 `/fin` 경기 결과 자동 기록까지 연결했습니다. 일정 등록 스크립트를 추가로 받으면 일정 API도 같은 방식으로 연결할 수 있습니다.

마인크래프트의 Skript 명령어가 실행될 때 `bot.py`의 인증 웹훅으로 HTTP POST를 보내면, 웹/Discord와 같은 Firestore `leagueGames`에 즉시 기록됩니다. 경기 결과가 들어오면 웹의 팀 승·패·무, 승률, 득점, 실점도 자동 갱신됩니다.

## 1. 봇 서버 환경변수

다음 값을 봇 호스팅 서비스의 환경변수에 추가합니다.

```env
MINECRAFT_WEBHOOK_SECRET=충분히_길고_추측하기_어려운_문자열
```

`PORT`는 Render/Railway 등의 호스팅 서비스가 자동으로 제공합니다. 직접 실행할 때는 기본값 `8080`을 사용합니다. 키는 Skript와 봇 서버에만 넣고 웹페이지나 GitHub에는 올리지 마세요.

## 2. 리그 일정 기록

요청 주소:

```text
POST https://봇주소/minecraft/league/schedule
X-KMB-Key: 환경변수와 같은 키
Content-Type: application/json
```

본문:

```json
{
  "homeTeam": "PBG",
  "awayTeam": "DSP",
  "stadium": "KMB 구장",
  "homeStarter": "HaeSol",
  "awayStarter": "AwayPitcher",
  "gameDateTime": "2026-09-01 22:00",
  "server": "KMB Minecraft"
}
```

## 3. 경기 결과 기록

요청 주소:

```text
POST https://봇주소/minecraft/league/result
```

헤더는 일정 요청과 같습니다.

```json
{
  "homeTeam": "PBG",
  "awayTeam": "DSP",
  "homeScore": 5,
  "awayScore": 3,
  "winningPitcher": "HaeSol",
  "losingPitcher": "AwayPitcher",
  "holdPitcher": "",
  "savePitcher": "Closer",
  "gameDateTime": "2026-09-01 22:00",
  "server": "KMB Minecraft"
}
```

같은 홈팀·어웨이팀·경기 일시의 일정이 이미 있으면 해당 문서를 경기 결과로 갱신합니다. 없으면 결과 문서를 새로 만듭니다.

## 4. Skript 연결 위치

사용 중인 HTTP 애드온의 POST 문법은 애드온마다 다릅니다. 기존 경기 명령어의 모든 검증과 점수 계산이 끝난 직후 다음 동작을 넣습니다.

```text
command /리그결과 <text> <text> <number> <number> ...:
    trigger:
        # 기존 경기 종료 처리
        # 마지막에 HTTP 애드온으로 /minecraft/league/result에 위 JSON을 POST
        # X-KMB-Key 헤더에 비밀 키 지정
        # HTTP 200이고 응답의 ok=true이면 "웹 기록 완료" 출력
```

Skript 코드에서 팀 코드, 점수, 투수, 경기 일시 변수를 JSON 필드에 그대로 매핑하면 됩니다. 요청 실패 시 경기 명령 자체를 취소하기보다는 관리자에게 재전송 안내를 띄우는 방식을 권장합니다.

## 5. 연결 확인

```text
GET https://봇주소/health
```

`{"ok": true}`가 반환되면 수신 서버가 실행 중입니다. `401 invalid webhook key`는 키가 다르거나 헤더가 빠졌다는 뜻입니다.
