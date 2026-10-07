#!/bin/bash
# PATERN 대시보드 보기 전용 연결 — Mac 에서 한 번 실행한다.
#   대시보드(127.0.0.1:8765) 앞에 보기 전용 문(127.0.0.1:8766)을 세우고
#   Tailscale 로 내 기기(아이폰)에만 그 문을 연다. 대시보드 본체(OVERDRIVE)는 건드리지 않는다.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.patern_viewer"
LABEL="com.patern.viewer-gateway"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UPSTREAM_PORT=8765
GW_PORT=8766

say()  { printf '\n▶ %s\n' "$*"; }
warn() { printf '\n⚠ %s\n' "$*"; }
die()  { printf '\n✖ %s\n' "$*" >&2; exit 1; }

listen_addrs() { { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; } | awk 'NR>1{print $9}' | sort -u; }

# 1. 대시보드 본체가 Mac 안에서만 열려 있는지 — 밖에도 열려 있으면 보기 전용 문이 의미 없다
say "대시보드($UPSTREAM_PORT) 열린 주소 확인"
UP="$(listen_addrs $UPSTREAM_PORT)"
[ -n "$UP" ] || die "$UPSTREAM_PORT 에서 대시보드가 보이지 않습니다. 대시보드를 먼저 켜고 다시 실행하세요."
echo "$UP"
if echo "$UP" | grep -vqE '^(127\.0\.0\.1|\[::1\]|localhost):'; then
  die "대시보드가 Mac 밖에도 열려 있습니다 ($UP). 이 상태로는 보기 전용 문을 거치지 않고 본체로 들어올 수 있어 중단합니다."
fi

# 2. 파이썬
PY=""
for c in /opt/anaconda3/bin/python3 "$(command -v python3 || true)"; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "python3 (3.8 이상)을 찾지 못했습니다."
echo "python: $PY"

# 3. 이 Mac 의 파이썬으로 시험부터
say "시험 실행"
( cd "$SRC" && "$PY" -m unittest -q test_patern_viewer_gateway ) || die "시험 실패 — 설치하지 않습니다."

# 4. 설치 (규칙 파일은 이미 있으면 그대로 둔다)
say "설치: $DEST"
mkdir -p "$DEST/logs" "$HOME/Library/LaunchAgents"
cp "$SRC/patern_viewer_gateway.py" "$DEST/"
if [ -f "$DEST/viewer_gateway_rules.json" ]; then
  echo "규칙 파일은 기존 것 유지: $DEST/viewer_gateway_rules.json"
else
  cp "$SRC/viewer_gateway_rules.json" "$DEST/"
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
GW="$(listen_addrs $GW_PORT)"
[ -z "$GW" ] || die "$GW_PORT 를 이미 다른 프로그램이 쓰고 있습니다 ($GW)."

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DEST/patern_viewer_gateway.py</string>
    <string>--listen</string><string>127.0.0.1:$GW_PORT</string>
    <string>--upstream</string><string>127.0.0.1:$UPSTREAM_PORT</string>
    <string>--rules</string><string>$DEST/viewer_gateway_rules.json</string>
    <string>--log</string><string>$DEST/logs/viewer_gateway.jsonl</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DEST/logs/gateway.out</string>
  <key>StandardErrorPath</key><string>$DEST/logs/gateway.err</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"

say "보기 전용 문 상태"
OK=""
for _ in $(seq 1 20); do
  if curl -fs "http://127.0.0.1:$GW_PORT/__viewer/health"; then OK=1; break; fi
  sleep 0.5
done
echo
[ -n "$OK" ] || die "보기 전용 문이 뜨지 않았습니다. $DEST/logs/gateway.err 를 확인하세요."
GW="$(listen_addrs $GW_PORT)"
echo "$GW" | grep -vqE '^127\.0\.0\.1:' && die "보기 전용 문이 127.0.0.1 밖에 열려 있습니다 ($GW)."

# 5. Tailscale 로 내 기기에만 연다
TS="$(command -v tailscale || true)"
if [ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi
if [ -z "$TS" ]; then
  say "Tailscale 이 아직 없습니다"
  echo "  1) Mac App Store 또는 https://tailscale.com/download/mac 에서 Tailscale 설치 후 로그인"
  echo "  2) 이 스크립트를 다시 실행"
  exit 0
fi
"$TS" status >/dev/null 2>&1 || die "Tailscale 에 로그인되어 있지 않습니다. 메뉴 막대의 Tailscale 아이콘에서 로그인한 뒤 다시 실행하세요."

say "Tailscale 로 보기 전용 문($GW_PORT)만 열기"
echo "(처음이면 'Serve 를 켜라'는 링크가 나옵니다 — 그 링크를 열어 허용하면 이어서 진행됩니다)"
"$TS" serve --bg "$GW_PORT"
STATUS="$("$TS" serve status 2>&1 || true)"
echo "$STATUS"
if echo "$STATUS" | grep -qE "(127\.0\.0\.1|localhost):$UPSTREAM_PORT"; then
  warn "대시보드 본체($UPSTREAM_PORT)도 Tailscale 에 열려 있습니다. 제어까지 열리니 닫으세요: $TS serve status 로 포트를 보고 '$TS serve --https=<포트> off'"
fi
if echo "$STATUS" | grep -qi "funnel on"; then
  warn "Funnel(인터넷 전체 공개)이 켜져 있습니다. 끄세요: $TS funnel reset"
fi

if { pmset -g 2>/dev/null || true; } | awk '$1=="sleep"{f=1; if ($2!=0) w=1} END{exit !(f && w)}'; then
  warn "Mac 이 잠자기에 들어가면 아이폰에서 안 보입니다 (시스템 설정 > 에너지 에서 자동 잠자기 끄기)."
fi

URL="$(echo "$STATUS" | grep -oE 'https://[^ /]+' | head -1 || true)"
say "완료"
echo "아이폰에서:"
echo "  1) App Store 에서 'Tailscale' 설치 → Mac 과 같은 계정으로 로그인 → VPN 허용"
echo "  2) Safari 에서 ${URL:-https://<맥이름>.<tailnet>.ts.net} 열기"
echo "  3) 공유 버튼 → '홈 화면에 추가' 하면 앱처럼 열립니다"
echo
echo "막힌 요청 보기:  $PY $DEST/patern_viewer_gateway.py --show-blocked --log $DEST/logs/viewer_gateway.jsonl"
