"""실험 코드 단위 테스트.  실행: python experiment/test_experiment.py   (추가 설치 없음, 네트워크는 127.0.0.1만 사용)

사전 지정 판정 규칙, 채점, SQL 안전 장치, 호출 래퍼의 재시도, 실행기의 중단·재개처럼
'숫자 하나가 바뀌면 이미 발표한 결과와 어긋나는' 부분을 고정해 둔다.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evaluate_v2 as E  # noqa: E402
import gold_semantic as GS  # noqa: E402
import llm_client as L  # noqa: E402
import run_experiment as R  # noqa: E402
import run_semantic as S  # noqa: E402
import verification as V  # noqa: E402
import verify_without_gold as VW  # noqa: E402


# ------------------------------------------------------------------ SQL 안전 장치
class SqlSafety(unittest.TestCase):
    def test_precheck_reasons(self):
        cases = {
            "SELECT SUM(items_ex_vat) FROM orders": None,
            "WITH x AS (SELECT 1) SELECT * FROM x": None,
            "SELECT replace(name,'a','b') FROM products": None,     # REPLACE() 함수는 정상 SQL (이전에는 오탐)
            "SELECT 'a;b' AS x": None,                              # 문자열 안의 세미콜론 (이전에는 오탐)
            "SELECT 1 -- delete this": None,                        # 주석 속 단어
            "SELECT 'ledger' AS w": None,                           # 문자열 리터럴 속 이름
            "": "empty",
            "SELECT 1; DROP TABLE orders": "multiple_statements",
            "DELETE FROM orders": "not_select",
            "REPLACE INTO orders VALUES (1)": "not_select",
            "WITH x AS (SELECT 1) DELETE FROM orders": "forbidden_keyword",
            "SELECT * FROM ledger": "hidden_table",
            'SELECT * FROM "ledger"': "hidden_table",
        }
        for sql, want in cases.items():
            self.assertEqual(R.safety_check(sql), want, sql)

    def test_extract_sql(self):
        self.assertEqual(R.extract_sql("설명\n```sql\nSELECT 1;\n```"), "SELECT 1")
        self.assertEqual(R.extract_sql("SELECT 2"), "SELECT 2")
        self.assertEqual(R.extract_sql("```\nSELECT 3\n```"), "SELECT 3")

    def test_authorizer_blocks_what_the_regex_cannot_see(self):
        """정규식 검사를 우회한 시도도 sqlite 허용 규칙에서 막혀야 한다 (실제 보장은 이쪽)."""
        for sql in ("SELECT * FROM (SELECT name FROM sqlite_master)",
                    "SELECT name FROM pragma_table_info('ledger')",      # 정답표 이름을 문자열에 숨김
                    "SELECT net_revenue_ex_vat FROM main.ledger",
                    "SELECT 1 FROM orders WHERE 1=(SELECT COUNT(*) FROM ledger)",
                    "SELECT load_extension('x')"):
            with self.assertRaises(Exception, msg=sql):
                R.execute_readonly(sql)

    def test_authorizer_and_readonly_connection_block_writes(self):
        for sql in ("DELETE FROM orders", "INSERT INTO products VALUES (99,'x','y',1)", "DROP TABLE orders",
                    "ATTACH DATABASE ':memory:' AS m", "PRAGMA writable_schema=1"):
            with self.assertRaises(Exception, msg=sql):
                R.execute_readonly(sql)
        self.assertEqual(R.execute_readonly("SELECT COUNT(*) FROM orders"), (500.0, "ok"))

    def test_result_shape_and_type(self):
        self.assertEqual(R.execute_readonly("SELECT 1, 2"), (None, "bad_shape"))
        self.assertEqual(R.execute_readonly("SELECT name FROM products LIMIT 1"), (None, "not_numeric"))
        self.assertEqual(R.execute_readonly("SELECT 1 WHERE 0"), (None, "bad_shape"))

    def test_runaway_query_is_interrupted(self):
        t0 = time.time()
        with self.assertRaises(Exception):
            R.execute_readonly("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c",
                               timeout_s=1)
        self.assertLess(time.time() - t0, 10)


# ------------------------------------------------------------------ 채점과 집계
class ScoringAndStats(unittest.TestCase):
    def test_is_match_uses_metric_tolerance(self):
        self.assertTrue(R.is_match("net_revenue", 18287000.4, 18287000))
        self.assertFalse(R.is_match("net_revenue", 18287001.0, 18287000))      # 매출은 정확히 일치
        self.assertTrue(R.is_match("aov", 156404.0, 156403))                    # 객단가는 ±1원
        self.assertTrue(R.is_match("refund_rate", 6.805, 6.80))                 # 비율은 ±0.01%p
        self.assertFalse(R.is_match("refund_rate", 0.068, 6.80))                # 분수로 답하면 오답
        self.assertFalse(R.is_match("paid_orders", None, 149))

    def test_consistency(self):
        self.assertEqual(R.consistency([1.0, 1.0, 1.0, 1.0, 1.0], 0.5), 1.0)
        self.assertEqual(R.consistency([1.0, 1.0, 1.0, 2.0, None], 0.5), 0.6)
        self.assertEqual(R.consistency([None, None], 0.5), 0.5)                 # 실패는 서로 다른 값으로 센다
        self.assertEqual(R.consistency([5.0, 5.4, 5.8], 0.5), 2 / 3)            # 허용 오차 안은 한 묶음

    def test_wilson_interval(self):
        lo, hi = R.wilson(70, 100)
        self.assertAlmostEqual(lo, 0.604, places=2)
        self.assertAlmostEqual(hi, 0.781, places=2)
        self.assertEqual(R.wilson(0, 0), (0.0, 0.0))
        self.assertEqual(R.wilson(0, 10)[0], 0.0)
        self.assertEqual(R.wilson(10, 10)[1], 1.0)

    def test_mock_label_is_detected_for_named_mock_models(self):
        self.assertTrue("MOCK-A".startswith("MOCK"))


# ------------------------------------------------------------------ 사전 지정 판정 규칙
class PreRegisteredRules(unittest.TestCase):
    def test_primary_verdict_thresholds(self):
        v = E.verdict_primary
        self.assertEqual(v(20.0, -5.0), "개선(회귀 없음)")                      # 경계값은 통과
        self.assertEqual(v(19.9, 0), "불명")
        self.assertEqual(v(66.0, -6.0), "개선되었으나 대조 질문에서 회귀")      # 실제 결과(-6%p)
        self.assertEqual(v(4.9, 10), "무개선")
        self.assertEqual(v(5.0, 10), "불명")

    def _runs(self, spec):
        """spec: [(모델, 조건, qid, rep, correct, value)] → 모델별 기록."""
        by_model = {}
        for m, cond, qid, rep, ok, val in spec:
            by_model.setdefault(m, []).append({"cond": cond, "qid": qid, "rep": rep, "correct": ok, "value": val})
        return by_model

    def test_method_verdict_branches(self):
        def verdict(n_wrong, n_right, flag_wrong, flag_right):
            runs = [("m", "B1", "H01", i, False, 1.0) for i in range(n_wrong)] + \
                   [("m", "B1", "H01", 100 + i, True, 2.0) for i in range(n_right)]
            by_model = self._runs(runs)
            flagged = {("m", "H01", i) for i in range(flag_wrong)} | {("m", "H01", 100 + i) for i in range(flag_right)}
            with contextlib.redirect_stdout(io.StringIO()):
                return E.eval_method("t", flagged, by_model, "B1")
        self.assertIn("표본 부족", verdict(9, 30, 9, 0))                          # 오답 10개 미만은 판정하지 않는다
        self.assertEqual(verdict(20, 20, 18, 2), "재현됨")                         # 검출 90%, 정밀도 90% / 기준선 50% = 1.8배
        self.assertEqual(verdict(20, 20, 12, 12), "재현 실패")                     # 정밀도 50% = 기준선과 같음(1.0배)
        self.assertEqual(verdict(20, 20, 20, 15), "재현 실패")                     # 정밀도 57%(1.14배): 검출은 100%여도 1.2배 미만
        self.assertEqual(verdict(20, 20, 20, 10), "불명")                          # 정밀도 67%(1.33배): 1.2배 이상 1.5배 미만
        self.assertEqual(verdict(20, 20, 10, 2), "불명")                           # 검출 50% < 60%라 재현됨이 아니다

    def test_ceiling_effect_of_the_lift_criterion(self):
        """결함 기록(docs/09 4절): 오답 비율 56%에서는 정밀도 100%여도 배수가 1.79배를 넘을 수 없다."""
        self.assertAlmostEqual(1 / 0.56, 1.786, places=2)
        self.assertGreater(E.LIFT_OK * 0.56, 0.83)  # 1.5배를 넘으려면 정밀도 84% 이상이 필요

    def test_partial_records_do_not_crash_and_are_not_judged(self):
        d = E.acc([], "B1", "표적", {})
        self.assertEqual(d, (0, 0))


# ------------------------------------------------------------------ 생성 파일의 재현성
class Generators(unittest.TestCase):
    def test_generated_json_is_lf_and_identical_to_committed(self):
        """회귀: Windows에서 생성기를 다시 돌리면 CRLF로 써서 동결 해시와 달라지던 문제.
        다시 생성한 파일이 저장소 파일과 한 바이트도 다르지 않아야 한다(= 생성이 결정적이고 줄 끝이 LF)."""
        import gold
        import holdout
        tmp = Path(tempfile.mkdtemp(prefix="gen-"))
        saved = (gold.OUT, holdout.OUT)
        gold.OUT = holdout.OUT = tmp
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                gold.main()
                holdout.main()
        finally:
            gold.OUT, holdout.OUT = saved
        for name in ("questions_A.json", "questions_A_holdout.json", "h3_cases.json"):
            fresh = (tmp / name).read_bytes()
            self.assertNotIn(bytes([13, 10]), fresh, name)
            self.assertEqual(fresh, (HERE / name).read_bytes(), name)


# ------------------------------------------------------------------ 검증 방법 (공통 규칙)
class VerificationRules(unittest.TestCase):
    def test_agree_and_flags(self):
        self.assertTrue(V.agree("aov", 100.0, 101.0))
        self.assertFalse(V.agree("aov", 100.0, 102.0))
        self.assertFalse(V.agree("aov", None, 100.0))
        self.assertTrue(V.cross_model_flag("net_revenue", 1.0, 2.0))
        self.assertFalse(V.cross_model_flag("net_revenue", 5.0, 5.0))
        self.assertTrue(V.cross_model_flag("net_revenue", 5.0, None))
        self.assertFalse(V.repeat_flag("net_revenue", [1.0] * 5, 5))
        self.assertTrue(V.repeat_flag("net_revenue", [1.0, 1.0, 1.0, 2.0, 2.0], 4))
        self.assertFalse(V.repeat_flag("net_revenue", [1.0, 1.0, 1.0, 1.0, 2.0], 4))
        self.assertTrue(V.repeat_flag("net_revenue", [None] * 5, 4))

    def test_both_scripts_share_the_same_rules(self):
        self.assertIs(E.V, V)
        self.assertIs(VW.V, V)

    def test_retro_score_handles_zero_wrong_or_right(self):
        rows = {("m", ("Q01", 1)): {"correct": True}, ("m", ("Q01", 2)): {"correct": True}}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            VW.score("t", set(), list(rows), rows)  # 오답 0건이어도 0으로 나누지 않는다
        self.assertIn("n/a", out.getvalue())

    def test_retro_load_ignores_holdout_and_smoke_records(self):
        """회귀: 같은 모델 이름의 holdout 기록이 본 실험 기록을 덮어써 분석이 0건이 되던 문제."""
        data = VW.load("B")
        self.assertEqual(len(data), 2)
        for runs in data.values():
            self.assertEqual(len(runs), 100)  # 모델당 조건 B 호출 100회


# ------------------------------------------------------------------ 실행기: 중단·재개·평가기 입력
class _Stub:
    """LLMClient 대신 쓰는 가짜 클라이언트. 네트워크 호출 없음."""
    behavior = "ok"
    calls = 0

    def __init__(self, model, temperature=0.7, **kw):
        self.model, self.temperature = model, temperature

    def chat(self, system, user, meta=None):
        _Stub.calls += 1
        if _Stub.behavior == "401":
            raise L.LLMError("HTTP 401: invalid key")
        if _Stub.behavior == "500":
            raise L.LLMError("HTTP 500: server error")
        return {"text": "```sql\nSELECT 1\n```", "latency_s": 0.0, "prompt_tokens": 1, "completion_tokens": 1}


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exp-test-"))
        self._saved = (R.RUNS_DIR, R.RUNS_MOCK_DIR, L.LLMClient, sys.argv)
        R.RUNS_DIR, R.RUNS_MOCK_DIR = self.tmp / "runs", self.tmp / "mock"
        L.LLMClient = _Stub
        _Stub.behavior, _Stub.calls = "ok", 0

    def tearDown(self):
        R.RUNS_DIR, R.RUNS_MOCK_DIR, L.LLMClient, sys.argv = self._saved

    def run_main(self, *args):
        sys.argv = ["run_experiment.py", *args]
        out = io.StringIO()
        code = None
        with contextlib.redirect_stdout(out):
            try:
                R.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_auth_failure_stops_remaining_calls(self):
        """회귀: 401이 나도 남은 호출을 전부 시도해 몇 분씩 낭비하던 문제."""
        _Stub.behavior = "401"
        code, _ = self.run_main("--suite", "holdout", "--model", "m", "--workers", "4", "--run-id", "auth")
        self.assertIn("인증 실패", str(code))
        self.assertLessEqual(_Stub.calls, 8)  # 계획된 100회가 아니라 동시에 진행 중이던 몇 건뿐

    def test_resume_refuses_different_conditions(self):
        """회귀: 온도 같은 조건이 다른데도 한 파일에 이어 써서 메타가 거짓이 되던 문제."""
        self.run_main("--suite", "holdout", "--model", "m", "--temperature", "0.7", "--limit", "3", "--run-id", "t1")
        code, _ = self.run_main("--suite", "holdout", "--model", "m", "--temperature", "0.2", "--run-id", "t1")
        self.assertIn("이어서 실행할 수 없습니다", str(code))
        self.assertIn("temperature", str(code))
        code, out = self.run_main("--suite", "holdout", "--model", "m", "--temperature", "0.7", "--limit", "3", "--run-id", "t1")
        self.assertIsNone(code)
        self.assertIn("남은 호출 0", out)  # 같은 조건이면 그대로 이어진다

    def test_resume_retries_only_infrastructure_failures(self):
        _Stub.behavior = "500"
        self.run_main("--suite", "holdout", "--model", "m", "--limit", "2", "--run-id", "t2")
        _Stub.behavior, _Stub.calls = "ok", 0
        _, out = self.run_main("--suite", "holdout", "--model", "m", "--limit", "2", "--run-id", "t2")
        self.assertEqual(_Stub.calls, 2)  # llm_error 두 건만 다시 시도
        rows = [json.loads(l) for l in (R.RUNS_DIR / "t2.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(1 for r in rows if r.get("status") == "llm_error"), 2)  # 옛 기록은 남고
        self.assertEqual(sum(1 for r in rows if r.get("status") == "ok"), 2)         # 새 기록이 뒤에 붙는다

    def test_resume_accepts_old_records_without_new_meta_fields(self):
        self.run_main("--mock", "--run-id", "old", "--limit", "2")
        p = R.RUNS_MOCK_DIR / "old.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        meta = json.loads(lines[0])
        for k in ("suite", "dictionary_hashes", "max_tokens"):  # 새 필드가 없던 옛 기록을 흉내 낸다
            meta.pop(k, None)
        p.write_text("\n".join([json.dumps(meta, ensure_ascii=False)] + lines[1:]) + "\n", encoding="utf-8")
        code, _ = self.run_main("--mock", "--run-id", "old", "--limit", "2")
        self.assertIsNone(code)

    def test_summary_flags_named_mock_runs(self):
        self.run_main("--mock", "--mock-model", "MOCK-A", "--run-id", "m1", "--limit", "10")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            R.summarize(str(R.RUNS_MOCK_DIR / "m1.jsonl"))
        self.assertIn("MOCK: 실험 결과가 아님", buf.getvalue())

    def test_evaluator_refuses_partial_records_and_prefers_complete_ones(self):
        for name, model in (("a", "MOCK-A"), ("b", "MOCK-B")):
            self.run_main("--suite", "holdout", "--mock", "--mock-model", model, "--run-id", name)
        # 모델 A에 스모크처럼 짧은 기록을 더 나중 파일로 추가
        self.run_main("--suite", "holdout", "--mock", "--mock-model", "MOCK-A", "--run-id", "z-smoke", "--limit", "4")
        E.HERE = self.tmp  # find_latest 가 임시 폴더를 보게 한다
        (self.tmp / "runs_mock").mkdir(exist_ok=True)
        for f in R.RUNS_MOCK_DIR.glob("*.jsonl"):
            (self.tmp / "runs_mock" / f.name).write_bytes(f.read_bytes())
        try:
            picked = sorted(p.name for p in E.find_latest(True))
        finally:
            E.HERE = HERE
        self.assertEqual(picked, ["a.jsonl", "b.jsonl"])  # 4회짜리 z-smoke 가 아니라 완전한 기록

        # 부분 기록으로 평가하면 오류로 죽지 않고 안내하며 종료한다. --allow-partial 이면 계산 불가로 표시한다
        short = R.RUNS_MOCK_DIR / "z-smoke.jsonl"
        full_b = R.RUNS_MOCK_DIR / "b.jsonl"
        sys.argv = ["evaluate_v2.py", str(short), str(full_b)]
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                E.main()
        self.assertIn("불완전한 기록", str(ctx.exception))
        sys.argv = ["evaluate_v2.py", str(short), str(full_b), "--allow-partial"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            E.main()
        self.assertIn("계산 불가", buf.getvalue())


# ------------------------------------------------------------------ 호출 래퍼
class _Server:
    """응답을 정해 주는 로컬 HTTP 서버(127.0.0.1)."""
    def __init__(self, respond):
        outer = self
        self.hits = 0

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.hits += 1
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                respond(self, outer.hits)

            do_GET = do_POST

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


OK_BODY = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}).encode()


def _send(h, code, body, extra=None):
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


class ClientBehavior(unittest.TestCase):
    def setUp(self):
        self._key = os.environ.get("NVIDIA_API_KEY")
        os.environ["NVIDIA_API_KEY"] = "dummy-key-for-test"
        self.sleeps = []
        self._sleep = time.sleep
        L.time.sleep = lambda s: self.sleeps.append(s)  # 재시도 대기를 실제로 기다리지 않는다
        self.servers = []

    def tearDown(self):
        L.time.sleep = self._sleep
        for s in self.servers:
            s.close()
        if self._key is None:
            os.environ.pop("NVIDIA_API_KEY", None)
        else:
            os.environ["NVIDIA_API_KEY"] = self._key

    def serve(self, respond):
        s = _Server(respond)
        self.servers.append(s)
        return s

    def client(self, s, **kw):
        return L.LLMClient("m", base_url=f"http://127.0.0.1:{s.port}/v1", rpm=60000, timeout=5, **kw)

    def test_retries_non_json_and_truncated_responses(self):
        """회귀: 비JSON·잘린 응답은 재시도 없이 비LLMError로 새어 나가던 문제."""
        def respond(h, n):
            if n == 1:
                _send(h, 200, b"<html>proxy error</html>")
            elif n == 2:  # 길이를 약속하고 덜 보낸 뒤 끊는다 → IncompleteRead
                h.send_response(200)
                h.send_header("Content-Length", "500")
                h.end_headers()
                h.wfile.write(b"abc")
            else:
                _send(h, 200, OK_BODY)
        s = self.serve(respond)
        self.assertEqual(self.client(s, max_retries=4).chat("sys", "user")["text"], "ok")
        self.assertEqual(s.hits, 3)

    def test_no_wait_after_the_last_attempt(self):
        s = self.serve(lambda h, n: _send(h, 500, b'{"error":"x"}'))
        with self.assertRaises(L.LLMError) as ctx:
            self.client(s, max_retries=3).chat("sys", "user")
        self.assertIn("재시도 3회 실패", str(ctx.exception))
        retry_waits = [s for s in self.sleeps if s >= 1]  # 호출 속도 제한용 짧은 대기는 제외하고 재시도 대기만 센다
        self.assertEqual(retry_waits, [2, 4])  # 3번 시도 사이의 2번만 기다리고, 마지막 시도 뒤에는 기다리지 않는다

    def test_non_retryable_errors_fail_fast(self):
        for code in (400, 401, 403, 404, 422):
            s = self.serve(lambda h, n, c=code: _send(h, c, b'{"detail":"x"}'))
            with self.assertRaises(L.LLMError):
                self.client(s, max_retries=5).chat("sys", "user")
            self.assertEqual(s.hits, 1, code)

    def test_redirect_is_not_followed_so_the_key_never_leaves(self):
        """회귀: urllib은 리다이렉트 시 Authorization 헤더를 그대로 들고 간다."""
        target = self.serve(lambda h, n: _send(h, 200, OK_BODY))
        s = self.serve(lambda h, n: _send(h, 302, b"{}", {"Location": f"http://127.0.0.1:{target.port}/steal"}))
        with self.assertRaises(L.LLMError) as ctx:
            self.client(s, max_retries=3).chat("sys", "user")
        self.assertIn("HTTP 302", str(ctx.exception))
        self.assertEqual((s.hits, target.hits), (1, 0))  # 다른 주소로는 아무 요청도 가지 않았다

    def test_base_url_must_be_https_except_localhost(self):
        for url in ("http://example.com/v1", "ftp://example.com", "file:///etc/passwd"):
            with self.assertRaises(L.LLMError, msg=url):
                L.LLMClient("m", base_url=url)
        L.LLMClient("m", base_url="https://example.com/v1")
        L.LLMClient("m", base_url="http://localhost:8000/v1")
        L.LLMClient("m", base_url="http://127.0.0.1:8000/v1")

    def test_key_is_never_in_error_messages(self):
        s = self.serve(lambda h, n: _send(h, 401, b'{"detail":"bad key dummy-key-for-test"}'))
        with self.assertRaises(L.LLMError) as ctx:
            self.client(s).chat("sys", "user")
        self.assertNotIn("dummy-key-for-test", str(ctx.exception))

    def test_system_role_fallback_merges_once(self):
        seen = []

        def respond(h, n):
            seen.append(n)
            _send(h, 400 if n == 1 else 200, b'{"detail":"System role not supported"}' if n == 1 else OK_BODY)
        s = self.serve(respond)
        c = self.client(s)
        self.assertTrue(c.chat("sys", "user")["system_merged"])
        self.assertTrue(c.merge_system)
        self.assertEqual(s.hits, 2)


# ------------------------------------------------------------------ INT-008 시맨틱 레이어 시험 (D-041)
class SemanticRoute(unittest.TestCase):
    KEYS = {"net_revenue", "paid_orders", "aov", "refund_rate", "cancel_rate"}

    @classmethod
    def setUpClass(cls):
        cls.qs = json.loads(S.QUESTIONS_FILE.read_text(encoding="utf-8"))

    def test_parse_route(self):
        p = lambda t: S.parse_route(t, self.KEYS)  # noqa: E731
        self.assertEqual(p('```json\n{"metric": "aov", "months": ["2026-02", "2026-01", "2026-01"]}\n```'),
                         ("aov", ["2026-01", "2026-02"], None))                      # 코드 블록·중복·순서 정리
        self.assertEqual(p('설명 {"metric": "UNANSWERABLE", "months": []} 끝'), (S.UNAN, [], None))
        self.assertEqual(p("모르겠습니다")[2], "bad_json")
        self.assertEqual(p('{"metric": "revenue", "months": ["2026-01"]}')[2], "unknown_metric")
        self.assertEqual(p('{"metric": "aov", "months": []}')[2], "bad_months")
        self.assertEqual(p('{"metric": "aov", "months": ["1월"]}')[2], "bad_months")
        self.assertEqual(p('{"metric": "aov"}')[2], "bad_months")
        self.assertEqual(p("[1, 2]")[2], "bad_json")                                  # 객체가 아니면 거부

    def test_questions_shape(self):
        self.assertEqual((len(self.qs), sum(bool(q["metric"]) for q in self.qs)), (S.N_ALL, S.N_ANSWERABLE))
        self.assertEqual(Counter(q["group"] for q in self.qs), {"main": 20, "holdout": 10})
        self.assertEqual(len({q["id"] for q in self.qs}), 30)
        for q in self.qs:  # 답할 수 없는 질문은 골드가 비어 있고, 답할 수 있는 질문은 값이 있다
            self.assertEqual(q["gold_value"] is None, q["metric"] is None, q["id"])

    def test_questions_file_is_what_the_builder_makes(self):
        """질문 파일을 손으로 고치면(동결 위반) 잡힌다: 같은 DB에서 다시 만든 값과 같아야 한다."""
        import sqlite3
        con = sqlite3.connect(f"file:{R.DB}?mode=ro", uri=True)
        self.assertEqual(GS.build(con), self.qs)
        con.close()

    def test_gold_matches_independent_python_recalculation(self):
        """골드(SQL)를 규칙 문서(사전 v2)에서 파이썬으로 다시 계산한 값과 맞춘다. 두 방법이 달라지면 둘 중 하나가 틀린 것이다."""
        import sqlite3
        con = sqlite3.connect(f"file:{R.DB}?mode=ro", uri=True)
        orders = con.execute("SELECT order_id, ordered_at, paid_at, status, items_ex_vat FROM orders").fetchall()
        refunds = con.execute("SELECT order_id, refund_type, refund_amount FROM refunds").fetchall()
        con.close()
        refund_of = {}
        for oid, kind, amt in refunds:
            refund_of.setdefault(oid, []).append((kind, amt))
        for q in self.qs:
            if not q["metric"]:
                continue
            months = set(q["months"])
            paid = [o for o in orders if o[2] and o[2][:7] in months]            # 결제 완료 = paid_at 값이 있음
            items = {o[0]: o[4] for o in paid}
            refund_ex = sum(items[oid] if kind == "FULL" else amt / 1.1
                            for oid in items for kind, amt in refund_of.get(oid, []))
            revenue = sum(items.values()) - refund_ex
            valid = sum(1 for o in paid if o[3] == "PAID")
            ordered = [o for o in orders if o[1][:7] in months and o[3] in ("PAID", "CANCELLED")]
            want = {"net_revenue": revenue, "paid_orders": valid, "aov": revenue / valid,
                    "refund_rate": 100 * refund_ex / sum(items.values()),
                    "cancel_rate": 100 * sum(o[3] == "CANCELLED" for o in ordered) / len(ordered)}[q["metric"]]
            self.assertTrue(R.is_match(q["metric"], want, q["gold_value"]), (q["id"], want, q["gold_value"]))

    def test_scoring_s(self):
        q = {q["id"]: q for q in self.qs}
        rec = lambda: {"value": None, "status": None, "correct": False, "error": None}  # noqa: E731
        ok = rec()
        S.score_s(ok, '{"metric": "paid_orders", "months": ["2026-02"]}', q["S19"], self.KEYS)
        self.assertEqual((ok["correct"], ok["value"]), (True, 129.0))
        wrong_metric = rec()
        S.score_s(wrong_metric, '{"metric": "cancel_rate", "months": ["2026-02"]}', q["S19"], self.KEYS)
        self.assertFalse(wrong_metric["correct"])
        refuse = rec()
        S.score_s(refuse, '{"metric": "UNANSWERABLE", "months": []}', q["S16"], self.KEYS)  # 기간 밖 질문에 거절 = 정답
        self.assertEqual((refuse["correct"], refuse["value"]), (True, S.UNAN_VALUE))
        wrongly_refuse = rec()
        S.score_s(wrongly_refuse, '{"metric": "UNANSWERABLE", "months": []}', q["S19"], self.KEYS)
        self.assertFalse(wrongly_refuse["correct"])
        answers_anyway = rec()  # 답할 수 없는 질문에 지표를 골라 답하면 오답
        S.score_s(answers_anyway, '{"metric": "net_revenue", "months": ["2026-04"]}', q["S16"], self.KEYS)
        self.assertFalse(answers_anyway["correct"])
        garbage = rec()
        S.score_s(garbage, "죄송합니다", q["S19"], self.KEYS)
        self.assertEqual((garbage["correct"], garbage["status"]), (False, "bad_json"))

    def test_judge_rule_fixed_before_results(self):
        self.assertEqual((S.S_SUCCESS_ACC, S.S_SUCCESS_CONS, S.S_FAIL_GAIN_PP), (0.90, 0.90, 5.0))
        self.assertEqual(S.judge(0.90, 0.90, 0.0), "성공")        # 경계는 성공
        self.assertEqual(S.judge(0.899, 0.95, 40.0), "불명")      # 정확도 미달인데 B보다 크게 나음
        self.assertEqual(S.judge(0.95, 0.899, 40.0), "불명")      # 일치율 미달
        self.assertEqual(S.judge(0.70, 0.95, 4.9), "실패")        # B 대비 개선이 5%p 미만
        self.assertEqual(S.judge(0.70, 0.95, 5.0), "불명")
        self.assertEqual(S.overall({"a": "성공", "b": "성공"}), "성공")
        self.assertEqual(S.overall({"a": "실패", "b": "실패"}), "실패")
        self.assertEqual(S.overall({"a": "성공", "b": "실패"}), "불명")
        self.assertEqual(S.overall({"a": "성공"}), "불명")        # 모델이 하나뿐이면 종합하지 않는다

    def run_mock(self, tmp, model, temperature=None):
        old = R.RUNS_MOCK_DIR
        R.RUNS_MOCK_DIR = Path(tmp)
        try:
            argv = ["run_semantic.py", "--mock", "--mock-model", model, "--run-id", model]
            with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                S.main()
        finally:
            R.RUNS_MOCK_DIR = old
        path = Path(tmp) / f"{model}.jsonl"
        if temperature is not None:  # 모의 기록은 온도 0 이라 판정이 안 나온다. 판정 경로를 시험하려고 온도만 바꾼다
            lines = path.read_text(encoding="utf-8").splitlines()
            meta = json.loads(lines[0])
            meta["temperature"] = temperature
            path.write_text("\n".join([json.dumps(meta, ensure_ascii=False)] + lines[1:]) + "\n", encoding="utf-8")
        return path

    def test_mock_run_counts_resume_and_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [self.run_mock(tmp, m, 0.7) for m in ("MOCK-X", "MOCK-Y")]
            for p in paths:
                runs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()[1:]]
                self.assertEqual(Counter(r["cond"] for r in runs), {"S": 150, "B": 120})  # 30×5, 답할 수 있는 24×5
                self.assertTrue(all(r["cond"] == "S" or self.qs_by_id(r["qid"])["metric"] for r in runs))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                S.evaluate([str(p) for p in paths])
            self.assertIn("[종합 판정 (D-041)]", out.getvalue())
            self.assertEqual(out.getvalue().count("[모델 판정]"), 2)

    def test_resume_fills_only_missing_calls_and_refuses_changed_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self.run_mock(tmp, "MOCK-R")
            lines = p.read_text(encoding="utf-8").splitlines()
            p.write_text("\n".join(lines[:-5]) + "\n", encoding="utf-8")        # 마지막 5개가 빠진 상태
            self.run_mock(tmp, "MOCK-R")                                        # 같은 실행 ID로 다시 → 빠진 것만 채움
            keys = [json.loads(l)["run_key"] for l in p.read_text(encoding="utf-8").splitlines()[1:]]
            self.assertEqual((len(keys), len(set(keys))), (270, 270))
            meta = json.loads(p.read_text(encoding="utf-8").splitlines()[0])    # 기록 조건을 바꾸면 이어쓰기를 거부
            meta["questions_hash"] = "changed"
            p.write_text("\n".join([json.dumps(meta)] + lines[1:]) + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                self.run_mock(tmp, "MOCK-R")

    def qs_by_id(self, qid):
        return next(q for q in self.qs if q["id"] == qid)

    def test_unhashable_metric_is_a_format_error_not_a_call_failure(self):
        """회귀: 리스트 metric 이 TypeError → llm_error 로 기록되던 문제."""
        self.assertEqual(S.parse_route('{"metric": ["aov"], "months": ["2026-01"]}', self.KEYS)[2], "unknown_metric")
        self.assertEqual(S.parse_route('{"metric": {"a": 1}, "months": []}', self.KEYS)[2], "unknown_metric")

    def test_evaluate_counts_retried_calls_once_and_survives_partial_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [self.run_mock(tmp, m, 0.7) for m in ("MOCK-P", "MOCK-Q")]
            lines = paths[0].read_text(encoding="utf-8").splitlines()
            failed = dict(json.loads(lines[1]), status="llm_error", correct=False, value=None)
            paths[0].write_text("\n".join([lines[0], json.dumps(failed, ensure_ascii=False)] + lines[1:]) + "\n", encoding="utf-8")
            out = io.StringIO()  # 실패 기록 뒤에 같은 호출의 재시도 기록이 있어도 완료로 인정되어 판정이 나온다
            with contextlib.redirect_stdout(out):
                S.evaluate([str(p) for p in paths])
            self.assertEqual(out.getvalue().count("[모델 판정]"), 2)
            partial = Path(tmp) / "partial.jsonl"  # 앞 4줄만(스모크 테스트 상태)이어도 0으로 나누지 않는다
            partial.write_text("\n".join(lines[:5]) + "\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                S.evaluate([str(partial)])

    def test_incomplete_run_gets_no_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self.run_mock(tmp, "MOCK-Z", 0.7)
            lines = p.read_text(encoding="utf-8").splitlines()
            p.write_text("\n".join(lines[:-3]) + "\n", encoding="utf-8")  # 호출 3개가 덜 끝난 상태
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                S.evaluate([str(p)])
            self.assertIn("판정 없음", out.getvalue())
            self.assertNotIn("[종합 판정", out.getvalue())

    def test_prompts_do_not_contain_answers(self):
        """누수 점검: 모델이 보는 입력(시스템·사전·질문)에 정답 값과 골드 SQL이 없어야 한다."""
        d = json.loads(S.DICT_FILE.read_text(encoding="utf-8"))
        seen = S.SYSTEM_S + S.build_prompt_s(S.render_keyed(d), "질문") + R.load_schema()
        import re
        for q in self.qs:
            if q["metric"]:
                self.assertNotIn(q["gold_sql"], seen)
                v = q["gold_value"]
                for text in {f"{v:,.0f}", f"{v:.0f}", f"{v:.2f}"} if v >= 100 else {f"{v:.2f}"}:  # 숫자 토큰 단위로만 찾는다('6.55' 때문에 '6'이 걸리지 않게)
                    self.assertIsNone(re.search(rf"(?<![\d.,]){re.escape(text)}(?![\d.,])", seen), (q["id"], text))
        self.assertNotIn("ledger", R.load_schema())


if __name__ == "__main__":
    unittest.main(verbosity=1)
