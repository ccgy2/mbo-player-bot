# KMB League Discord 봇 — 선수 멤버십 자동 동기화

이 폴더의 `bot.py`를 실행합니다. 웹사이트와 같은 Firebase Firestore를 사용하며, Discord 서버에서 나간 선수를 현재 선수 목록에서 자동 제외합니다.

## 이번 수정의 동작

| 상황 | 처리 |
| --- | --- |
| 지정된 선수 등록 메시지에 반응 | 기존 자동 등록 유지. Discord 사용자 ID와 서버 ID를 선수 문서에 연결 |
| 등록 역할을 받음 | `DISCORD_ROSTER_ROLE_IDS`를 설정한 경우 자동 등록 |
| 서버 나가기·추방·차단 | 연결된 선수의 `rosterStatus`를 `left_guild`로 변경 |
| 웹 선수 목록·로스터·봇 로스터·FA/트레이드 대상 | `left_guild` 상태의 선수 제외 |
| 기존 팀·포지션·등번호·이적 정보·처벌 기록 | 보존 |
| 서버 재입장만 함 | 제외 상태 유지 |
| 재입장 후 등록 메시지에 다시 반응하거나 등록 역할을 받음 | 기존 선수 문서를 복귀 처리. 팀/포지션/등번호는 유지 |
| 봇이 꺼져 있는 동안 탈퇴 | 시작 후 및 10분마다 서버 소속을 다시 확인해 누락 보정 |
| 전체 멤버 조회 실패·빈 응답·권한 부족·서버 장애 | 탈퇴로 단정하지 않고 보류 |

탈퇴 처리는 문서 삭제가 아닙니다. 현재 목록에서 숨기고 기록을 보존하므로, 같은 Discord 계정이 정상 재등록하면 기존 선수 정보에 다시 연결됩니다. 역할을 잃거나 메시지 반응을 지우는 것만으로는 선수 기록을 제외하지 않습니다.

첨부된 옛 `bot.zip`은 반응 등록 기능이 없는 이전 Python 봇이었습니다. 이번 봇은 웹 프로젝트에 들어 있던 더 최신 Python 버전을 기준으로 반응 등록, 슬래시 명령어, 팀 관리, 경기 일정/결과, Minecraft 웹훅을 함께 보존했습니다. 옛 `!` 명령어 26개의 이름과 별칭을 유지하고 `!선수연결`을 추가했습니다.

## 적용 순서

1. 현재 실행 중인 봇을 중지합니다. 같은 토큰/DB로 옛 봇과 새 봇을 동시에 실행하지 마세요.
2. 이 폴더의 파일로 봇 코드를 교체합니다. 기존 `.env` 또는 Railway Variables의 토큰과 Firebase 설정은 그대로 재사용합니다.
3. 권장 설정: `DISCORD_ROSTER_GUILD_ID`에 선수 관리 Discord **서버 ID**를 넣습니다.
4. Discord Developer Portal → 해당 애플리케이션 → Bot → Privileged Gateway Intents에서 **SERVER MEMBERS INTENT**와 **MESSAGE CONTENT INTENT**를 켭니다.
5. 아래 명령으로 패키지를 설치한 뒤 봇을 시작합니다.
6. 함께 제공된 웹사이트도 배포합니다. 기존 웹은 새 `rosterStatus` 필터를 아직 적용하지 않아 탈퇴 선수가 계속 보일 수 있습니다.

```powershell
python -m pip install -r requirements.txt
python bot.py
```

새 설치라면 `.env.example`을 `.env`로 복사하고 빈 설정을 채웁니다.

```powershell
Copy-Item .env.example .env
```

기존 실행 환경의 Python 3.11 설정, `Procfile`의 `worker: python bot.py`, Railway 설정 파일은 유지했습니다. 최신 Python 기능에 필요한 `aiohttp`가 requirements에 포함되어 있습니다.

### Railway

기존 서비스의 Variables를 재사용하고 필요한 서버 ID/등록 역할 ID만 추가한 뒤 재배포합니다. 시작 명령은 `python bot.py`입니다. 봇 역할은 1개 인스턴스로 실행하세요. Python 코드가 들어 있는 **이 봇 폴더**를 배포하며, 웹의 `public` 폴더를 봇 실행 경로로 쓰지 않습니다.

Minecraft HTTP 웹훅을 사용한다면 `MINECRAFT_WEBHOOK_SECRET`과 공개 HTTP 도메인/포트를 설정하고 서비스가 HTTP 요청을 받을 수 있게 운영합니다. 서버가 제공하는 `PORT`를 우선 사용합니다. Heroku처럼 프로세스 유형을 구분하는 호스팅에서 HTTP 웹훅을 노출할 때에는 `web: python bot.py` 유형이 필요하며, 별도 worker로 같은 봇을 중복 실행하지 않습니다. Minecraft를 쓰지 않으면 해당 설정은 비워 둡니다.

## 환경변수

