"""Supabase(Postgres) 연결 계층. 기본은 꺼져 있고(SQLite 그대로), 환경변수로 켠다.

켜는 법:  POC_DESK_BACKEND=supabase  (.env 의 값은 자동으로 읽는다. 이미 있는 환경변수는 덮어쓰지 않는다)
필요한 값: SUPABASE_PROJECT_REF, SUPABASE_POOLER_HOST(선택), 역할별 비밀번호
           POC_READER_DEMO_PASSWORD, APP_SERVICE_PASSWORD, SUPABASE_URL(로그인 검증용),
           SUPABASE_PUBLISHABLE_KEY (옛 이름 SUPABASE_ANON_KEY 도 읽는다)

역할을 나눠서 쓴다 (docs/13, erd.md):
  poc_reader_demo  모델이 만든 SQL·계산식 실행 전용. demo 스키마 읽기만, 읽기 전용 트랜잭션, 5초 제한. app·personal 접근 불가
  app_service      지표 정의·원장 읽기, 실행 기록 추가. demo·personal 접근 불가
접속은 Supabase 공식 인증서(app/certs/prod-ca-2021.crt)로 서버 신원을 확인한다.
"""
import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))  # 한국 시간. 서버리스 서버는 UTC라서 화면·기록 시각이 9시간 어긋나지 않게 명시한다(서머타임 없음)
APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
VENDOR = APP_DIR / "_vendor"
CA_FILE = APP_DIR / "certs" / "prod-ca-2021.crt"


def load_dotenv(path=None):
    """표준 라이브러리만으로 .env 를 읽는다. 이미 설정된 환경변수는 건드리지 않는다."""
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_dotenv()


POOLER_PORT = int(os.environ.get("SUPABASE_POOLER_PORT", "5432"))  # 서비스 역할 접속 포트. 5432 세션 모드, 6543 트랜잭션 모드(서버리스에 적합: 쉬는 연결이 DB 슬롯을 붙잡지 않는다)
SESSION_PORT = 5432  # 읽기 전용 계산 역할은 항상 세션 모드: SET search_path 가 필요하다(역할 기본값은 풀러를 거치면 적용되지 않는 것을 확인했다)
PERSIST = os.environ.get("POC_DESK_DB_PERSIST", "1") != "0"  # 0: 요청마다 연결을 닫는다(서버리스에서 연결이 남아 쌓이지 않게)


def enabled():
    return os.environ.get("POC_DESK_BACKEND", "sqlite").lower() == "supabase"


def _driver():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    try:
        import pg8000.native as pg
    except ImportError as e:
        raise RuntimeError("Supabase 접속 도구(pg8000)가 없습니다. 먼저 실행하세요: "
                           "python -m pip install --target app/_vendor -r requirements.txt") from e
    return pg


def _need(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"환경변수 {name} 가 없습니다 (.env 확인).")
    return v


def _connect(role, password_var, port):
    pg = _driver()
    ref = _need("SUPABASE_PROJECT_REF")
    host = os.environ.get("SUPABASE_POOLER_HOST", "aws-0-ap-northeast-2.pooler.supabase.com")
    ctx = ssl.create_default_context(cafile=str(CA_FILE))  # 인증서와 호스트 이름을 모두 검증
    return pg.Connection(f"{role}.{ref}", host=host, port=port, database="postgres",
                         password=_need(password_var), ssl_context=ctx, timeout=15)


