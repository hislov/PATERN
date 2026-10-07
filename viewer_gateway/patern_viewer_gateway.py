#!/usr/bin/env python3
"""PATERN 대시보드 보기 전용 게이트웨이.

127.0.0.1:8766 에서 받아 127.0.0.1:8765 대시보드로 넘긴다.
읽기(GET/HEAD)는 통과, 제어·민감 경로는 차단, POST 는 허용 목록에 있는 것만 통과한다.
표준 라이브러리만 쓴다 — 대시보드 본체(OVERDRIVE)는 건드리지 않는다.

    python3 patern_viewer_gateway.py                 # 실행
    python3 patern_viewer_gateway.py --show-blocked  # 원장에서 막힌 요청 집계
"""
import argparse
import collections
import http.client
import http.server
import json
import os
import posixpath
import re
import socketserver
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "viewer_gateway_rules.json")
DEFAULT_LOG = os.path.expanduser("~/.patern_viewer/logs/viewer_gateway.jsonl")
LOG_ROTATE_BYTES = 5 * 1024 * 1024

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
}
# 경로 구조를 바꿀 수 있는 인코딩: %2F(/) %2E(.) %5C(\)
ENCODED_STRUCTURE = re.compile(r"%(2f|2e|5c)", re.I)


class Rules:
    def __init__(self, data):
        self.deny_always = [re.compile(p, re.I) for p in data.get("deny_always_patterns", [])]
        self.deny_get = [re.compile(p, re.I) for p in data.get("deny_get_patterns", [])]
        self.static_ext = tuple(e.lower() for e in data.get("static_ext", []))
        self.allow_post = [re.compile(p, re.I) for p in data.get("allow_post_paths", [])]
        rate = data.get("post_rate", {})
        self.post_concurrent = int(rate.get("concurrent", 1))
        self.post_per_minute = int(rate.get("per_minute", 6))
        self.max_body = int(data.get("max_body_bytes", 65536))
        self.upstream_timeout = float(data.get("upstream_timeout_s", 180))

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))


def normalize_path(raw_path):
    """요청 경로를 판정용으로 푼다. 구조를 흐리는 경로면 None."""
    if "\\" in raw_path or ENCODED_STRUCTURE.search(raw_path):
        return None
    path = urllib.parse.unquote(raw_path)
    if not path.startswith("/") or "//" in path:
        return None
    if any(seg in (".", "..") for seg in path.split("/")):
        return None
    norm = posixpath.normpath(path)
    if path.endswith("/") and norm != "/":
        norm += "/"
    return norm


def decide(method, raw_path, headers, rules):
    """(결정, 상태코드, 사유). 결정은 'forward' | 'post' | 'block'."""
    path = normalize_path(raw_path)
    if path is None:
        return "block", 400, "bad_path"
    if headers.get("Upgrade"):
        return "block", 403, "upgrade_not_allowed"
    for pat in rules.deny_always:
        if pat.search(path):
            return "block", 403, "deny_always:" + pat.pattern
    if method in ("GET", "HEAD"):
        # 정적 파일(.js .css 등)은 이름에 'order' 같은 말이 있어도 동작이 아니다
        if not path.lower().endswith(rules.static_ext):
            for pat in rules.deny_get:
                if pat.search(path):
                    return "block", 403, "deny_get:" + pat.pattern
        return "forward", 0, "read"
    if method == "POST":
        for pat in rules.allow_post:
            if pat.fullmatch(path):
                return "post", 0, "allow_post:" + pat.pattern
        return "block", 403, "post_not_allowed"
    return "block", 403, "method_not_allowed"


class PostLimiter:
    def __init__(self, concurrent, per_minute):
        self.sem = threading.BoundedSemaphore(max(concurrent, 1))
        self.per_minute = per_minute
        self.stamps = collections.deque()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            while self.stamps and now - self.stamps[0] > 60:
                self.stamps.popleft()
            if len(self.stamps) >= self.per_minute:
                return "rate_per_minute"
            if not self.sem.acquire(blocking=False):
                return "busy"
            self.stamps.append(now)
            return None

    def release(self):
        self.sem.release()


class Ledger:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, row):
        if not self.path:
            return
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self.lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_ROTATE_BYTES:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


