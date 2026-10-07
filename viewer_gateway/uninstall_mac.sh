#!/bin/bash
# 보기 전용 연결 해제 — 아이폰 접속을 닫고 게이트웨이를 내린다. 로그와 규칙은 ~/.patern_viewer 에 남긴다.
set -uo pipefail

LABEL="com.patern.viewer-gateway"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

TS="$(command -v tailscale || true)"
if [ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi
if [ -n "$TS" ]; then
  "$TS" serve --https=443 off && echo "Tailscale 접속 닫음"
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "게이트웨이 내림"
rm -f "$PLIST"
echo "완료 (로그·규칙: $HOME/.patern_viewer)"