class _Session:
    """역할별로 연결 하나를 재사용한다(접속 0.2초를 매 질문마다 치르지 않게). 끊기면 한 번 다시 연결한다."""

    def __init__(self, role, password_var, search_path=None, port=None):
        self.role, self.password_var, self.search_path, self.port = role, password_var, search_path, port
        self.conn, self.lock = None, threading.Lock()

    def _open(self):
        self.conn = _connect(self.role, self.password_var, self.port or SESSION_PORT)
        if self.search_path:
            self.conn.run(f"set search_path = {self.search_path}")

    def run(self, _stmt, **params):
        with self.lock:
            try:
                for attempt in (1, 2):
                    try:
                        if self.conn is None:
                            self._open()
                        return self.conn.run(_stmt, **params)
                    except (OSError, ConnectionError, ssl.SSLError):  # 연결이 끊긴 경우만 재시도 (SQL 오류는 그대로 올린다)
                        self.close()
                        if attempt == 2:
                            raise
                    except Exception as e:  # noqa: BLE001
                        if type(e).__name__ == "InterfaceError":
                            self.close()
                            if attempt == 2:
                                raise
                        else:
                            raise
            finally:
                if not PERSIST:
                    self.close()

    def close(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None


_reader = _Session("poc_reader_demo", "POC_READER_DEMO_PASSWORD", search_path="demo")
_service = _Session("app_service", "APP_SERVICE_PASSWORD", port=POOLER_PORT)
DATASET = "demo"


def shutdown():
    _reader.close()
    _service.close()


# ---- 오류 문구: 데이터베이스가 돌려주는 내부 문구(파일 이름·코드 포함)를 화면에 그대로 내보내지 않는다 ----
_FRIENDLY = {
    "57014": "시간 제한(5초)을 넘어 취소되었습니다. 더 작은 범위로 다시 시도하세요.",
    "42501": "허용되지 않은 테이블에 접근하려 했습니다(권한 없음). 합성 주문 데이터 테이블만 읽을 수 있습니다.",
    "25006": "읽기 전용입니다. 데이터를 바꾸는 SQL은 실행되지 않습니다.",
    "42P01": "존재하지 않는 테이블입니다.",
    "42703": "존재하지 않는 열입니다.",
    "42883": "존재하지 않는 함수이거나 인자 형식이 맞지 않습니다(SQLite 문법을 쓴 경우 PostgreSQL 문법으로 바꿔야 합니다).",
}


def friendly_error(exc):
    """pg8000 오류({'C': 코드, 'M': 메시지, ...})를 사람이 읽을 문장으로 바꾼다. 모르는 오류는 메시지 한 줄만 보여 준다."""
    info = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else None
    if info and info.get("C"):
        code = info["C"]
        if code in _FRIENDLY:
            return _FRIENDLY[code]
        kind = "SQL 문법 오류" if code.startswith("42") else "데이터베이스 오류"
        return f"{kind}: {info.get('M', '알 수 없음')}"
    if isinstance(exc, (OSError, ConnectionError, TimeoutError, ssl.SSLError)) or type(exc).__name__ == "InterfaceError":
        return "데이터베이스에 연결하지 못했습니다. 잠시 뒤 다시 시도하세요."
    return None


# ---- 모델·계산식 실행 (읽기 전용) ----
def execute_readonly(sql):
    """SELECT 한 개를 실행해 숫자 하나를 돌려준다. run_experiment.execute_readonly 와 같은 모양: (값, 상태)."""
    rows = _reader.run(sql)
    if len(rows) != 1 or len(rows[0]) != 1:
        return None, "bad_shape"
    v = rows[0][0]
    if isinstance(v, bool) or v is None or not isinstance(v, (int, float)) and not hasattr(v, "as_tuple"):
        return None, "not_numeric"
    return float(v), "ok"


def query_rows(sql):
    """미리 정해 둔(신뢰하는) 읽기 전용 SQL의 결과 행 전체. 모델이 만든 SQL에는 쓰지 않는다."""
    return _reader.run(sql)


def schema_text():
    """LLM에 줄 테이블 구조(demo 스키마). 원장(app)은 다른 스키마라 처음부터 보이지 않고, 주석도 없다."""
    rows = _reader.run("select table_name, column_name, data_type from information_schema.columns "
                       "where table_schema = 'demo' order by table_name, ordinal_position")
    tables = {}
    for t, c, d in rows:
        tables.setdefault(t, []).append(f"    {c} {d}")
    return "\n".join(f"CREATE TABLE {t} (\n" + ",\n".join(cols) + "\n);" for t, cols in tables.items())


SYSTEM_NOTE = ("데이터베이스는 PostgreSQL입니다. 테이블 이름은 스키마 없이 쓰세요(예: orders). "
               "날짜 월 추출은 to_char(paid_at, 'YYYY-MM'), 참/거짓 합계는 SUM((조건)::int)를 쓰세요.")


# ---- PostgreSQL용 기준 계산식 (experiment/gold.py 의 SQLite 계산식과 같은 정의, 문법만 다름) ----
def _in(months):
    return ",".join(f"'{m}'" for m in months)


def gold_sql(metric, months):
    paid_m = f"to_char(o.paid_at, 'YYYY-MM') IN ({_in(months)})"
    refund_ex = ("COALESCE(SUM(CASE r.refund_type WHEN 'FULL' THEN o.items_ex_vat "
                 "ELSE r.refund_amount * 100 / 110 END), 0)")
    revenue = (f"SELECT SUM(o.items_ex_vat) - {refund_ex} FROM orders o "
               f"LEFT JOIN refunds r USING (order_id) WHERE o.paid_at IS NOT NULL AND {paid_m}")
    valid_orders = (f"SELECT COUNT(*) FROM orders o WHERE o.status='PAID' "
                    f"AND o.paid_at IS NOT NULL AND {paid_m}")
    if metric == "net_revenue":
        return revenue
    if metric == "paid_orders":
        return valid_orders
    if metric == "aov":
        return f"SELECT ROUND(1.0 * ({revenue}) / ({valid_orders}))"
    if metric == "refund_rate":
        return (f"SELECT ROUND(100.0 * {refund_ex} / SUM(o.items_ex_vat), 2) FROM orders o "
                f"LEFT JOIN refunds r USING (order_id) WHERE o.paid_at IS NOT NULL AND {paid_m}")
    if metric == "cancel_rate":
        return (f"SELECT ROUND(100.0 * SUM((o.status='CANCELLED')::int) / COUNT(*), 2) FROM orders o "
                f"WHERE o.status IN ('PAID','CANCELLED') "
                f"AND to_char(o.ordered_at, 'YYYY-MM') IN ({_in(months)})")
    raise ValueError(metric)


# ---- 지표 정의·원장 (app 스키마, 서비스 역할) ----
def ledger_total(months, metric_key="net_revenue"):
    rows = _service.run(
        "select l.period_month, l.value from app.ledger l join app.kpi_definitions d using (definition_id) "
        "where d.dataset = :ds and d.metric_key = :mk and d.version = "
        "(select max(version) from app.kpi_definitions where dataset = :ds and metric_key = :mk)",
        ds=DATASET, mk=metric_key)
    ledger = {m: float(v) for m, v in rows}
    if any(m not in ledger for m in months):
        return None
    return sum(ledger[m] for m in months)


def definition_id(metric_key, version_text):
    """KPI 사전 내장 지표의 정의 번호. 사용자 정의 지표(파일)는 아직 DB에 없으므로 None."""
    try:
        version = int(str(version_text).lower().replace("사전", "").replace("v", "").strip())
    except ValueError:
        return None
    rows = _service.run("select definition_id from app.kpi_definitions where dataset = :ds and metric_key = :mk "
                        "and version = :v and source = 'builtin'", ds=DATASET, mk=metric_key, v=version)
    return rows[0][0] if rows else None


# ---- 실행 기록 ----
_COLS = ("run_id, user_id, ts, dataset, definition_id, question, months, mode, model, sql, value, reference_value, "
         "overall, status, error, elapsed_ms, checks, metric_key, metric_name, definition_version, unit")


def _aware(ts):
    """기록 시각 문자열을 시간대가 있는 값으로. 예전 기록처럼 시간대가 없으면 한국 시간으로 본다."""
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=KST)


