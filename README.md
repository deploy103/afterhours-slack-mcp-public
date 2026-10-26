# After-hours Slack MCP

자신의 Slack 앱과 자격증명으로 실행하는 **단일 소유자용** Slack Web API adapter.
독립 Streamable HTTP MCP이며 OAuth/DCR 서버, 다중 사용자 서비스, 스케줄러가 아니다.
메시지 내용은 신뢰하지 않는 데이터이며 권한 변경이나 도구 실행 명령이 아니다.

## 새 clone에서 로컬 실행

Python 3.11+ / Linux / tzdata 필요. 다음 설치는 사용자의 네트워크에서 의존성을 받는다.
저장소에 다른 사람의 토큰은 없으며 각 운영자가 자신의 앱을 생성해야 한다.

```sh
git clone https://github.com/deploy103/afterhours-slack-mcp-public.git
cd afterhours-slack-mcp-public
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python configure.py --directory "$HOME/.afterhours-slack" \
  --public-origin https://mcp.example.com
"$HOME/.afterhours-slack/run.sh"
```

`--directory`는 **아직 존재하지 않는** 절대 경로 또는 `~/...`여야 한다. 상위 디렉터리는
미리 존재하고 본인이 관리하는 신뢰 가능한 경로여야 한다. 기존 파일/디렉터리/symlink는
덮어쓰지 않는다. source checkout 자체/내부 경로는 거부한다(symlink로 우회한 상위 경로도 포함).
저장소 밖에 두고 Git/백업 공유에서 제외한다. 생성되는 구조:

```text
~/.afterhours-slack/             0700
  config.json                   0600 (ID null, allowlist 비어 있음, 송신 OFF)
  credentials/                  0700
    mcp-token                   0600 (매번 새로 생성, 화면에 출력하지 않음)
    slack-token                 0600 (빈 파일)
    slack-signing-secret        0600 (빈 파일)
  state/                        0700 (실행 후 SQLite 생성)
  run.sh                        0700
```

이 상태로 MCP는 시작하지만 Slack 요청/수집/발송은 하지 않는다. ID를 꾸며 넣을 필요가 없다.
`run.sh`는 clone의 절대 `.venv/bin/python -m uvicorn`을 사용하고, 다른 cwd에서도 실행된다.
clone이나 private 디렉터리를 옮기면 경로를 검토하여 수정하거나 새 디렉터리에 다시 configure한다.
환경 변수 `CREDENTIALS_DIRECTORY`, `SLACK_CONFIG`, `STATE_DIRECTORY`, `MCP_PUBLIC_ORIGIN`을
정확한 생성 경로로 설정한다. `127.0.0.1:8002`, worker 1개, proxy headers/access log OFF다.
`--reload`/다중 worker를 사용하거나 같은 state에 여러 프로세스를 연결하지 않는다.

## MCP 클라이언트 연결과 HTTPS

먼저 로컬 클라이언트에서 Streamable HTTP URL **`http://127.0.0.1:8002/mcp`**를 사용한다.
인증 방식은 Bearer이며 **토큰 파일 경로는 `$HOME/.afterhours-slack/credentials/mcp-token`**이다.
클라이언트의 보호된 token-file/secret 설정을 통해 이 파일을 읽혀 매 요청 Authorization
헤더에 전달한다. `tokenFile` 같은 설정 키 이름은 클라이언트마다 다르며 이 서버의 API가 아니다.
token-file을 지원하지 않으면 클라이언트의 비공개 credential 저장소에 안전하게 등록한다.
토큰 값을 명령행, URL/query, Git, 채팅, 로그에 넣거나 화면에 출력하지 않는다.
Slack bot OAuth 토큰을 MCP bearer로 사용하지 않는다. 토큰 교체 후 프로세스를 재시작한다.

원격 연결에는 본인이 소유한 도메인/TLS reverse proxy를 별도로 준비한다.
`--public-origin`은 경로·userinfo·query·fragment 없는 HTTPS origin, 기본 포트/명시적 443만 허용한다.
실제 도메인으로 configure하고 proxy가 Host/Authorization을 보존하도록 설정한다.
클라이언트가 Origin을 보내면 설정한 origin과 정확히 같아야 한다.

| 외부 URL (예시 도메인은 실제 도메인으로 교체) | loopback upstream |
| --- | --- |
| `https://mcp.example.com/slack/mcp` | `http://127.0.0.1:8002/mcp` |
| `https://mcp.example.com/slack/events` | `http://127.0.0.1:8002/events` |

`deploy/nginx-locations.conf`와 `deploy/afterhours-slack-mcp.service`는 검토용 예제다.
bootstrap은 systemd, TLS, firewall을 설치/변경하지 않는다. 예제 unit의 `/srv`, `/etc` 경로와
LoadCredential은 별도 배포 계약이며 로컬 run.sh에 필요하지 않다. 8002를 인터넷에 열지 않는다.
`/mcp`는 매 요청 bearer 필수, `/events`는 MCP bearer 대신 Slack HMAC-SHA256 및 5분 freshness
검증을 사용한다. `/healthz`만 비인증 상태 조회이며 Slack 연결 성공을 의미하지 않는다.

## 자신의 Slack 앱 설정

