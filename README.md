# MBO 선수 관리 웹사이트

Firebase Firestore를 이용해 선수 이름, 팀 정보, 이동 정보, 구단 프런트 정보를 저장하고 조회하는 정적 웹앱입니다.

## 파일 구성

- `index.html`: 화면 구조
- `styles.css`: 반응형 스타일
- `app.js`: Firebase 연결, 저장, 수정, 삭제, 검색 로직

## Firebase 연결 방법

1. Firebase 콘솔에서 프로젝트를 만듭니다.
2. 웹 앱을 추가한 뒤 Firebase SDK 설정값을 복사합니다.
3. `app.js` 상단의 `firebaseConfig` 값을 실제 설정값으로 교체합니다.
4. Authentication에서 이메일/비밀번호 로그인을 활성화합니다.
5. Firestore Database를 만들고 테스트용으로 시작합니다.
6. `index.html`을 브라우저에서 열거나 간단한 로컬 서버로 실행합니다.

## Firestore 컬렉션

앱은 자동으로 아래 컬렉션을 사용합니다.

- `players`: 선수 정보
- `clubs`: 구단 프런트 정보
- `movements`: 트레이드, FA 영입, 방출, 이적 등 선수 이동 내역
- `users`: 계정 프로필과 역할 정보
- `appMeta/bootstrap`: 첫 BOSS 계정 생성 여부

## 역할

- `BOSS`: 전체 계정 권한 관리, 로스터, 이동, 구단 프런트 관리
- `STAFF`: 로스터 수정, 트레이드, FA, 선수 이동 등 전체 운영 관리
- `OWNER`: 본인 팀 선수의 이동 정보와 관련 이동 내역만 관리
- `COACH`: 본인 팀 선수의 포지션만 수정
- `UMPIRE`: PLAYER와 같은 조회 권한에 로스터 유효성 검사 탭 추가
- `PLAYER`: 기본 역할, 로스터와 선수 이동 정보 조회만 가능

계정 권한 관리:

- `BOSS`만 `계정 권한 관리` 탭을 볼 수 있습니다.
- 닉네임, 이메일, 역할, 팀으로 계정을 검색할 수 있습니다.
- 역할과 팀을 수정할 수 있습니다.
- 삭제 버튼은 Firestore의 `users/{uid}` 프로필 문서를 삭제합니다.
- 브라우저 앱에서는 Firebase Authentication 계정 자체를 관리자 권한으로 삭제할 수 없습니다. Auth 계정까지 완전히 지우려면 Firebase Console의 Authentication 사용자 목록에서 삭제하거나, Firebase Admin SDK/Cloud Functions에서 `admin.auth().deleteUser(uid)`를 실행해야 합니다.

## 선수 이동 기능

메인 화면 상단에는 최근 이동 내용 5건이 표시됩니다. 팀별 이동 내역에서는 팀명 또는 선수명 검색과 이동 유형 필터를 사용할 수 있습니다.

트레이드는 여러 명을 줄 단위로 입력할 수 있습니다. 예를 들어 이전 팀 선수에 3명, 새 팀/상대 선수에 2명을 입력하면 `3대2` 트레이드로 표시됩니다.

선수 이름은 줄바꿈 또는 쉼표로 구분할 수 있습니다. 트레이드는 하나의 묶음 기록으로 저장되고, 방출/은퇴/FA 영입/이적은 여러 명을 입력하면 선수별 기록으로 각각 저장됩니다.

이동 처리하려는 선수는 먼저 `선수 목록`에 등록되어 있어야 합니다. 이전 팀/새 팀을 입력하면 해당 팀 소속 선수인지도 확인합니다.

선수 일괄 등록:

- 선수 등록 폼의 `.txt 일괄 등록`에 텍스트 파일을 넣으면 닉네임을 한 번에 등록합니다.
- 파일명으로 팀을 자동 판별합니다. 예: `CPX.txt`는 `CPX` 팀으로 등록합니다.
- 파일 안의 닉네임은 줄바꿈 또는 쉼표로 구분합니다.
- 같은 팀에 이미 등록된 닉네임은 중복 등록하지 않습니다.

팀 입력 목록:

- `RMS`
- `NDG`
- `CPX`
- `IH`
- `KRA`
- `PLT`
- `ODV`
- `SLU`
- `무소속`

팀 색상:

- `ODV` 오버드라이브: `#FF6A00`
- `RMS` 레이 마린스: `#c9982c`
- `NDG` 나이트 드래곤즈: `#000000`
- `CPX` 청화 피닉스: `#1b65ba`
- `IH` 아이언 호네츠: `#fec804`
- `KRA` 크라켄즈: `#c00000`
- `PLT` 클로베츠 플랜츠: `#0cb218`
- `SLU` 브레이브 슬러거즈: `#850000`

지원 이동 유형:

- `TRADE`: 트레이드
- `FA_SIGN`: FA 영입
- `RELEASE`: 방출
- `FORCED_RELEASE`: 임의해지, 선수는 `무소속`으로 이동하고 원래 팀 정보가 저장됨
- `RETIRE`: 은퇴, 새 팀은 `무소속`으로 저장
- `NICKNAME`: 닉네임 변경, 기존 닉네임과 새 닉네임 개수가 같아야 하며 선수 목록의 이름도 함께 변경
- `TRANSFER`: 이적

임의해지 규칙:

- 임의해지된 선수는 `forcedReleaseOriginalTeam` 값에 원 소속팀이 저장됩니다.
- 이후 FA 영입, 이적, 트레이드로 복귀할 때는 원 소속팀으로만 이동할 수 있습니다.
- 웹과 Discord 봇 모두 같은 제한을 검사합니다.

화면은 `메인`, `선수 이동`, `로스터` 탭으로 나뉩니다. 메인에는 최근 이동과 선수 목록이 보이고, 선수 이동 탭에는 이동 등록과 팀별 이동 내역이 표시됩니다.

팀별 이동 내역 권한:

- `BOSS`, `STAFF`: 전체 팀 이동 내역 조회
- `OWNER`, `COACH`: 본인 팀 관련 이동 내역만 조회
- `PLAYER`: 팀별 이동 내역 조회 불가

첫 번째로 회원가입한 계정은 자동으로 `BOSS`가 됩니다. 이후 가입 계정은 기본 `PLAYER`이며, `BOSS`가 화면 상단의 권한 관리 패널에서 역할과 팀을 바꿀 수 있습니다.

## 테스트용 Firestore 규칙

개발 중에만 아래처럼 열어둘 수 있습니다. 회원가입 후 `users`, `appMeta` 문서를 만들기 때문에 쓰기 권한이 필요합니다.

```txt
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /{document=**} {
      allow read, write: if request.auth != null;
    }
  }
}
```

현재 앱은 HTML/JS만으로 역할별 화면과 저장 로직을 제한합니다. 실제 운영 서버에서는 사용자가 브라우저 코드를 조작할 수 있으므로, Cloud Functions 또는 Firebase Admin SDK로 첫 관리자 생성과 역할 변경을 서버에서 검증하는 방식이 더 안전합니다.

회원가입이 안 될 때 먼저 확인할 것:

- Authentication > Sign-in method에서 이메일/비밀번호가 활성화되어 있는지
- Firestore Database가 생성되어 있는지
- Firestore 규칙이 로그인한 사용자의 쓰기를 허용하는지
- 같은 이메일로 이미 가입한 계정이 있는지

## Discord 봇 연동

`bot.js`는 Discord 명령어와 Firestore를 연결합니다. 봇에서 선수를 등록하거나 이동을 입력하면 웹사이트의 Firestore 데이터가 바뀌고, 웹사이트에서 선수 이동을 저장하면 봇이 지정 채널에 공지를 보냅니다.

### 1. Discord 봇 만들기

1. Discord Developer Portal에서 애플리케이션을 만듭니다.
2. Bot 메뉴에서 봇을 생성하고 토큰을 복사합니다.
3. Privileged Gateway Intents에서 `MESSAGE CONTENT INTENT`를 켭니다.
4. 서버 멤버 역할 확인이 필요하므로 `SERVER MEMBERS INTENT`도 켭니다.
5. OAuth2 URL Generator에서 `bot` 권한을 선택하고 서버에 초대합니다.
6. 봇 권한은 최소한 `View Channels`, `Send Messages`, `Embed Links`, `Read Message History`가 필요합니다.

