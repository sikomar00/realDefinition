"""realDefinition 로컬 웹 서버 (표준 라이브러리만 사용, 설치 불필요).

실행: python app/server.py            → http://127.0.0.1:8765 이 자동으로 열린다
      python app/server.py --port 9000 --no-browser

보안: 내 PC(127.0.0.1)에서만 접속된다. 다른 웹사이트가 이 서버로 요청을 보내는 것을 막기 위해
Host·Origin 검사, JSON 요청만 허용, 사용자 지정 헤더(X-PocDesk) 확인을 한다. API 키는 화면으로 나가지 않는다.
"""
import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402
import db  # noqa: E402

PUBLIC = engine.PUBLIC  # POC_DESK_PUBLIC=1: 인터넷에 공개한 데모 (내 PC 전용 규칙 대신 같은 출처 규칙을 쓴다)
RATE_LIMIT, RATE_WINDOW = 90, 60  # 같은 접속자(IP)당 60초에 90번까지. 서버리스에서는 인스턴스별 근사치라 완전한 방어는 아니다
_hits, _hits_lock = {}, threading.Lock()
STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 100_000
DRAIN_LIMIT = 2_000_000  # 너무 큰 요청도 이만큼까지는 읽어서 버린다 (그 이상이면 연결을 닫음)
SUPABASE_ORIGIN = (os.environ.get("SUPABASE_URL") or "").rstrip("/") if engine.BACKEND == "supabase" else ""
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       f"connect-src 'self' {SUPABASE_ORIGIN}".rstrip() + "; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "PocDesk/1.0"
    timeout = 30  # 소켓 읽기·쓰기 제한(초). 요청을 덜 보내고 멈춘 클라이언트가 스레드를 영원히 붙잡지 못하게 한다

    def log_message(self, fmt, *args):
        """조용히: 정상 요청 기록은 버리고, 4xx·5xx 응답과 send_error 같은 오류 기록은 표준 오류로 남긴다."""
        is_request_log = fmt.startswith('"')  # log_request: '"%s" %s %s' → (요청줄, 응답 코드, 크기)
        failed = len(args) > 1 and str(args[1]).startswith(("4", "5"))
        if not is_request_log or failed:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- 보조
    def _restore_path(self):
        """Vercel 은 모든 요청을 /api/index?__vp=<원래 경로> 로 바꿔 넘긴다(vercel.json 의 rewrite). 원래 경로와 쿼리를 되돌린다."""
        if not PUBLIC:
            return
        u = urlparse(self.path)
        if u.path != "/api/index":
            return
        q = parse_qs(u.query, keep_blank_values=True)
        orig = q.pop("__vp", [""])[0].lstrip("/")
        rest = urlencode(q, doseq=True)
        self.path = "/" + orig + ("?" + rest if rest else "")

    def _host_ok(self):
        """내 PC 모드: 127.0.0.1·localhost 만. 공개 데모: 어떤 도메인이든 받되, 쓰기 요청은 같은 출처만 받는다(_origin_ok)."""
        host = self.headers.get("Host", "")
        return bool(host) if PUBLIC else host in self._allowed_hosts()

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if PUBLIC:
            return urlparse(origin).netloc == self.headers.get("Host", "")  # 다른 사이트에서 보낸 요청 차단
        return origin.replace("http://", "") in self._allowed_hosts()

    def _throttled(self):
        """공개 데모에서만: 한 접속자가 너무 자주 요청하면 거절한다."""
        if not PUBLIC:
            return False
        fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        who = fwd or self.client_address[0]
        now = time.monotonic()
        with _hits_lock:
            hits = [t for t in _hits.get(who, []) if now - t < RATE_WINDOW]
            hits.append(now)
            _hits[who] = hits
            if len(_hits) > 2000:
                for k in [k for k, v in _hits.items() if not v or now - v[-1] >= RATE_WINDOW]:
                    del _hits[k]
            return len(hits) > RATE_LIMIT

    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, code, body, ctype, extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            if PUBLIC:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, TimeoutError):
            self.close_connection = True  # 응답을 받을 클라이언트가 이미 떠났다

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _error(self, code, message):
        self._json({"error": message}, code)

    def _user(self):
        """Authorization: Bearer <로그인 토큰> 이 유효한 본인 계정이면 {'id','email'}, 아니면 None.
        Supabase 모드가 아니면 항상 None(로컬 SQLite 모드는 로그인을 쓰지 않는다)."""
        if engine.BACKEND != "supabase":
            return None
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return db.verify_token(auth[7:].strip())

    # ---- GET
    def do_GET(self):
        self._restore_path()
        if not self._host_ok():
            return self._error(403, "허용되지 않은 접속 주소입니다.")
        if self._throttled():
            return self._error(429, "요청이 너무 많습니다. 잠시 뒤 다시 시도하세요.")
        url = urlparse(self.path)
        path = url.path
        if path.startswith("/api/"):
            return self._api_get(path, parse_qs(url.query))
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._error(404, "찾을 수 없습니다.")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _api_get(self, path, q):
        try:
            if path == "/api/status":
                user = self._user()
                has_key = engine.llm_key_status()
                # 공개 데모에서는 비용·남용을 막으려고 AI 모드를 로그인한 본인에게만 연다
                st = {"llm_key": bool(has_key and (user or not PUBLIC)), "ai_login_required": bool(has_key and PUBLIC and not user),
                      "default_model": engine.DEFAULT_MODEL, "data_months": engine.VALID_MONTHS, "backend": engine.BACKEND, "public": PUBLIC}
                if engine.BACKEND == "supabase":  # 로그인 화면이 Supabase Auth 로 직접 로그인하는 데 쓰는 공개 값(비밀 아님)
                    st["auth"] = {"url": os.environ.get("SUPABASE_URL"), "public_key": db.public_key()}
                    st["user"] = {"email": user["email"]} if user else None
                return self._json(st)
            if path == "/api/dictionary":
                return self._json({"metrics": engine.dictionary()})
            if path == "/api/history":
                user = self._user()
                if engine.BACKEND == "supabase" and not user:
                    return self._error(401, "실행 기록은 로그인한 본인 것만 볼 수 있습니다. 로그인해 주세요.")
                return self._json({"runs": engine.history(50, user["id"] if user else None)})
            if path == "/api/run":
                user = self._user()
                if engine.BACKEND == "supabase" and not user:
                    return self._error(401, "로그인이 필요합니다.")
                return self._json(engine.get_run((q.get("id") or [""])[0], user["id"] if user else None))
        except ValueError as e:
            return self._error(404, str(e))
        self._error(404, "찾을 수 없습니다.")

    # ---- POST
    def _read_body(self):
        """요청 본문을 먼저 읽어 둔다. 거절 응답을 보내기 전에 본문을 소비하지 않으면,
        클라이언트가 본문을 보내는 도중 연결이 끊겨 거절 응답을 못 받는 일이 간헐적으로 생긴다.
        반환: (본문 바이트, 길이 오류 여부, 너무 큼 여부)"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b"", True, False
        if length < 0:
            return b"", True, False
        try:
            if length <= MAX_BODY:
                raw = self.rfile.read(length)
                if len(raw) < length:  # 약속한 길이보다 적게 보내고 연결을 닫았다
                    self.close_connection = True
                    return b"", True, False
                return raw, False, False
            remaining = min(length, DRAIN_LIMIT)  # 너무 큰 본문은 읽고 버리되 상한까지만
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            if length > DRAIN_LIMIT:
                self.close_connection = True  # 끝까지 소비하지 못했으니 연결을 닫는다
            return b"", False, True
        except (TimeoutError, ConnectionError):
            self.close_connection = True  # 본문을 보내다 멈춘 클라이언트: 스레드를 놓아 준다
            return b"", True, False

    def do_POST(self):
        raw, bad_length, too_big = self._read_body()
        self._restore_path()
        if not self._host_ok():
            return self._error(403, "허용되지 않은 접속 주소입니다.")
        if not self._origin_ok():
            return self._error(403, "허용되지 않은 출처입니다.")
        if self._throttled():
            return self._error(429, "요청이 너무 많습니다. 잠시 뒤 다시 시도하세요.")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._error(415, "JSON 요청만 받습니다.")
        if self.headers.get("X-PocDesk") != "1":
            return self._error(403, "잘못된 요청입니다.")
        if bad_length:
            return self._error(400, "잘못된 요청입니다.")
        if too_big:
            return self._error(413, "요청이 너무 큽니다.")
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._error(400, "요청 형식이 올바르지 않습니다.")
        path = urlparse(self.path).path
        try:
            if path == "/api/intake":
                return self._json(engine.intake(body.get("question")))
            if path == "/api/definition":
                if PUBLIC:  # 공개 서버는 파일을 영구 저장할 수 없고, 누구의 정의인지도 보장할 수 없다
                    return self._error(403, "공개 데모에서는 사용자 정의 지표 저장이 꺼져 있습니다. 내 PC에서 실행하면 저장할 수 있습니다.")
                if engine.BACKEND == "supabase" and not self._user():
                    return self._error(401, "사용자 정의 지표는 로그인한 본인만 저장할 수 있습니다.")
                aliases = body.get("aliases") or []
                if isinstance(aliases, str):
                    aliases = [a for a in aliases.replace("，", ",").split(",")]
                return self._json(engine.save_definition(body.get("name"), aliases, body.get("formula"),
                                                         body.get("include_exclude"), body.get("period_basis"),
                                                         body.get("unit")))
            if path == "/api/run":
                user = self._user()  # 비로그인도 합성 데이터 계산은 가능하다(기록의 user_id 는 비어 있음)
                if PUBLIC and (body.get("mode") or "reference") == "ai" and not user:
                    return self._error(401, "공개 데모에서 AI 모드는 로그인한 본인만 쓸 수 있습니다. 검증된 계산식 모드는 로그인 없이 쓸 수 있습니다.")
                return self._json(engine.run_query(str(body.get("question") or "")[:300], body.get("metric_key"),
                                                   list(body.get("months") or []), body.get("mode") or "reference",
                                                   body.get("model"), user["id"] if user else None))
            if path == "/api/compare":  # 로그인 없이도 가능(합성 데이터). 기록에는 남기지 않는다
                return self._json(engine.compare_definitions(body.get("metric_key"), list(body.get("months") or [])))
            if path == "/api/report":
                user = self._user()
                if engine.BACKEND == "supabase" and not user:
                    return self._error(401, "리포트는 로그인한 본인의 실행 기록으로만 만들 수 있습니다.")
                return self._json(engine.make_report(str(body.get("run_id") or ""), user["id"] if user else None))
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"내부 오류: {type(e).__name__}: {e}\n")
            return self._error(500, "처리 중 오류가 발생했습니다.")
        self._error(404, "찾을 수 없습니다.")


def make_server(port=8765):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    server = make_server(a.port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"realDefinition 실행 중: {url}  (끄려면 Ctrl+C)")
    print("모드:", "공개 데모(POC_DESK_PUBLIC=1)" if PUBLIC else "내 PC 전용")
    print("데이터:", "Supabase(서울, 합성 데이터)" if engine.BACKEND == "supabase" else "로컬 SQLite 파일")
    print("AI 모드:", "API 키 설정됨" if engine.llm_key_status() else "API 키 없음 (검증된 계산식 모드만 사용 가능)")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