def record_run(rec, user_id=None):
    _service.run(
        f"insert into app.query_runs ({_COLS}) values (:run_id, :user_id, :ts, :dataset, :definition_id, :question, "
        ":months, :mode, :model, :sql_text, :value, :reference_value, :overall, :status, :error, :elapsed_ms, "
        "cast(:checks as jsonb), :metric_key, :metric_name, :definition_version, :unit)",
        run_id=rec["id"], user_id=user_id, ts=_aware(rec["ts"]), dataset=DATASET,
        definition_id=definition_id(rec["metric_key"], rec["definition_version"]),
        question=rec["question"], months=list(rec["months"]), mode=rec["mode"], model=rec["model"], sql_text=rec["sql"],
        value=rec["value"], reference_value=rec["reference_value"], overall=rec["overall"], status=rec["status"],
        error=rec["error"], elapsed_ms=rec["elapsed_ms"], checks=json.dumps(rec["checks"], ensure_ascii=False),
        metric_key=rec["metric_key"], metric_name=rec["metric_name"],
        definition_version=rec["definition_version"], unit=rec["unit"])


def _row_to_rec(r):
    keys = [c.strip() for c in _COLS.split(",")]
    d = dict(zip(keys, r))
    d["id"] = d.pop("run_id")
    d["ts"] = d["ts"].astimezone(KST).isoformat(timespec="seconds") if hasattr(d["ts"], "astimezone") else str(d["ts"])
    d["months"] = list(d["months"] or [])
    for k in ("value", "reference_value"):
        d[k] = float(d[k]) if d[k] is not None else None
    d["checks"] = d["checks"] if isinstance(d["checks"], list) else json.loads(d["checks"] or "[]")
    d["user_id"] = str(d["user_id"]) if d["user_id"] else None
    return d