### 2. Firebase Admin SDK 키 만들기

1. Firebase Console > 프로젝트 설정 > 서비스 계정으로 이동합니다.
2. 새 비공개 키를 생성합니다.
3. JSON 파일에서 아래 값을 확인합니다.
   - `project_id`
   - `client_email`
   - `private_key`

### 3. `.env` 설정

`env.example`을 복사해서 `.env` 파일을 만듭니다.

```powershell
Copy-Item env.example .env
```

`.env`에 값을 채웁니다.

```env
DISCORD_TOKEN=your_discord_bot_token_here
DISCORD_CHANNEL_ID=your_channel_id_here
FIREBASE_PROJECT_ID=mbo-player
FIREBASE_CLIENT_EMAIL=firebase-adminsdk-xxxxx@mbo-player.iam.gserviceaccount.com
FIREBASE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
```

채널 ID는 Discord 개발자 모드를 켠 뒤 공지 채널을 우클릭해서 복사합니다.

### 4. 패키지 설치 및 실행

```powershell
npm install
npm start
```

정상 실행되면 콘솔에 `MBO 봇 준비됨` 로그가 표시됩니다.

### 5. 지원 명령어

접두사는 `!mbo`입니다.

```txt
!mbo 도움말
!mbo 선수목록
!mbo 선수목록 RMS
!mbo 선수등록 <이름> <팀> [포지션]
!mbo 선수삭제 <이름> <팀>
!mbo 이동 <유형> <이전팀> <선수명> [새팀] [날짜]
!mbo 이동 트레이드 <이전팀> <선수명> <새팀> <상대선수명> [날짜]
!mbo 최근이동
```

예시:

```txt
!mbo 선수등록 Hyojung_0501 RMS 2B
!mbo 이동 방출 RMS Hyojung_0501
!mbo 이동 임의해지 CPX mario_1313
!mbo 이동 트레이드 RMS Hyojung_0501 NDG mario_1313 2026-05-05
```

지원 이동 유형:

- `트레이드`
- `FA영입`
- `방출`
- `은퇴`
- `닉네임변경`
- `이적`
- `임의해지`

### 6. 웹사이트와 봇이 연동되는 방식

- 봇 명령어는 Firebase Admin SDK로 Firestore의 `players`, `movements` 컬렉션을 수정합니다.
- 웹사이트는 Firestore `onSnapshot`으로 실시간 구독 중이므로 봇이 수정한 내용이 자동 반영됩니다.
- 웹사이트에서 이동 내역을 저장하면 `movements`에 문서가 추가됩니다.
- 봇은 `movements`의 새 문서를 감지해서 `DISCORD_CHANNEL_ID` 채널에 Embed 공지를 보냅니다.
- 봇 자신이 만든 이동 내역은 `createdBy: "discord-bot"` 값으로 구분해서 중복 공지를 막습니다.

### 7. 주의사항

- 봇을 켜 둔 PC/서버가 꺼지면 Discord 명령어와 자동 공지가 작동하지 않습니다.
- `PLAYER` 역할이 있는 Discord 사용자는 `!mbo 선수등록`을 사용할 수 없게 막혀 있습니다.
- Discord 역할명은 정확히 `PLAYER`여야 합니다.
- 임의해지 선수는 원 소속팀으로만 복귀할 수 있습니다.
- 봇은 Firebase Admin SDK를 사용하므로 `.env`와 서비스 계정 키를 외부에 공개하면 안 됩니다.

## 실행

정적 파일이라 `index.html`을 직접 열 수 있습니다. Firebase 모듈 CDN을 안정적으로 불러오려면 로컬 서버를 쓰는 편이 좋습니다.

```powershell
python -m http.server 5500
```

그 다음 브라우저에서 `http://localhost:5500`으로 접속합니다.

## Railway로 Discord 봇 24시간 구동

가능합니다. 이 프로젝트의 봇은 `package.json`에 이미 아래 실행 스크립트가 있어서 Railway가 Node.js 앱으로 감지하면 `npm start`로 실행할 수 있습니다.