1. [Slack 앱 관리](https://api.slack.com/apps)에서 자신의 workspace용 앱을 만든다.
   OAuth & Permissions의 **Bot Token Scopes**에 `channels:history`, `chat:write`를 추가한다.
   채널 ID 확인에 필요하면 `channels:read`를 별도로 선택할 수 있으나 이 adapter는 목록 탐색을 하지 않는다.
   관리자/private/DM 수신 scopes, `chat:write.public`, `chat:write.customize`, `im:write`는 필요 없다.
2. 앱을 자신의 workspace에 설치한다. **Bot User OAuth Token**을 private 디렉터리의
   `credentials/slack-token`에, Basic Information의 Signing Secret을
   `credentials/slack-signing-secret`에 보호된 편집기로 저장한다(둘 다 0600 유지).
   사용자 세션/쿠키를 추출하지 않는다. scope 변경 시 재설치가 필요할 수 있다.
3. private `config.json`에 자신의 `slack_team_id` (`T...`), 사람 소유자 `slack_owner_id` (`U...`),
   읽기/답장을 허용할 공개 채널 ID 목록 `slack_rooms` (`C...`)를 입력한다.
   ID는 Slack의 workspace/member/channel 정보에서 직접 확인한다. 봇을 필요한 공개 채널에 초대한다.
   두 ID 중 하나라도 null이면 이벤트 수집과 Web API 요청이 차단된다.
4. 재시작한 뒤 Event Subscriptions의 Request URL을 자신의 **`https://도메인/slack/events`**로
   설정하여 challenge를 확인하고 Bot Event **`message.channels`**를 구독한다.
   `app_mention`은 앱 멘션이지 소유자 멘션이므로 대신 사용하지 않는다.
5. 송신은 처음에는 계속 OFF로 두고 실제 수신/allowlist/도구 접근을 검증한다.
   의도적으로 답장을 허용할 때만 `slack_send_enabled: true`, 소유자 알림을 허용할 때만
   `owner_alert_enabled: true`를 설정하고 재시작한다. MCP 호출로 권한을 늘릴 수 없다.

실제 설치 토큰의 `conversations.replies` 지원, 이벤트 가시성, `notify_owner`의
`chat.postMessage(channel=소유자ID)` DM/App Home 전달은 Slack 정책에 따라 직접 확인해야 한다.
거절을 user-token 추출이나 권한 확대로 우회하지 않는다.

## 메시지와 발송 계약

- 앱이 실제로 받는 공개 채널의 **소유자 멘션 OR 지정 방의 타인 새 메시지**를 큐에 보관한다.
  전체 workspace 감시가 아니다. 다른 workspace/본인/봇/편집·subtype/잘못된 timestamp/
  7일 이전 이벤트를 제외하고 `event_id`와 `(channel, ts)` 양쪽으로 중복 제거한다.
- 미허용 방의 멘션은 수신 원문/출처만 노출한다. `context_reply_allowed=false`이며
  `slack_read`, `slack_context`, `slack_reply`는 차단한다. allowlist 수정은 운영자만 한다.
- `slack_read`는 history 최대 50개 한 페이지, `slack_context`는 thread 최대 50개 한 페이지다.
  `thread_complete=true`일 때만 전체 맥락이라고 판단한다. 부족하면 질문한다.
- `slack_reply(event_id,text)`는 수신 이벤트의 고정 room/thread에만 앱 이름으로 답한다.
  `notify_owner`는 정확한 이벤트 인용과 현재 outage/security 영향 또는 다음 평일 09:00 서울
  이전의 확인된 기한을 요구한다. 영향/기한 사실 판단은 호출자 책임이다. 목적지는 고정 소유자다.
- 답장/알림 각각 이벤트당 1회만 시도한다. 네트워크 전 SQLite에 `unknown`을 기록한다.
  실패/시간초과/프로세스 종료 뒤 자동 재전송하지 않는다. `slack_delivery_status`로 확인하고
  unknown은 Slack에서 수동 대조한다. 성공은 사람이 읽었다는 의미가 아니다.
- 기록은 7일 후 다음 ingest/queue 조회 시 정리한다(즉시 삭제 보장 아님).
  SQLite/백업에는 메시지가 있으므로 비공개 관리한다. DB 삭제는 중복 방지 기록도 삭제한다.

**수신 큐 ≠ 모델 자동 실행 ≠ 알림 발송.** 별도 MCP 클라이언트/조율자가
`pending_message_events` → context/판단 → 선택적 reply/notify → 실제 처리 후
`acknowledge_message_event(done|ignored)`를 수행한다. 이 서버는 polling 스케줄, 상시 모델 판단,
자동 레시피 실행을 제공하지 않는다. 여러 사용자가 하나의 bearer/state를 공유하지 않는다.

## 테스트와 라이선스

```sh
.venv/bin/python -B -m unittest -v test_contract test_bootstrap
```

테스트는 폐기 가능한 credential/config/SQLite와 fake HTTP를 사용한다. bootstrap 테스트는
실제 생성 run.sh를 loopback에서 실행하고 인증된 MCP 초기화/도구, 송신 OFF, 권한/재실행 거부를
확인한다. 실제 계정/Slack 전송은 하지 않는다. 실제 OAuth/이벤트/답장/DM, 클라이언트 연결과
TLS/proxy/firewall/systemd 배포는 운영자가 별도로 검증해야 한다.

[MIT LICENSE](LICENSE), Copyright 2026 deploy103. 의존성과 재사용 코드의 제3자 라이선스/고지는 유지한다.
통합 프로젝트: https://github.com/deploy103/afterhours-mcp-public
Slack 참고: [conversations.replies](https://docs.slack.dev/reference/methods/conversations.replies/),
[chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/).