| 변수 | 용도 |
| --- | --- |
| `DISCORD_TOKEN` | 기존 봇 토큰. 필수 |
| `FIREBASE_PROJECT_ID` | 기존 Firebase 프로젝트 ID. 필수 |
| `FIREBASE_CLIENT_EMAIL` | 기존 Firebase 서비스 계정 이메일. 필수 |
| `FIREBASE_PRIVATE_KEY` | 기존 Firebase 서비스 계정 비공개 키. 필수. `\n` 개행 유지 |
| `DISCORD_ROSTER_GUILD_ID` | 선수 관리 서버 ID. 설정 권장. 봇이 여러 서버에 있으면 명확한 범위 설정 필요 |
| `DISCORD_REACTION_ROSTER_MESSAGE_ID` | 원래 자동 등록 메시지 ID. 기존 기본값 `1515707426421084321` 유지. 빈 값이면 반응 자동 등록 비활성 |
| `DISCORD_REACTION_ROSTER_CHANNEL_ID` | 해당 메시지가 있는 채널 ID. 기존 값이 있으면 유지 |
| `DISCORD_REACTION_ROSTER_TEAM` | 신규 자동 등록 기본 팀. 기본 `무소속`. 기존 등록 선수의 소속은 덮어쓰지 않음 |
| `DISCORD_ROSTER_ROLE_IDS` | **선택**. 선수를 등록하는 역할 ID를 쉼표로 구분. 비워 두면 역할로 등록하지 않음 |
| `DISCORD_ROSTER_ROLE_ID` | 위 변수 대신 역할 1개를 지정할 때의 호환 이름 |
| `DISCORD_CHANNEL_ID` | 기존 일반 공지 채널 |
| `DISCORD_LOG_CHANNEL_ID` | 기존 운영 로그 채널 |
| `MINECRAFT_WEBHOOK_SECRET` | 선택. 기존 Minecraft 웹훅 인증값 |
| `MINECRAFT_WEBHOOK_PORT` / `PORT` | 선택. HTTP 웹훅 수신 포트. `PORT` 우선 |

역할 ID 예시에서 **팀 역할을 모두 넣을 필요는 없습니다**. 등록 채널에서 사용자가 받는 선수 자격 역할만 넣으면 됩니다. 역할 자체를 부여하는 기존 반응 역할 봇/Discord 설정은 그대로 사용합니다. 이 봇의 기존 팀 역할 동기화 설정과 새 선수 등록 역할 설정은 별개입니다.

역할을 새로 설정하지 않아도 원래의 **지정 메시지 반응 → 선수 등록** 흐름과 **서버 탈퇴 → 현재 목록 제외**가 작동합니다.

## 관리 서버와 기존 데이터 연결

자동 동기화 대상 서버는 다음 순서로 결정합니다.

1. `DISCORD_ROSTER_GUILD_ID` 또는 기존 Firestore `appMeta/discordBot.rosterGuildId`.
2. 설정된 반응 등록 채널이 속한 서버.
3. 위 설정이 없고 봇이 정확히 서버 1개에만 속해 있으면 그 서버.

서버 ID와 반응 채널의 서버가 다르거나, 여러 서버 중 어느 서버인지 확정할 수 없으면 자동 동기화를 보류하고 실행 로그에 이유를 남깁니다. 다른 서버에 명시적으로 연결된 선수는 변경하지 않습니다.

기존 `discordId`만 있고 서버 ID가 없는 반응 등록 선수는 확정된 관리 서버 범위에서 기존 ID로 연결합니다. 같은 ID가 이미 다른 서버에 연결되어 있어 모호한 데이터는 자동 변경하지 않습니다.

**Discord ID가 없는 수동 선수는 이름만 보고 탈퇴 처리하지 않습니다.** 등록 메시지 반응/역할 등록 과정에서 같은 이름·같은 팀의 기존 선수 1명이 분명하게 일치하면 연결합니다. 다른 팀의 동명이인, 이미 다른 Discord 계정에 연결된 이름, 여러 후보는 보류합니다.

현재 Discord 별명과 기존 선수명이 다르거나 기본 팀이 `무소속`이라 기존 팀 선수와 자동 연결되지 않는다면 관리자가 정확한 선수를 한 번 연결할 수 있습니다.

```text
!선수연결 @서버멤버 "기존선수명" PBG
```

이 명령은 기존 관리자 권한 검사를 적용하고, 다른 계정의 연결을 덮어쓰거나 소속팀을 바꾸지 않습니다. 선수명도 그대로 둡니다. 이미 탈퇴 제외된 선수의 자격을 이 명령만으로 복귀시키지는 않습니다. 연결 이후 다시 등록 메시지에 반응하거나 등록 역할을 받으면 기존 기록에 복귀합니다.

Discord 별명 변경 자동 반영은 기존 기능을 유지합니다. 새 별명이 다른 선수 이름과 충돌하면 선수명을 유지하고 확인 필요 상태를 기록합니다.

## 재시작과 재등록 정책