```json
{
  "scripts": {
    "start": "node bot.js"
  }
}
```

### 먼저 절대 올리면 안 되는 파일

GitHub에 올리면 안 됩니다.

- `.env`
- Firebase 서비스 계정 JSON 파일
- `node_modules`

프로젝트에는 `.gitignore`가 있어 위 파일들이 커밋되지 않도록 막습니다. 그래도 GitHub에 올리기 전 `git status`에서 `.env`나 `firebase-adminsdk` JSON이 보이면 절대 커밋하지 마세요.

### GitHub에 올리기

```powershell
git init
git add .
git status
git commit -m "Initial MBO bot deployment"
git branch -M main
git remote add origin https://github.com/내계정/저장소이름.git
git push -u origin main
```

`git status`에서 `.env`나 서비스 계정 JSON이 보이면 `git add` 전에 멈추고 `.gitignore`를 확인해야 합니다.

### Railway 프로젝트 만들기

1. Railway에서 새 프로젝트를 만듭니다.
2. `Deploy from GitHub repo`를 선택합니다.
3. 방금 올린 GitHub 저장소를 선택합니다.
4. Railway가 Node.js 프로젝트를 감지하면 자동으로 빌드합니다.
5. 서비스의 `Variables` 탭으로 이동합니다.

### Railway Variables 설정

Railway에는 `.env` 파일을 올리는 대신 Variables에 직접 넣습니다.

필수 변수:

```env
DISCORD_TOKEN=...
DISCORD_CHANNEL_ID=...
FIREBASE_PROJECT_ID=mbo-player
FIREBASE_CLIENT_EMAIL=...
FIREBASE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
```

Railway Variables의 RAW Editor가 있으면 `.env` 내용을 붙여넣는 방식이 편합니다.

주의:

- `FIREBASE_PRIVATE_KEY`의 `\n` 줄바꿈은 유지해야 합니다.
- 따옴표까지 포함해도 `bot.js`에서 `replace(/\\n/g, "\n")`로 처리합니다.
- Firebase 서비스 계정 JSON 파일 자체를 Railway에 업로드하거나 GitHub에 커밋할 필요는 없습니다.

### Discord Developer Portal 설정

봇이 메시지 명령어를 읽고 역할을 확인하려면 아래 Intent가 켜져 있어야 합니다.

- `MESSAGE CONTENT INTENT`
- `SERVER MEMBERS INTENT`

봇 초대 권한은 최소 아래가 필요합니다.

- View Channels
- Send Messages
- Embed Links
- Read Message History

### 24시간 구동 관련 비용

Railway는 현재 Free/Trial로 실험할 수 있지만, 계속 켜두는 봇은 보통 유료 플랜 사용을 전제로 잡는 편이 안전합니다. Railway 문서 기준 Hobby 플랜은 월 $5이고, 이 금액이 월 리소스 사용량에 포함됩니다. Discord 봇은 트래픽이 크지 않으면 보통 리소스를 많이 쓰지 않지만, 실제 비용은 Railway 사용량에 따라 달라집니다.

### 배포 후 확인

Railway 서비스 로그에서 아래 문구가 보이면 봇이 켜진 상태입니다.

```txt
MBO 봇 준비됨: 봇이름#0000
```

Discord 서버에서 테스트합니다.

```txt
!mbo 도움말
!mbo 선수목록
!mbo 최근이동
```

웹에서 선수 이동을 저장했을 때 `DISCORD_CHANNEL_ID` 채널에 공지가 올라오면 웹 -> 봇 연동도 정상입니다.

### Railway에서 자주 막히는 부분

- Variables를 넣은 뒤 재배포하지 않으면 새 값이 반영되지 않을 수 있습니다.
- `DISCORD_TOKEN`이 틀리면 봇 로그인이 실패합니다.
- `FIREBASE_PRIVATE_KEY` 줄바꿈이 깨지면 Firebase Admin 초기화가 실패합니다.
- Discord Developer Portal에서 Message Content Intent가 꺼져 있으면 `!mbo` 명령어를 읽지 못합니다.
- 봇이 채널에 글을 못 쓰면 Discord 채널 권한과 `DISCORD_CHANNEL_ID`를 확인해야 합니다.