class Gateway(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, listen, upstream, rules, ledger):
        super().__init__(listen, Handler)
        self.upstream = upstream
        self.rules = rules
        self.ledger = ledger
        self.limiter = PostLimiter(rules.post_concurrent, rules.post_per_minute)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PaternViewer/1.0"
    protocol_version = "HTTP/1.0"  # 응답마다 연결을 닫는다 — 스트리밍(SSE)도 그대로 흘려보낸다

    def log_message(self, fmt, *args):
        pass

    def _who(self):
        return (self.headers.get("Tailscale-User-Login")
                or self.headers.get("X-Forwarded-For")
                or self.client_address[0])

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self):
        t0 = time.monotonic()
        # urlsplit 은 '//x/..' 의 x 를 호스트로 읽는다 — 판정 경로와 넘기는 경로가 같도록 직접 자른다
        raw_path = self.path.split("?", 1)[0].split("#", 1)[0]
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "who": self._who(),
               "method": self.command, "path": self.path}
        try:
            if raw_path == "/__viewer/health":
                self._health()
                row.update(decision="health", status=200)
                return
            decision, status, reason = decide(self.command, raw_path, self.headers, self.server.rules)
            row["reason"] = reason
            if decision == "block":
                row.update(decision="blocked", status=status)
                self._send_json(status, {"ok": False, "error": "보기 전용", "reason": reason})
                return
            body = None
            if decision == "post":
                length = int(self.headers.get("Content-Length") or 0)
                if length > self.server.rules.max_body:
                    row.update(decision="blocked", status=413)
                    self._send_json(413, {"ok": False, "error": "요청이 너무 큼"})
                    return
                busy = self.server.limiter.acquire()
                if busy:
                    row.update(decision="limited", status=429, reason=busy)
                    self._send_json(429, {"ok": False, "error": "잠시 후 다시", "reason": busy})
                    return
                try:
                    body = self.rfile.read(length) if length else b""
                    row["status"] = self._forward(body)
                finally:
                    self.server.limiter.release()
            else:
                row["status"] = self._forward(None)
            row["decision"] = "forwarded"
        except (BrokenPipeError, ConnectionResetError):
            row.setdefault("decision", "client_gone")
        finally:
            row["ms"] = int((time.monotonic() - t0) * 1000)
            self.server.ledger.write(row)

    def _forward(self, body):
        host, port = self.server.upstream
        conn = http.client.HTTPConnection(host, port, timeout=self.server.rules.upstream_timeout)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in ("host", "content-length")}
        headers["Host"] = "%s:%d" % (host, port)
        headers["X-Patern-Viewer"] = "1"
        headers["X-Forwarded-For"] = self.client_address[0]
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            self._send_json(502, {"ok": False, "error": "대시보드에 연결 안 됨", "detail": str(e)})
            return 502
        try:
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                read = getattr(resp, "read1", resp.read)
                while True:
                    chunk = read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            return resp.status
        finally:
            conn.close()

    def _health(self):
        host, port = self.server.upstream
        up = False
        try:
            c = http.client.HTTPConnection(host, port, timeout=3)
            c.request("HEAD", "/")
            up = c.getresponse().status < 500
            c.close()
        except OSError:
            pass
        self._send_json(200, {"ok": True, "mode": "view_only", "upstream": "%s:%d" % (host, port),
                              "upstream_up": up})

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle


def show_blocked(log_path):
    counts = collections.Counter()
    for p in (log_path + ".1", log_path):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("decision") == "blocked":
                    path = r.get("path", "").split("?", 1)[0]
                    counts[(r.get("method"), path, r.get("reason"))] += 1
    if not counts:
        print("막힌 요청 없음")
        return
    for (method, path, reason), n in counts.most_common():
        print("%5d  %-6s %-50s %s" % (n, method, path, reason))


def parse_hostport(s):
    host, _, port = s.rpartition(":")
    return host or "127.0.0.1", int(port)


def main(argv=None):
    ap = argparse.ArgumentParser(description="PATERN 대시보드 보기 전용 게이트웨이")
    ap.add_argument("--listen", default="127.0.0.1:8766")
    ap.add_argument("--upstream", default="127.0.0.1:8765")
    ap.add_argument("--rules", default=DEFAULT_RULES)
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--show-blocked", action="store_true")
    args = ap.parse_args(argv)

    if args.show_blocked:
        show_blocked(args.log)
        return 0
    listen = parse_hostport(args.listen)
    if listen[0] not in ("127.0.0.1", "::1", "localhost"):
        print("게이트웨이는 127.0.0.1 에만 연다 (받은 값: %s)" % args.listen, file=sys.stderr)
        return 2
    srv = Gateway(listen, parse_hostport(args.upstream), Rules.load(args.rules), Ledger(args.log))
    print("view-only gateway %s -> %s" % (args.listen, args.upstream), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
