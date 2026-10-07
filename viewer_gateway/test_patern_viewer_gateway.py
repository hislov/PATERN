"""보기 전용 게이트웨이 시험 — 가짜 대시보드를 띄우고 게이트웨이를 거쳐 실제 HTTP 로 확인한다."""
import http.client
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patern_viewer_gateway as G  # noqa: E402

RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer_gateway_rules.json")


class FakeDashboard(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.hits = []
        self.slow = threading.Event()


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.hits.append((self.command, self.path, body, dict(self.headers)))
        if self.path.startswith("/api/slow"):
            self.server.slow.wait(5)
        if self.path.startswith("/api/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(3):
                self.wfile.write(b"data: %d\n\n" % i)
                self.wfile.flush()
            return
        out = json.dumps({"method": self.command, "path": self.path,
                          "body": body.decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(out)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _reply


def rules_with(**over):
    with open(RULES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    data.update(over)
    return G.Rules(data)


class GatewayCase(unittest.TestCase):
    rules_override = {"allow_post_paths": ["/api/patern_desk/ask", "/api/slow"]}

    def setUp(self):
        self.up = FakeDashboard()
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "gw.jsonl")
        self.gw = G.Gateway(("127.0.0.1", 0), ("127.0.0.1", self.up.server_address[1]),
                            rules_with(**self.rules_override), G.Ledger(self.log))
        threading.Thread(target=self.gw.serve_forever, daemon=True).start()

    def tearDown(self):
        self.up.slow.set()
        self.gw.shutdown()
        self.up.shutdown()
        self.gw.server_close()
        self.up.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.gw.server_address[1], timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def upstream_paths(self):
        return [h[1] for h in self.up.hits]


class TestReads(GatewayCase):
    def test_page_and_read_api_pass_through(self):
        for path in ("/", "/dashboard.html", "/api/market_now?x=1", "/api/aegis_status"):
            status, data = self.req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(json.loads(data)["path"], path)  # 쿼리까지 그대로
        self.assertEqual(self.upstream_paths(),
                         ["/", "/dashboard.html", "/api/market_now?x=1", "/api/aegis_status"])

    def test_head_passes(self):
        status, data = self.req("HEAD", "/api/aegis_status")
        self.assertEqual((status, data), (200, b""))

    def test_streaming_response_arrives_whole(self):
        status, data = self.req("GET", "/api/stream")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"data: 0\n\ndata: 1\n\ndata: 2\n\n")

    def test_static_file_with_action_word_passes(self):
        status, _ = self.req("GET", "/static/order_card.js")
        self.assertEqual(status, 200)

    def test_marks_upstream_request_as_viewer(self):
        self.req("GET", "/api/aegis_status")
        self.assertEqual(self.up.hits[0][3].get("X-Patern-Viewer"), "1")


class TestBlocks(GatewayCase):
    def assertBlocked(self, method, path, status=403, body=None, headers=None):
        got, data = self.req(method, path, body=body, headers=headers)
        allowed = status if isinstance(status, tuple) else (status,)
        self.assertIn(got, allowed, (method, path, data))
        self.assertEqual(self.up.hits, [], "차단된 요청이 대시보드까지 갔다: %s %s" % (method, path))

    def test_known_control_routes(self):
        for path in ("/api/refresh_pdd", "/api/pdd_diagnose", "/API/Refresh_PDD",
                     "/api/kickstart/aggregator", "/api/run", "/api/job/run", "/api/set_mode",
                     "/api/kis_token"):
            self.assertBlocked("GET", path)
            self.assertBlocked("POST", path)

    def test_post_not_in_allow_list(self):
        self.assertBlocked("POST", "/api/aegis_status", body=b"{}")

    def test_other_methods(self):
        for m in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertBlocked(m, "/api/aegis_status")

    def test_sensitive_and_dotfiles(self):
        for path in ("/.env", "/.git/config", "/api/secret", "/api/broker_api_key", "/api/apikey"):
            self.assertBlocked("GET", path)
        self.assertBlocked("GET", "/static/.env.js")  # 정적 확장자여도 deny_always 는 적용

    def test_path_tricks(self):
        for path in ("/api/x/../refresh_pdd", "/api/%2e%2e/refresh_pdd", "/api%2frefresh_pdd",
                     "/api/./aegis_status", "/api\\refresh_pdd", "http://127.0.0.1:8765/api/refresh_pdd"):
            self.assertBlocked("GET", path, status=400)
        # 최근 파이썬은 앞의 '//' 를 '/' 로 접어서 넘긴다 — 판정과 전달이 같은 경로를 쓰므로 어느 쪽이든 막힌다
        for path in ("//api/refresh_pdd", "//refresh_pdd/api/aegis_status"):
            self.assertBlocked("GET", path, status=(400, 403))

    def test_allow_list_is_full_match_not_prefix(self):
        self.assertBlocked("POST", "/api/patern_desk/ask/../../refresh_pdd", status=400)
        self.assertBlocked("POST", "/api/patern_desk/ask_and_refresh", body=b"{}")

    def test_websocket_upgrade(self):
        self.assertBlocked("GET", "/ws", headers={"Upgrade": "websocket", "Connection": "Upgrade"})


class TestAllowedPost(GatewayCase):
    rules_override = {"allow_post_paths": ["/api/patern_desk/ask", "/api/slow"],
                      "post_rate": {"concurrent": 1, "per_minute": 3}, "max_body_bytes": 100}

    def test_allowed_post_forwards_body(self):
        status, data = self.req("POST", "/api/patern_desk/ask", body=b'{"q":"x"}',
                                headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["body"], '{"q":"x"}')

    def test_per_minute_limit(self):
        codes = [self.req("POST", "/api/patern_desk/ask", body=b"{}")[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(len(self.up.hits), 3)

    def test_one_at_a_time(self):
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("a", self.req("POST", "/api/slow", body=b"{}")))
        t.start()
        for _ in range(50):
            if self.up.hits:
                break
            time.sleep(0.05)
        status, _ = self.req("POST", "/api/patern_desk/ask", body=b"{}")
        self.assertEqual(status, 429)
        self.up.slow.set()
        t.join(10)
        self.assertEqual(result["a"][0], 200)

    def test_body_too_large(self):
        status, _ = self.req("POST", "/api/patern_desk/ask", body=b"x" * 101)
        self.assertEqual(status, 413)
        self.assertEqual(self.up.hits, [])


class TestOps(GatewayCase):
    def test_upstream_down_is_502(self):
        self.up.shutdown()
        self.up.server_close()
        status, data = self.req("GET", "/api/aegis_status")
        self.assertEqual(status, 502)

    def test_health(self):
        status, data = self.req("GET", "/__viewer/health")
        body = json.loads(data)
        self.assertEqual((status, body["mode"], body["upstream_up"]), (200, "view_only", True))

    def test_health_counts_upstream_without_head_as_up(self):
        # 대시보드 서버가 HEAD 를 구현하지 않으면 501 — 그래도 살아 있다
        self.up.RequestHandlerClass = type("NoHead", (http.server.BaseHTTPRequestHandler,), {
            "do_GET": FakeHandler._reply, "log_message": FakeHandler.log_message})
        status, data = self.req("GET", "/__viewer/health")
        body = json.loads(data)
        self.assertEqual((body["upstream_up"], body["upstream_status"]), (True, 501))

    def test_health_reports_down(self):
        self.up.shutdown()
        self.up.server_close()
        body = json.loads(self.req("GET", "/__viewer/health")[1])
        self.assertFalse(body["upstream_up"])
        self.assertTrue(body["upstream_error"])

    def test_ledger_and_show_blocked(self):
        self.req("GET", "/api/aegis_status")
        self.req("POST", "/api/refresh_pdd")
        # 원장 줄은 응답을 보낸 뒤에 쓴다 — 클라이언트가 응답을 받은 시점엔 아직 없을 수 있다
        rows = []
        for _ in range(100):
            if os.path.exists(self.log):
                with open(self.log, encoding="utf-8") as f:
                    rows = [json.loads(l) for l in f]
            if len(rows) >= 2:
                break
            time.sleep(0.05)
        self.assertEqual([r["decision"] for r in rows], ["forwarded", "blocked"])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            G.show_blocked(self.log)
        self.assertIn("/api/refresh_pdd", buf.getvalue())

    def test_refuses_non_loopback_listen(self):
        out = {}
        t = threading.Thread(target=lambda: out.setdefault(
            "rc", G.main(["--listen", "0.0.0.0:0", "--log", self.log])), daemon=True)
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive(), "0.0.0.0 으로 열고 서버를 띄웠다")
        self.assertEqual(out.get("rc"), 2)


class TestShippedRules(unittest.TestCase):
    """배포되는 규칙 파일 자체를 고정한다 — POST 는 처음엔 아무것도 열려 있지 않아야 한다."""

    def test_no_post_open_by_default(self):
        with open(RULES_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["allow_post_paths"], [])

    def test_decide_default_rules(self):
        rules = rules_with()
        self.assertEqual(G.decide("POST", "/api/patern_desk/ask", {}, rules)[0], "block")
        self.assertEqual(G.decide("GET", "/api/refresh_pdd", {}, rules)[0], "block")
        self.assertEqual(G.decide("GET", "/api/strong_now", {}, rules)[0], "forward")


if __name__ == "__main__":
    unittest.main()
