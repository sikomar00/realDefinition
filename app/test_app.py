"""realDefinition 엔진과 서버 테스트.  실행: python app/test_app.py   (추가 설치 없음, 데이터는 임시 폴더에 쓴다)"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="pocdesk-test-")
os.environ["POC_DESK_DATA_DIR"] = str(Path(TMP) / "data")
os.environ["POC_DESK_REPORT_DIR"] = str(Path(TMP) / "reports")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import engine  # noqa: E402
import server  # noqa: E402

QUESTIONS = json.loads((engine.ROOT / "experiment" / "questions_A.json").read_text(encoding="utf-8"))


class FakeClient:
    """LLM 대신 정해진 답을 돌려주는 가짜 클라이언트 (네트워크 호출 없음)."""
    def __init__(self, text):
        self.text = text

    def chat(self, system, user, meta=None):
        FakeClient.last_user = user
        return {"text": self.text, "latency_s": 0.0, "prompt_tokens": None, "completion_tokens": None}


class Isolated(unittest.TestCase):
    """테스트마다 새 임시 폴더를 쓴다. 앞 테스트가 저장한 사전·기록이 다음 테스트에 새어 들어가지 않게 한다."""
    def setUp(self):
        super().setUp()
        self._dirs = (engine.DATA_DIR, engine.REPORT_DIR)
        base = Path(tempfile.mkdtemp(prefix="pd-case-", dir=TMP))
        engine.DATA_DIR, engine.REPORT_DIR = base / "data", base / "reports"

    def tearDown(self):
        engine.DATA_DIR, engine.REPORT_DIR = self._dirs
        super().tearDown()


class Interpretation(Isolated):
    def test_months(self):
        cases = {"1월과 2월 매출을 합치면 얼마야?": ["2026-01", "2026-02"], "1~2월 환불률은?": ["2026-01", "2026-02"],
                 "2~3월에 주문 하나당 평균 얼마?": ["2026-02", "2026-03"], "1분기 매출 합계는?": engine.VALID_MONTHS,
                 "2026년 3월 취소율": ["2026-03"], "매출이 얼마야?": [],
                 # 회귀: 세 달 이상 나열하거나 범위와 단독 월을 섞어도 뒤쪽 월이 빠지지 않아야 한다
                 "1월, 2월, 3월 매출": engine.VALID_MONTHS, "1월과 2월과 3월 매출": engine.VALID_MONTHS,
                 "1~2월과 3월 매출": engine.VALID_MONTHS, "1월과 3월 매출": ["2026-01", "2026-03"],
                 "1월부터 3월까지 매출": engine.VALID_MONTHS}
        for text, want in cases.items():
            self.assertEqual(engine.parse_months(text)["months"], want, text)
        self.assertEqual(engine.parse_months("4월 매출")["invalid"], ["2026-04"])

    def test_metrics(self):
        cases = {"2월에 얼마 벌었어?": ["net_revenue"], "1월 환불 반영한 매출은?": ["net_revenue"],
                 "2월에 판매한 것 중 환불로 나간 비중은?": ["refund_rate"], "3월에 주문 몇 건 들어왔어?": ["paid_orders"],
                 "2월 주문 중 취소된 비율은?": ["cancel_rate"], "2월 객단가는?": ["aov"], "재구매율은 얼마야?": []}
        for text, want in cases.items():
            self.assertEqual([c["key"] for c in engine.detect_metrics(text)], want, text)
        self.assertEqual(len(engine.detect_metrics("취소된 주문 몇 건이야?")), 2)  # 모호하면 사용자가 고르게 한다

    def test_intake_validation(self):
        with self.assertRaises(ValueError):
            engine.intake("   ")
        self.assertEqual(engine.intake("재구매율은?")["status"], "unknown")
        self.assertEqual(engine.intake("취소된 주문 몇 건이야?")["status"], "ambiguous")


class ReferenceMode(Isolated):
    def test_all_twenty_questions_match_gold(self):
        for q in QUESTIONS:
            r = engine.run_query(q["question"], q["metric"], q["months"], "reference")
            self.assertAlmostEqual(r["value"], float(q["gold_value"]), places=2, msg=q["id"])
            self.assertNotEqual(r["overall"], "ERROR", q["id"])

    def test_values_match_an_independent_python_recomputation(self):
        """위 테스트는 앱과 정답이 같은 SQL이라 SQL 자체의 오류를 못 잡는다. 파이썬으로 따로 다시 계산한 값과 비교한다."""
        import sqlite3
        sys.path.insert(0, str(engine.ROOT / "experiment"))
        from holdout import independent_check
        con = sqlite3.connect(f"file:{engine.R.DB}?mode=ro", uri=True)
        try:
            for q in QUESTIONS:
                r = engine.run_query(q["question"], q["metric"], q["months"], "reference")
                self.assertAlmostEqual(r["value"], float(independent_check(con, q["metric"], q["months"])),
                                       delta=0.011, msg=q["id"])
        finally:
            con.close()

    def test_duplicate_and_unsorted_months_are_normalized(self):
        """회귀: 같은 달이 두 번 들어와도 원장 합계가 두 번 더해져 정답이 경고로 바뀌면 안 된다."""
        r = engine.run_query("매출", "net_revenue", ["2026-02", "2026-02", "2026-01"], "reference")
        self.assertEqual((r["months"], r["overall"]), (["2026-01", "2026-02"], "PASS"))
        self.assertEqual(r["value"], 23304000 + 18287000)

    def test_reference_mode_runs_the_query_once_and_unit_is_set(self):
        calls = []
        orig = engine.R.execute_readonly
        engine.R.execute_readonly = lambda sql, *a, **k: (calls.append(sql), orig(sql, *a, **k))[1]
        try:
            r = engine.run_query("2월 환불률", "refund_rate", ["2026-02"], "reference")
        finally:
            engine.R.execute_readonly = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual((r["unit"], r["reference_value"]), ("%", r["value"]))

    def test_revenue_is_reconciled_with_ledger_exactly(self):
        r = engine.run_query("2월 매출", "net_revenue", ["2026-02"], "reference")
        self.assertEqual(r["value"], 18287000)
        self.assertEqual(r["overall"], "PASS")
        self.assertEqual(r["checks"][0]["name"], "원장 대사")

    def test_other_metrics_have_no_ledger_so_not_marked_verified(self):
        r = engine.run_query("1분기 환불률", "refund_rate", engine.VALID_MONTHS, "reference")
        self.assertEqual(r["overall"], "INFO")  # '통과'가 아니다
        self.assertEqual(r["unit"], "%")

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            engine.run_query("x", "net_revenue", [], "reference")
        with self.assertRaises(ValueError):
            engine.run_query("x", "net_revenue", ["2026-04"], "reference")
        with self.assertRaises(ValueError):
            engine.run_query("x", "no_such_metric", ["2026-01"], "reference")  # 사전에 없는 지표는 거부
        with self.assertRaises(ValueError):
            engine.run_query("x", "net_revenue", ["2026-01"], "magic")


class Dictionary(Isolated):
    def test_defined_custom_metric_is_recognized_in_questions(self):
        """회귀: 정의해 둔 지표를 이름이나 별칭으로 다시 물으면 '사전에 없음'이 아니라 그 지표로 인식해야 한다."""
        self.assertEqual(engine.intake("재구매율은 얼마야?")["status"], "unknown")
        m = engine.save_definition("재구매율", ["리피트율"], "재구매 고객 ÷ 전체", "취소 제외", "결제일", "%")
        for q in ("재구매율은 얼마야?", "리피트율 알려줘", "재 구매 율은?"):
            got = engine.intake(q)
            self.assertEqual((got["status"], got["candidates"][0]["key"]), ("ok", m["key"]), q)
        self.assertEqual(engine.intake("2월 매출은?")["candidates"][0]["key"], "net_revenue")  # 내장 지표는 그대로

    def test_custom_metric_display_unit_and_report_cell_escaping(self):
        m = engine.save_definition("신규 X", [], "A | B\n÷ C", "없음", "결제일", "%, 소수 둘째 자리 반올림")
        self.assertEqual(engine.short_unit(m["unit"]), "%")
        self.assertEqual(engine._fmt_value(m["key"], 42.5, "%"), "42.50%")
        self.assertEqual(engine._fmt_value("custom_y", 7.0, "건"), "7건")
        self.assertEqual(engine._fmt_value("custom_y", 7.25, "점"), "7.25점")
        self.assertEqual(engine._fmt_value("net_revenue", 18287000.0), "18,287,000원")  # 내장 지표 표기는 그대로
        orig = (engine._make_client, engine.llm_key_status)
        engine.llm_key_status = lambda: True
        engine._make_client = lambda model: FakeClient("```sql\nSELECT 42.5\n```")
        try:
            run = engine.run_query("신규 X는?", m["key"], ["2026-01"], "ai", "stub")
        finally:
            engine._make_client, engine.llm_key_status = orig
        self.assertEqual(run["unit"], "%")
        rep = engine.make_report(run["id"])["markdown"]
        self.assertIn("**42.50%**", rep)
        row = [l for l in rep.splitlines() if l.startswith("| 계산식")][0]
        self.assertEqual(row, "| 계산식 | A " + chr(92) + "| B ÷ C |")  # '|' 이스케이프, 줄바꿈은 공백으로

    def test_corrupt_dictionary_file_does_not_break_the_app(self):
        engine.save_definition("깨질 지표", [], "a", "b", "c", "d")
        engine._custom_path().write_text("{깨진 파일", encoding="utf-8")
        self.assertEqual(len(engine.dictionary()), 5)  # 내장 지표 5개는 계속 쓸 수 있다
        self.assertEqual(len(list(engine.DATA_DIR.glob("custom_kpis.corrupt-*.json"))), 1)  # 깨진 파일은 보관됨

    def test_concurrent_saves_do_not_lose_updates_and_leave_no_temp_file(self):
        names = [f"동시 지표 {i}" for i in range(12)]
        errors = []

        def save(n):
            try:
                engine.save_definition(n, [], "a", "b", "c", "d")
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=save, args=(n,)) for n in names]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        saved = {m["name"] for m in engine.dictionary() if m["source"] == "custom"}
        self.assertEqual(saved, set(names))
        self.assertEqual(list(engine.DATA_DIR.glob("*.tmp")), [])

    def test_history_skips_broken_lines_and_finds_old_runs(self):
        r1 = engine.run_query("a", "net_revenue", ["2026-01"], "reference")
        with engine._history_path().open("a", encoding="utf-8") as f:
            f.write('{"id": "깨진 줄\n')  # 쓰다 만 줄
        r2 = engine.run_query("b", "net_revenue", ["2026-02"], "reference")
        self.assertEqual([r["id"] for r in engine.history()], [r2["id"], r1["id"]])
        self.assertEqual(engine.get_run(r1["id"])["question"], "a")
        self.assertEqual(len(engine.history(limit=1)), 1)

    def test_four_fields_required_and_versioning(self):
        with self.assertRaises(ValueError) as ctx:
            engine.save_definition("재구매율", [], "a", "", "c", "")
        self.assertIn("포함·제외", str(ctx.exception))
        with self.assertRaises(ValueError):
            engine.save_definition("매출", [], "a", "b", "c", "d")  # 내장 지표 이름
        m1 = engine.save_definition("재구매율", ["리피트율"], "재구매 고객 ÷ 전체 고객", "취소 제외", "결제일", "%")
        m2 = engine.save_definition("재구매율", [], "재구매 고객 ÷ 결제 고객", "취소 제외", "결제일", "%")
        self.assertEqual((m1["version"], m2["version"]), ("v1", "v2"))
        stored = [m for m in engine.dictionary() if m["key"] == m1["key"]][0]
        self.assertEqual(len(stored["history"]), 1)
        self.assertFalse(stored["has_reference"])
        with self.assertRaises(ValueError):  # 기준 계산식이 없는 지표는 계산식 모드 불가
            engine.run_query("재구매율", m1["key"], ["2026-01"], "reference")


class AiMode(Isolated):
    def setUp(self):
        super().setUp()
        self._orig = (engine._make_client, engine.llm_key_status)
        engine.llm_key_status = lambda: True

    def tearDown(self):
        engine._make_client, engine.llm_key_status = self._orig
        super().tearDown()

    def _ai(self, sql_text, metric="net_revenue", months=("2026-02",), q="2월 매출"):
        engine._make_client = lambda model: FakeClient(sql_text)
        return engine.run_query(q, metric, list(months), "ai", "stub-model")

    def test_correct_sql_passes_both_checks(self):
        gold = engine.gold_sql("net_revenue", ["2026-02"])
        r = self._ai(f"```sql\n{gold}\n```")
        self.assertEqual(r["overall"], "PASS")
        self.assertEqual(sorted(c["name"] for c in r["checks"]), ["기준 계산식 대조", "원장 대사"])
        self.assertIn("확정한 기간: 2026-02", FakeClient.last_user)  # 확정한 기간이 프롬프트에 전달됨

    def test_prompt_never_contains_ledger_or_rows(self):
        self._ai("```sql\nSELECT 1\n```")
        self.assertNotIn("ledger", FakeClient.last_user)
        self.assertNotIn("CREATE TABLE ledger", FakeClient.last_user)

    def test_wrong_sql_is_flagged(self):
        r = self._ai("```sql\nSELECT SUM(total_amount) FROM orders\n```")
        self.assertEqual(r["overall"], "FLAG")
        self.assertTrue(any(c["status"] == "FLAG" for c in r["checks"]))

    def test_unsafe_sql_is_blocked_and_logged(self):
        for text, why in (("```sql\nDELETE FROM orders\n```", "SELECT로 시작하지 않음"),
                          ("```sql\nSELECT net_revenue_ex_vat FROM ledger\n```", "정답표")):
            r = self._ai(text)
            self.assertEqual((r["status"], r["overall"]), ("BLOCKED", "ERROR"))
            self.assertIn(why, r["error"])
        self.assertTrue(any(h["status"] == "BLOCKED" for h in engine.history()))

    def test_custom_metric_is_never_marked_verified(self):
        m = engine.save_definition("신규 지표 X", [], "무언가", "없음", "결제일", "건")
        r = self._ai("```sql\nSELECT 42\n```", metric=m["key"], months=("2026-01",), q="X는?")
        self.assertEqual(r["overall"], "UNVERIFIED")
        self.assertNotIn("PASS", [c["status"] for c in r["checks"]])
        self.assertIn("신규 지표 X", FakeClient.last_user)  # 사용자 정의가 사전 텍스트로 전달됨

    def test_missing_key_gives_clear_error(self):
        engine.llm_key_status = lambda: False
        r = engine.run_query("2월 매출", "net_revenue", ["2026-02"], "ai", "m")
        self.assertEqual(r["overall"], "ERROR")
        self.assertIn("NVIDIA_API_KEY", r["error"])
        self.assertNotIn("nvapi", r["error"])


class Report(Isolated):
    def test_report_contains_sections(self):
        r = engine.run_query("1분기 환불률은?", "refund_rate", engine.VALID_MONTHS, "reference")
        rep = engine.make_report(r["id"])
        for needle in ("# PoC 리포트", "## 결과", "## 대사(검증) 결과", "## 사용한 정의", "```sql", "합성(가짜)"):
            self.assertIn(needle, rep["markdown"])
        self.assertTrue((engine.REPORT_DIR / rep["filename"]).exists())
        with self.assertRaises(ValueError):
            engine.make_report("없는-id")


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data

    def post(self, path, obj, **kw):
        h = {"Content-Type": "application/json", "X-PocDesk": "1"}
        h.update(kw.pop("headers", {}))
        return self.req("POST", path, json.dumps(obj).encode(), h, **kw)

    def test_pages_and_security_headers(self):
        s, h, d = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn("realDefinition", d.decode())
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(self.req("GET", "/app.js")[0], 200)
        self.assertEqual(self.req("GET", "/style.css")[0], 200)
        self.assertEqual(self.req("GET", "/../engine.py")[0], 404)
        self.assertEqual(self.req("GET", "/%2e%2e/engine.py")[0], 404)

    def test_rejects_foreign_host_and_origin(self):
        self.assertEqual(self.req("GET", "/api/status", host="evil.example.com")[0], 403)  # DNS 리바인딩 방어
        self.assertEqual(self.post("/api/intake", {"question": "2월 매출"}, headers={"Origin": "http://evil.example.com"})[0], 403)

    def test_post_requires_json_and_custom_header(self):
        self.assertEqual(self.req("POST", "/api/intake", b'{"question":"x"}', {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.req("POST", "/api/intake", b"question=x", {"X-PocDesk": "1", "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", "/api/intake", b"{bad", {"X-PocDesk": "1", "Content-Type": "application/json"})[0], 400)
        big = b'{"question":"' + b"x" * 200_000 + b'"}'
        self.assertEqual(self.req("POST", "/api/intake", big, {"X-PocDesk": "1", "Content-Type": "application/json"})[0], 413)

    def test_rejections_with_body_arrive_reliably(self):
        """회귀 테스트: 거절하는 POST도 본문을 먼저 읽어야 클라이언트가 간헐적으로 연결이 끊기지 않는다."""
        body = json.dumps({"question": "x" * 5000}).encode()
        for _ in range(40):
            self.assertEqual(self.req("POST", "/api/intake", body, {"Content-Type": "application/json"})[0], 403)
            self.assertEqual(self.req("POST", "/api/intake", body, {"Content-Type": "text/plain", "X-PocDesk": "1"})[0], 415)
            self.assertEqual(self.post("/api/intake", {"question": "x"}, headers={"Origin": "http://evil.example.com"})[0], 403)

    def test_stalled_body_does_not_hold_a_thread_forever(self):
        """회귀: Content-Length만큼 보내지 않고 멈춘 클라이언트가 핸들러 스레드를 영원히 붙잡으면 안 된다."""
        import contextlib
        import io
        import socket
        import time
        old = server.Handler.timeout
        self.assertTrue(old is not None and 0 < old <= 60, "기본 소켓 시간 제한이 있어야 한다")
        server.Handler.timeout = 1  # 테스트를 빠르게 하려고 줄인다. 새 연결부터 적용된다
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                head = (f"POST /api/intake HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                        "X-PocDesk: 1\r\nContent-Length: 5000\r\n\r\n").encode()
                s.sendall(head + b'{"question"')
                t0 = time.time()
                while s.recv(4096):  # 서버가 연결을 닫을 때까지 읽는다
                    pass
                s.close()
                time.sleep(0.3)  # 서버 스레드가 정리를 마칠 시간
            self.assertLess(time.time() - t0, 6)
            self.assertNotIn("Traceback", err.getvalue())  # 예외가 새어 나와 오류 추적이 찍히면 안 된다
            self.assertEqual(self.req("GET", "/api/status")[0], 200)  # 다른 요청은 계속 처리된다
        finally:
            server.Handler.timeout = old

    def test_log_message_keeps_errors_and_drops_normal_requests(self):
        import contextlib
        import io
        h = server.Handler.__new__(server.Handler)
        h.log_date_time_string = lambda: "T"

        def out(fmt, *args):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                h.log_message(fmt, *args)
            return buf.getvalue()
        self.assertEqual(out('"%s" %s %s', "GET / HTTP/1.1", "200", "-"), "")
        self.assertIn("404", out('"%s" %s %s', "GET /x HTTP/1.1", "404", "-"))
        self.assertIn("Bad request", out("code %d, message %s", 400, "Bad request syntax"))  # send_error 형식도 남긴다

    def test_end_to_end_flow(self):
        s, _, d = self.post("/api/intake", {"question": "2월에 얼마 벌었어?"})
        intake = json.loads(d)
        self.assertEqual((s, intake["candidates"][0]["key"], intake["months"]), (200, "net_revenue", ["2026-02"]))
        s, _, d = self.post("/api/run", {"question": "2월에 얼마 벌었어?", "metric_key": "net_revenue",
                                          "months": ["2026-02"], "mode": "reference"})
        run = json.loads(d)
        self.assertEqual((s, run["value"], run["overall"]), (200, 18287000, "PASS"))
        s, _, d = self.post("/api/report", {"run_id": run["id"]})
        self.assertEqual(s, 200)
        self.assertIn("18,287,000원", json.loads(d)["markdown"])
        s, _, d = self.req("GET", "/api/history")
        self.assertTrue(json.loads(d)["runs"])
        s, _, d = self.post("/api/run", {"question": "x", "metric_key": "nope", "months": ["2026-01"]})
        self.assertEqual(s, 400)
        self.assertIn("사전에 없는", json.loads(d)["error"])
        s, _, d = self.post("/api/definition", {"name": "Z", "formula": "a"})
        self.assertEqual(s, 400)

    def test_compare_endpoint(self):
        s, _, d = self.post("/api/compare", {"metric_key": "net_revenue", "months": ["2026-01", "2026-02", "2026-03"]})
        self.assertEqual(s, 200)
        res = json.loads(d)
        self.assertEqual(len(res["rows"]), 6)
        self.assertTrue(res["official_matches_ledger"])
        self.assertEqual(self.post("/api/compare", {"metric_key": "aov", "months": ["2026-01"]})[0], 400)
        self.assertEqual(self.req("POST", "/api/compare", b"{}", {"Content-Type": "application/json"})[0], 403)  # 사용자 지정 헤더 없이는 거절

    def test_status_does_not_leak_key(self):
        os.environ["NVIDIA_API_KEY"] = "dummy-secret-value-for-test"
        try:
            s, _, d = self.req("GET", "/api/status")
            self.assertNotIn("secret", d.decode())
            self.assertTrue(json.loads(d)["llm_key"])
        finally:
            del os.environ["NVIDIA_API_KEY"]


class DefinitionComparison(Isolated):
    """정의 비교: 같은 '매출' 질문을 정의 6가지로 계산해 나란히 보여 준다."""
    Q = ["2026-01", "2026-02", "2026-03"]

    def test_six_definitions_and_the_official_one_matches_the_ledger_exactly(self):
        r = engine.compare_definitions("net_revenue", self.Q)
        self.assertEqual([x["key"] for x in r["rows"]], ["V0", "V1", "V2", "V3", "V4", "V5"])
        official = [x for x in r["rows"] if x["official"]]
        self.assertEqual([x["key"] for x in official], ["V5"])
        self.assertTrue(r["official_matches_ledger"])
        self.assertEqual(round(r["official_total"]), round(r["ledger_total"]))
        self.assertEqual(official[0]["diff"], 0)

    def test_definitions_really_change_the_number(self):
        r = engine.compare_definitions("net_revenue", self.Q)
        totals = {x["key"]: x["total"] for x in r["rows"]}
        self.assertGreater(totals["V0"], totals["V1"])  # 주문 총액 > 결제 완료 총액
        self.assertGreater(totals["V1"], totals["V2"])  # 부가세·배송비를 빼면 줄어든다
        self.assertGreater(totals["V2"], totals["V5"])  # 취소·환불을 반영하면 줄어든다
        self.assertAlmostEqual(r["spread"], totals["V0"] - min(totals.values()), places=6)
        self.assertGreater(r["spread_pct"], 10)  # 정의만 달라도 1할 넘게 차이 난다(합성 데이터 기준)

    def test_months_are_respected_and_sum_of_months_equals_total(self):
        one = engine.compare_definitions("net_revenue", ["2026-02"])
        self.assertEqual(one["months"], ["2026-02"])
        for x in one["rows"]:
            self.assertEqual(list(x["by_month"]), ["2026-02"])
            self.assertAlmostEqual(x["total"], x["by_month"]["2026-02"])
        three = engine.compare_definitions("net_revenue", self.Q + ["2026-01"])  # 중복은 한 번만
        self.assertEqual(three["months"], self.Q)

    def test_only_revenue_has_variants_and_bad_input_is_rejected(self):
        for bad in ("refund_rate", "nope", None):
            with self.assertRaises(ValueError):
                engine.compare_definitions(bad, self.Q)
        with self.assertRaises(ValueError):
            engine.compare_definitions("net_revenue", ["2026-09"])

    def test_postgres_translation_only_changes_the_date_function(self):
        import compare
        from gold import load_variants
        for name, sql in load_variants().items():
            pg = compare.to_postgres(sql)
            self.assertNotIn("strftime", pg, name)
            self.assertIn("to_char(", pg, name)
            self.assertEqual(sql.count("strftime("), pg.count("to_char("), name)
            self.assertEqual(sql.replace("strftime('%Y-%m', ", "").count("FROM"), pg.replace("to_char(", "").count("FROM"), name)


class SupabaseBackend(unittest.TestCase):
    """Supabase 모드의 규칙을 가짜 연결로 시험한다(네트워크·비밀번호 불필요). 로그인 검증과 DB 호출은 대체한다."""
    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    req = Http.req
    post = Http.post

    OWNER = {"id": "00000000-0000-0000-0000-000000000001", "email": "owner@example.com"}

    def setUp(self):
        self._saved = (engine.BACKEND, db.verify_token, engine.history, engine.run_query, engine.get_run, engine.make_report,
                       server.SUPABASE_ORIGIN)
        engine.BACKEND = "supabase"
        os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
        os.environ.setdefault("SUPABASE_ANON_KEY", "anon-public-key")
        db.verify_token = lambda tok: self.OWNER if tok == "good-token" else None
        self.calls = []
        engine.history = lambda limit=50, user_id=None: self.calls.append(("history", user_id)) or [{"id": "r1"}]
        engine.run_query = lambda q, mk, months, mode="reference", model=None, user_id=None: self.calls.append(("run", user_id)) or {"id": "r2"}
        engine.make_report = lambda run_id, user_id=None: self.calls.append(("report", user_id)) or {"filename": "x.md", "path": "x", "markdown": "x"}

    def tearDown(self):
        (engine.BACKEND, db.verify_token, engine.history, engine.run_query, engine.get_run, engine.make_report,
         server.SUPABASE_ORIGIN) = self._saved

    AUTH = {"Authorization": "Bearer good-token"}

    def test_history_and_report_need_login(self):
        self.assertEqual(self.req("GET", "/api/history")[0], 401)
        self.assertEqual(self.req("GET", "/api/history", headers={"Authorization": "Bearer bad-token"})[0], 401)
        self.assertEqual(self.post("/api/report", {"run_id": "x"})[0], 401)
        s, _, d = self.req("GET", "/api/history", headers=self.AUTH)
        self.assertEqual(s, 200)
        self.assertEqual(self.calls, [("history", self.OWNER["id"])])  # 본인 번호로만 조회한다

    def test_saving_a_custom_definition_needs_login(self):
        body = {"name": "재구매율", "formula": "a", "include_exclude": "b", "period_basis": "c", "unit": "%"}
        self.assertEqual(self.post("/api/definition", body)[0], 401)

    def test_public_demo_can_run_without_login_and_logged_in_run_carries_user_id(self):
        body = {"question": "q", "metric_key": "net_revenue", "months": ["2026-01"], "mode": "reference"}
        self.assertEqual(self.post("/api/run", body)[0], 200)
        self.assertEqual(self.post("/api/run", body, headers=self.AUTH)[0], 200)
        self.assertEqual(self.calls, [("run", None), ("run", self.OWNER["id"])])

    def test_status_reports_backend_and_never_leaks_secrets(self):
        s, _, d = self.req("GET", "/api/status")
        st = json.loads(d)
        self.assertEqual(st["backend"], "supabase")
        self.assertIsNone(st["user"])
        self.assertEqual(set(st["auth"]), {"url", "public_key"})  # 공개 값만: 비밀번호·서비스 키는 화면으로 나가지 않는다
        for secret in ("PASSWORD", "service_role"):
            self.assertNotIn(secret, d.decode())
        s, _, d = self.req("GET", "/api/status", headers=self.AUTH)
        self.assertEqual(json.loads(d)["user"], {"email": "owner@example.com"})


class PublicDemo(unittest.TestCase):
    """인터넷에 공개한 데모(POC_DESK_PUBLIC=1)의 규칙. 내 PC 전용 규칙과 다르게 동작해야 한다."""
    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    req = Http.req
    post = Http.post

    def setUp(self):
        self._saved = (server.PUBLIC, engine.PUBLIC, server.RATE_LIMIT, db.PERSIST)
        server.PUBLIC = engine.PUBLIC = True
        server._hits.clear()

    def tearDown(self):
        server.PUBLIC, engine.PUBLIC, server.RATE_LIMIT, db.PERSIST = self._saved
        server._hits.clear()

    DOMAIN = "demo.example.app"

    def test_any_domain_is_served_but_only_in_public_mode(self):
        s, h, d = self.req("GET", "/api/status", host=self.DOMAIN)
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)["public"])
        server.PUBLIC = False
        self.assertEqual(self.req("GET", "/api/status", host=self.DOMAIN)[0], 403)  # 내 PC 모드에서는 여전히 막는다

    def test_writes_must_come_from_the_same_origin(self):
        body = {"question": "2월 매출은?"}
        ok = self.post("/api/intake", body, host=self.DOMAIN, headers={"Origin": f"https://{self.DOMAIN}"})
        self.assertEqual(ok[0], 200)
        evil = self.post("/api/intake", body, host=self.DOMAIN, headers={"Origin": "https://evil.example"})
        self.assertEqual(evil[0], 403)
        self.assertEqual(self.post("/api/intake", body, host=self.DOMAIN)[0], 200)  # Origin 이 없는 요청(서버 간 호출)은 헤더 규칙으로 막는다
        self.assertEqual(self.req("POST", "/api/intake", b"{}", {"Content-Type": "application/json"}, host=self.DOMAIN)[0], 403)

    def test_saving_definitions_is_off(self):
        body = {"name": "x", "formula": "a", "include_exclude": "b", "period_basis": "c", "unit": "%"}
        s, _, d = self.post("/api/definition", body, host=self.DOMAIN)
        self.assertEqual(s, 403)
        self.assertIn("공개 데모", json.loads(d)["error"])

    def test_ai_mode_is_only_for_the_logged_in_owner_in_public_mode(self):
        os.environ["NVIDIA_API_KEY"] = "dummy-key-for-test"
        saved = (engine.BACKEND, db.verify_token, engine.run_query)
        calls = []
        engine.BACKEND = "supabase"
        os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
        os.environ.setdefault("SUPABASE_ANON_KEY", "anon-public-key")
        db.verify_token = lambda tok: {"id": "u1", "email": "o@example.com"} if tok == "good" else None
        engine.run_query = lambda q, mk, months, mode="reference", model=None, user_id=None: calls.append((mode, user_id)) or {"id": "r"}
        try:
            st = json.loads(self.req("GET", "/api/status", host=self.DOMAIN)[2])
            self.assertFalse(st["llm_key"])  # 키가 있어도 비로그인에게는 꺼져 있다
            self.assertTrue(st["ai_login_required"])
            st = json.loads(self.req("GET", "/api/status", headers={"Authorization": "Bearer good"}, host=self.DOMAIN)[2])
            self.assertTrue(st["llm_key"])
            self.assertFalse(st["ai_login_required"])
            body = {"question": "q", "metric_key": "net_revenue", "months": ["2026-01"], "mode": "ai"}
            self.assertEqual(self.post("/api/run", body, host=self.DOMAIN)[0], 401)
            self.assertEqual(calls, [])  # 엔진까지 가지 않는다
            self.assertEqual(self.post("/api/run", body, host=self.DOMAIN, headers={"Authorization": "Bearer good"})[0], 200)
            self.assertEqual(calls, [("ai", "u1")])
            body["mode"] = "reference"  # 계산식 모드는 로그인 없이 된다
            self.assertEqual(self.post("/api/run", body, host=self.DOMAIN)[0], 200)
            server.PUBLIC = engine.PUBLIC = False  # 내 PC 모드는 키만 있으면 로그인 없이 쓴다
            self.assertTrue(json.loads(self.req("GET", "/api/status", host=f"127.0.0.1:{self.port}")[2])["llm_key"])
        finally:
            engine.BACKEND, db.verify_token, engine.run_query = saved
            del os.environ["NVIDIA_API_KEY"]

    def test_hsts_and_security_headers_in_public_mode(self):
        _, h, _ = self.req("GET", "/", host=self.DOMAIN)
        lower = {k.lower(): v for k, v in h.items()}
        self.assertIn("strict-transport-security", lower)
        self.assertIn("default-src 'self'", lower["content-security-policy"])

    def test_vercel_rewritten_paths_are_restored(self):
        """Vercel 은 모든 요청을 /api/index?__vp=원래경로 로 넘긴다. 원래 주소의 화면·API 로 동작해야 한다."""
        s, _, d = self.req("GET", "/api/index?__vp=api/status", host=self.DOMAIN)
        self.assertEqual(s, 200)
        self.assertIn("backend", json.loads(d))
        s, h, d = self.req("GET", "/api/index?__vp=", host=self.DOMAIN)  # 첫 화면
        self.assertEqual(s, 200)
        self.assertIn(b"<html", d.lower())
        s, h, d = self.req("GET", "/api/index?__vp=app.js", host=self.DOMAIN)  # 정적 파일
        self.assertEqual(s, 200)
        self.assertIn("javascript", {k.lower(): v for k, v in h.items()}["content-type"])
        body = {"question": "2월 매출은?"}
        self.assertEqual(self.post("/api/index?__vp=api/intake", body, host=self.DOMAIN)[0], 200)  # POST 도 복원
        self.assertEqual(self.req("GET", "/api/index?__vp=api/index", host=self.DOMAIN)[0], 404)  # 자기 자신을 가리켜도 돌지 않는다
        server.PUBLIC = False
        self.assertEqual(self.req("GET", "/api/index?__vp=api/status", host=f"127.0.0.1:{self.port}")[0], 404)  # 내 PC 모드에서는 복원하지 않는다

    def test_rate_limit(self):
        server.RATE_LIMIT = 3
        codes = [self.req("GET", "/api/status", host=self.DOMAIN)[0] for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_anonymous_runs_are_not_written_to_the_database(self):
        saved = (engine.BACKEND, db.record_run)
        calls = []
        engine.BACKEND = "supabase"
        db.record_run = lambda rec, user_id=None: calls.append(user_id)
        try:
            engine._save_run({"id": "x"}, None)
            self.assertEqual(calls, [])  # 비로그인(공개) 실행은 누구나 DB에 행을 쌓지 못하게 기록하지 않는다
            engine._save_run({"id": "y"}, "user-1")
            self.assertEqual(calls, ["user-1"])
            engine.PUBLIC = False
            engine._save_run({"id": "z"}, None)  # 내 PC 모드는 비로그인도 기록한다
            self.assertEqual(calls, ["user-1", None])
        finally:
            engine.BACKEND, db.record_run = saved

    def test_reader_always_uses_session_mode_and_service_uses_the_configured_port(self):
        """읽기 전용 계산 역할은 SET search_path 가 필요해 항상 세션 모드(5432). 서비스 역할은 설정한 포트(서버리스에서는 6543)."""
        ports, sent = [], []

        class FakeConn:
            def run(self, stmt, **kw):
                sent.append(stmt)
                return []

            def close(self):
                pass

        saved = (db.POOLER_PORT, db._connect)
        db._connect = lambda role, pw, port: ports.append((role, port)) or FakeConn()
        try:
            svc = db._Session("app_service", "PW", port=6543)
            svc._open()
            rd = db._Session("poc_reader_demo", "PW", search_path="demo")
            rd._open()
            self.assertEqual(ports, [("app_service", 6543), ("poc_reader_demo", 5432)])
            self.assertEqual(sum("search_path" in x for x in sent), 1)  # 읽기 전용 역할에만 보낸다
            self.assertEqual(db._reader.port or db.SESSION_PORT, 5432)
            self.assertIsNone(db._reader.port)
        finally:
            db.POOLER_PORT, db._connect = saved

    def test_connections_are_closed_per_request_when_not_persistent(self):
        class FakeConn:
            closed = False

            def run(self, stmt, **kw):
                return [[1]]

            def close(self):
                FakeConn.closed = True

        for persist, expect_closed in ((False, True), (True, False)):
            FakeConn.closed = False
            db.PERSIST = persist
            sess = db._Session("r", "PW")
            sess.conn = FakeConn()
            self.assertEqual(sess.run("select 1"), [[1]])
            self.assertEqual(FakeConn.closed, expect_closed, persist)
            self.assertEqual(sess.conn is None, expect_closed, persist)


class DatabaseDialects(unittest.TestCase):
    def test_default_backend_is_local_sqlite(self):
        self.assertEqual(engine.BACKEND, "sqlite")
        self.assertFalse(db.enabled())

    def test_postgres_reference_sql_uses_postgres_syntax_for_all_metrics(self):
        for metric in engine.UNIT_LABEL:
            sql = db.gold_sql(metric, ["2026-01", "2026-02"])
            self.assertNotIn("strftime", sql, metric)
            self.assertIn("'2026-01','2026-02'", sql)
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"))
        self.assertIn("::int", db.gold_sql("cancel_rate", ["2026-01"]))  # SQLite 의 SUM(조건) 은 Postgres 에서 쓸 수 없다
        with self.assertRaises(ValueError):
            db.gold_sql("unknown", ["2026-01"])

    def test_postgres_and_sqlite_reference_sql_define_the_same_things(self):
        """같은 지표는 같은 월 조건·같은 환불 계산식을 쓴다(문법만 다르다). 값 비교는 Supabase 연결 시험에서 한다."""
        import gold as G
        for metric in engine.UNIT_LABEL:
            a, b = db.gold_sql(metric, ["2026-03"]), G.gold_sql(metric, ["2026-03"])
            for shared in ("o.status='PAID'", "o.status='CANCELLED'", "r.refund_amount * 100 / 110", "'FULL'"):
                self.assertEqual(shared in a, shared in b, (metric, shared))

    def test_history_in_sqlite_mode_ignores_user_id(self):
        self.assertEqual(engine.history(5, user_id="anyone"), engine.history(5))

    def test_dotenv_does_not_override_existing_environment(self):
        path = Path(TMP) / "x.env"
        path.write_text("POCDESK_T1=from-file\nPOCDESK_T2=only-file\n# 주석\n\n", encoding="utf-8")
        os.environ["POCDESK_T1"] = "from-env"
        try:
            db.load_dotenv(path)
            self.assertEqual(os.environ["POCDESK_T1"], "from-env")
            self.assertEqual(os.environ["POCDESK_T2"], "only-file")
        finally:
            os.environ.pop("POCDESK_T1", None)
            os.environ.pop("POCDESK_T2", None)

    def test_database_errors_are_shown_in_plain_language_without_internal_details(self):
        timeout = db.friendly_error(Exception({"S": "ERROR", "C": "57014", "M": "canceling statement", "F": "postgres.c"}))
        denied = db.friendly_error(Exception({"C": "42501", "M": "permission denied for schema app", "F": "aclchk.c"}))
        syntax = db.friendly_error(Exception({"C": "42601", "M": 'syntax error at or near "x"', "F": "scan.l"}))
        other = db.friendly_error(Exception({"C": "XX000", "M": "boom"}))
        self.assertIn("5초", timeout)
        self.assertIn("권한", denied)
        self.assertTrue(syntax.startswith("SQL 문법 오류:"))
        self.assertEqual(other, "데이터베이스 오류: boom")
        for text in (timeout, denied, syntax, other):
            self.assertNotIn("aclchk", text)
            self.assertNotIn("'S':", text)  # 내부 사전(dict) 모양이 그대로 나가지 않는다
        self.assertIn("연결", db.friendly_error(ConnectionError("x")))
        self.assertIsNone(db.friendly_error(ValueError("not a db error")))

    def test_timestamps_are_korean_time_even_when_the_server_runs_in_utc(self):
        from datetime import datetime, timedelta, timezone
        utc = datetime(2026, 10, 20, 6, 48, 0, tzinfo=timezone.utc)
        self.assertEqual(utc.astimezone(db.KST).isoformat(timespec="seconds"), "2026-10-20T15:48:00+09:00")
        self.assertEqual(db._aware("2026-10-20T15:48:00+09:00").utcoffset(), timedelta(hours=9))
        self.assertEqual(db._aware("2026-10-20T15:48:00").utcoffset(), timedelta(hours=9))  # 옛 기록(시간대 없음)은 한국 시간으로 본다
        rec = engine.run_query("시각", "net_revenue", ["2026-01"], "reference")
        self.assertTrue(rec["ts"].endswith("+09:00"), rec["ts"])
        self.assertEqual(len(rec["id"].split("-")[0]), 8)

    def test_token_with_wrong_shape_is_rejected_without_network(self):
        for bad in (None, "", "abc", "a.b", "a.b.c.d"):
            self.assertIsNone(db.verify_token(bad))


class PublicKey(unittest.TestCase):
    """INT-010: 새 publishable 키로 옮겨도 로그인 확인이 깨지지 않게 한다. 옛 anon 키 이름은 롤백용으로 계속 읽는다."""
    ENV = ("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.ENV}

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_new_key_wins_and_old_name_still_works(self):
        self.assertIsNone(db.public_key())
        os.environ["SUPABASE_ANON_KEY"] = "legacy-anon"
        self.assertEqual(db.public_key(), "legacy-anon")           # 환경변수를 지우면 옛 키로 복귀(롤백)
        os.environ["SUPABASE_PUBLISHABLE_KEY"] = "sb_publishable_test"
        self.assertEqual(db.public_key(), "sb_publishable_test")   # 새 키가 우선

    def test_verify_token_sends_the_key_only_in_apikey_header(self):
        """새 키는 JWT가 아니라 Authorization: Bearer 에 넣으면 거절된다. 사용자 토큰만 Bearer 에 실려야 한다."""
        import unittest.mock
        os.environ["SUPABASE_PUBLISHABLE_KEY"] = "sb_publishable_test"
        seen = []

        class Resp:
            def read(self):
                return b"{}"  # id 가 없으니 DB 조회 전에 None 으로 끝난다

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with unittest.mock.patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co"}), \
                unittest.mock.patch("urllib.request.urlopen", lambda req, timeout=None: seen.append(req) or Resp()):
            self.assertIsNone(db.verify_token("aaa.bbb.ccc"))
        h = {k.lower(): v for k, v in seen[0].header_items()}
        self.assertEqual(h["apikey"], "sb_publishable_test")
        self.assertEqual(h["authorization"], "Bearer aaa.bbb.ccc")
        self.assertNotIn("sb_publishable_test", h["authorization"])

    def test_qa_config_prefers_publishable_key_and_falls_back_to_anon(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_config", Path(__file__).resolve().parent.parent / "qa" / "make_config.py")
        mc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mc)
        legacy = [{"name": "anon", "api_key": "legacy-anon"}, {"name": "service_role", "api_key": "legacy-service"}]
        new = {"name": "default", "api_key": "sb_publishable_x"}
        self.assertEqual(mc.pick_public_key(legacy), "legacy-anon")
        self.assertEqual(mc.pick_public_key(legacy + [new]), "sb_publishable_x")
        self.assertIsNone(mc.pick_public_key([legacy[1]]))  # service_role 키는 절대 고르지 않는다


if __name__ == "__main__":
    unittest.main(verbosity=2)
