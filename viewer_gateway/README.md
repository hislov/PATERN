# PATERN 대시보드 보기 전용 연결 (아이폰)

Mac 의 대시보드(`127.0.0.1:8765`) 앞에 **보기 전용 문**(`127.0.0.1:8766`)을 세우고,
Tailscale 로 **내 기기에만** 그 문을 연다. 대시보드 본체(OVERDRIVE)는 건드리지 않는다.

```
아이폰 ─▶ Tailscale(내 기기만) ─▶ 보기 전용 문 :8766 ─▶ 대시보드 :8765
```

| 요청 | 처리 |
|---|---|
| 화면·탭 이동·목록·차트 (GET) | 통과 |
| 새로고침·재시작·진단·주문·전송 등 동작 이름이 든 경로 | 차단 (403 "보기 전용") |
| POST | `allow_post_paths` 에 있는 경로만 통과 (처음엔 없음) · 동시 1건 · 분당 6건 |
| PUT / DELETE / PATCH / 웹소켓 | 차단 |
| `.env`·숨김 파일·secret·password·api_key | 차단 |

## 설치 (Mac 에서 한 번)

```bash
git clone -b claude/pattern-dev-credit-shortage-zqmpw4 https://github.com/hislov/PATERN.git ~/PATERN_viewer
bash ~/PATERN_viewer/viewer_gateway/install_mac.sh
```

스크립트가 하는 일:
1. 대시보드(8765)가 Mac 안(127.0.0.1)에만 열려 있는지 확인 — 밖에도 열려 있으면 **중단**
2. 이 Mac 의 파이썬으로 시험 실행 — 실패하면 설치 안 함
3. `~/.patern_viewer/` 에 설치, launchd(`com.patern.viewer-gateway`)로 상시 실행
4. `tailscale serve --bg 8766` — 8766 만 연다 (8765 는 열지 않는다)
5. 아이폰에서 열 주소 출력

Tailscale 이 없으면 Mac App Store 에서 설치·로그인한 뒤 스크립트를 다시 실행한다.
처음 `tailscale serve` 를 쓰면 허용 링크가 나온다 — 열어서 허용하면 이어진다.

## 아이폰

1. App Store 에서 **Tailscale** 설치 → Mac 과 같은 계정으로 로그인 → VPN 허용
2. Safari 에서 스크립트가 알려준 `https://<맥이름>.<tailnet>.ts.net` 열기
3. 공유 버튼 → **홈 화면에 추가** → 앱처럼 열린다

Mac 이 켜져 있고 잠자지 않아야 보인다.

## 운영

- 막힌 요청 보기 (화면 일부가 비면 먼저 이걸 본다):
  `python3 ~/.patern_viewer/patern_viewer_gateway.py --show-blocked`
- 규칙 바꾸기: `~/.patern_viewer/viewer_gateway_rules.json` 편집 후
  `launchctl kickstart -k gui/$(id -u)/com.patern.viewer-gateway`
- **PATERN AI 질문 열기**: 질문 API 가 생기면(PATERN 데스크 D3) 그 경로를 `allow_post_paths` 에 한 줄 추가.
  티커 조회가 POST 라면 같은 방법으로 추가.
- 요청 원장: `~/.patern_viewer/logs/viewer_gateway.jsonl` (누가·무엇을·통과/차단, 5MB 회전)
- 끄기: `bash ~/PATERN_viewer/viewer_gateway/uninstall_mac.sh`

## 하지 말 것

- `tailscale serve 8765` — 본체가 열려 제어까지 된다
- `tailscale funnel` — 인터넷 전체 공개
- 대시보드를 `0.0.0.0` 으로 열기 — 보기 전용 문을 거치지 않게 된다

## 알려진 한계

- 화면 코드가 `http://127.0.0.1:8765/...` 같은 절대 주소를 부르면 아이폰에서는 그 부분이 비어 보인다.
- 웹소켓 실시간 갱신은 막혀 있다 (일반 요청·SSE 스트림은 통과).