- 캐시가 없는 탈퇴도 `on_raw_member_remove`로 처리합니다.
- 시작 후/10분 정기 점검은 멤버 목록을 끝까지 읽은 다음 각 대상의 현재 서버 소속을 다시 확인합니다.
- 탈퇴 추론에는 Discord의 `Unknown Member` 응답(`404`, 코드 `10007`)만 사용합니다. `Unknown Guild`, 권한 오류, 제한, 네트워크 장애는 보류합니다.
- 재입장만 했거나 별명만 변경한 경우 `left_guild`를 풀지 않습니다.
- 시작 시 예전 메시지에 남아 있는 오래된 반응만으로 이미 탈퇴 제외된 선수를 복귀시키지 않습니다. 재입장 후 다시 반응을 달거나 등록 역할을 받아야 합니다. 관리자의 명시적 `/반응선수등록` 실행은 지정 메시지 현재 반응자를 다시 등록하는 작업으로 처리합니다.
- 등록/탈퇴 이벤트와 정기 점검이 겹치면 늦게 도착한 조회 결과가 최신 이벤트를 덮어쓰지 않도록 검사합니다.
- 같은 등록 이벤트가 반복되어도 새 문서를 계속 만들지 않습니다. 동일 Discord ID의 기존 중복 문서가 있다면 탈퇴 시 모두 제외하되, 모호한 중복을 자동 합치거나 지우지는 않습니다.

## Firestore 데이터 계약

기존 `players/{id}` 문서에 다음 필드만 보충합니다. 별도 컬렉션 이동이나 일괄 삭제 마이그레이션은 없습니다.

| 필드 | 의미 |
| --- | --- |
| `discordId` | 문자열 Discord 사용자 ID |
| `discordGuildId` | 문자열 선수 관리 서버 ID |
| `rosterStatus` | `active` 또는 `left_guild`. 필드가 없는 옛 수동 선수는 활성 취급 |
| `discordSyncVersion` | 현재 동기화 형식 `1` |
| `discordLeftAt` | 탈퇴 확인 시 Firestore 서버 타임스탬프 |
| `discordLeftReason` | 이벤트/정기 확인 등의 처리 이유 |
| `discordRejoinedAt` | 재등록으로 복귀한 시각 |
| `discordRegistrationSource` | `reaction` 또는 `role` |

`left_guild`와 정확히 일치할 때만 현재 목록에서 제외합니다. 보관 상태 필드가 없거나 알 수 없는 상태는 자동 제외하지 않습니다. 신규 활성 등록이나 정상 복귀 시 탈퇴 시각·사유를 해제합니다. `team`, `position`, `number`, `transfer`, `forcedReleaseOriginalTeam`, 이적 및 처벌 컬렉션은 탈퇴/복귀 때문에 삭제하지 않습니다.

관리자 웹 수동 등록으로 탈퇴 선수를 같은 이름·같은 팀의 새 선수로 중복 생성하지 않도록 웹과 봇에서 기존 보관 기록도 검사합니다. 이적/처벌 내역 조회는 보관 기록을 유지하며, 현재 선수 대상 조회는 활성 선수를 사용합니다.

## 오프라인 검증

```powershell
python -m unittest discover -s tests -v
python -m py_compile bot.py roster_sync.py
```

테스트는 가짜 Firestore·Discord 객체와 실제 배포 파일에서 추출한 함수/이벤트 핸들러를 사용합니다. `.env`를 읽거나 Firebase를 초기화하지 않고, 실제 Discord 로그인·메시지 전송·운영 DB 변경을 하지 않습니다. 가짜 저장소의 동기 함수를 인라인으로 실행하므로 호스팅의 실제 HTTP 연결, Firestore 자격 증명/규칙, Gateway 권한 및 스레드 실행은 통합 테스트 범위 밖입니다.

핵심 검증 범위: 캐시 없는 탈퇴, 중복 탈퇴 이벤트, 기존 기록 보존, 서버 범위, 이전 문자열/숫자 Discord ID, 동명이인/다른 팀/다른 계정 충돌, 반복 등록, 재입장과 재등록 구분, 재시작 중 누락, 역할 미설정/설정, 불완전 조회/권한 오류/잘못된 서버, 이벤트와 늦은 조회 경합, 웹과 공통 활성 로스터 조회 규칙.

운영 연결은 이 환경에서 실행하지 않았습니다. 배포 후 실제 등록 메시지·역할과 테스트 계정으로 등록 → 서버 탈퇴 → 웹 현재 목록 제외 → 재입장만으로는 미복귀 → 다시 등록 시 기존 정보 복귀를 확인하세요. 실제 호스팅 장애/사용자 연결 상황에 따라 실시간 반영이 늦으면 봇 로그와 10분 점검 결과를 확인합니다.

## 공식 동작 근거

- discord.py 2.4 `on_raw_member_remove`: https://discordpy.readthedocs.io/en/v2.4.0/api.html#discord.on_raw_member_remove
- discord.py 멤버 Intent와 멤버 조회: https://discordpy.readthedocs.io/en/v2.4.0/intents.html
- Discord Gateway / Privileged Intents: https://docs.discord.com/developers/events/gateway

토큰과 Firebase 서비스 계정 키는 `.env` 또는 호스팅 비밀 변수에서만 관리하고, 웹 `public`·Git 저장소·배포 ZIP에 넣지 않습니다.