def history(user_id, limit=50):
    rows = _service.run(f"select {_COLS} from app.query_runs where user_id = :u order by ts desc limit :n",
                        u=user_id, n=limit)
    return [_row_to_rec(r) for r in rows]


def get_run(run_id, user_id):
    rows = _service.run(f"select {_COLS} from app.query_runs where run_id = :r and user_id = :u", r=run_id, u=user_id)
    return _row_to_rec(rows[0]) if rows else None


# ---- 로그인 확인 (Supabase Auth 세션 → 본인 계정인지) ----
_user_cache, _user_lock = {}, threading.Lock()


def public_key():
    """Supabase 공개 키. 새 publishable 키를 먼저 쓰고, 없으면 옛 anon 키(2026년 말 폐기 예정)로 돌아간다."""
    return os.environ.get("SUPABASE_PUBLISHABLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")


def verify_token(access_token):
    """로그인 토큰이 유효하고 app.profiles 에 등록된 본인 계정이면 {'id','email'}, 아니면 None."""
    if not access_token or access_token.count(".") != 2:
        return None
    import time
    now = time.monotonic()
    with _user_lock:
        hit = _user_cache.get(access_token)
        if hit and now - hit[0] < 30:
            return hit[1]
    req = urllib.request.Request(_need("SUPABASE_URL") + "/auth/v1/user",
                                 headers={"apikey": public_key() or _need("SUPABASE_PUBLISHABLE_KEY"),  # 새 키는 JWT가 아니라 Bearer에 넣을 수 없다
                                          "Authorization": "Bearer " + access_token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            user = json.loads(r.read())
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None
    uid = user.get("id")
    if not uid:
        return None
    ok = _service.run("select 1 from app.profiles where user_id = :u", u=uid)
    result = {"id": uid, "email": user.get("email")} if ok else None
    with _user_lock:
        if len(_user_cache) > 100:
            _user_cache.clear()
        _user_cache[access_token] = (now, result)
    return result
