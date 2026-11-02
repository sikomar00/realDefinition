"""INT-008 시맨틱 레이어 시험 실행기·채점기 (D-041, PRD F15).

조건 S: LLM은 {"metric", "months"} 만 고르고, SQL은 gold.py 의 검증된 식이 만든다.
        계산은 구성상 맞으므로 이 시험이 재는 것은 '질문을 지표·기간으로 바르게 옮기는가'(라우팅)뿐이다.
조건 B: 사전 v2 + 자유 SQL(기존 방식). 답할 수 있는 질문 24개에서만, 같은 모델·같은 온도로 비교한다.

사용 예
  python experiment/run_semantic.py --dry-run
  python experiment/run_semantic.py --mock
  python experiment/run_semantic.py --model MODEL --limit 4                  # 스모크(실제 호출 4회)
  python experiment/run_semantic.py --model MODEL                            # 온도 0.7, 조건 S+B (모델당 270회)
  python experiment/run_semantic.py --model MODEL --temperature 0 --conds S  # 온도 0 병행(판정에 쓰지 않음)
  python experiment/run_semantic.py --summarize experiment/runs_semantic/XXX.jsonl ...

주의: 아래 판정 기준은 D-041 에서 실행 전에 고정한 값이다. 결과를 본 뒤 바꾸지 않는다.
"""
import argparse
import json
import random
import re
import sqlite3
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_experiment as R  # noqa: E402
from gold import gold_sql  # noqa: E402

RUNS_DIR = HERE / "runs_semantic"  # 기존 runs/ 는 동결이라 섞지 않는다
QUESTIONS_FILE = HERE / "questions_S.json"
DICT_FILE = HERE / "kpi_dictionary_A_v2.json"

# ---- 사전 고정된 판정 기준 (D-041) ----
S_SUCCESS_ACC = 0.90        # 시맨틱 정확도(질문 30개 전체) ≥ 90% 그리고
S_SUCCESS_CONS = 0.90       # 질문 단위 일치율 ≥ 90% 이면 '성공'
S_FAIL_GAIN_PP = 5.0        # 성공이 아니고, 답할 수 있는 24개에서 (S-B) 정확도 차이 < 5%p 이면 '실패', 아니면 '불명'
REPS = 5
SHUFFLE_SEED = 20261102
UNAN = "UNANSWERABLE"
UNAN_VALUE = -1e9           # '답할 수 없음'을 일치율 계산에서 하나의 값으로 묶기 위한 표식(실제 지표 값과 겹치지 않음)
N_ANSWERABLE, N_ALL = 24, 30

SYSTEM_S = (
    "당신은 사용자의 질문을 KPI 사전의 지표와 기간으로 옮기는 라우터입니다. SQL은 쓰지 마세요. "
    'JSON 객체 하나만 출력하세요: {"metric": "<지표 키>", "months": ["YYYY-MM", ...]}. '
    f'질문이 사전에 없는 지표를 묻거나, 데이터 기간 밖이거나, 지표가 모호해 하나로 정할 수 없으면 {{"metric": "{UNAN}", "months": []}} 를 출력하세요.'
)


def render_keyed(d):
    """사전을 지표 키와 함께 보여 준다(조건 B와 같은 내용에 키만 더한다)."""
    return "\n".join(f"[지표 키: {k}]\n" + R.render_dictionary({"metrics": {k: m}}) for k, m in d["metrics"].items())


def build_prompt_s(dict_text, question):
    return "\n\n".join([R.COMMON_NOTE, f"[KPI 사전]\n{dict_text}", f"[질문]\n{question}"])


def parse_route(text, keys):
    """(지표, 정렬된 월 목록, 오류 사유). 오류가 없으면 사유는 None."""
    i = text.find("{")
    if i < 0:
        return None, None, "bad_json"
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
    except ValueError:
        return None, None, "bad_json"
    metric, months = (obj.get("metric"), obj.get("months")) if isinstance(obj, dict) else (None, None)
    if metric == UNAN:
        return UNAN, [], None
    if not isinstance(metric, str) or metric not in keys:  # 리스트 등 해시 불가 값도 형식 오류로 센다(호출 실패가 아님)
        return metric if isinstance(metric, str) else None, None, "unknown_metric"
    if not isinstance(months, list) or not months or not all(isinstance(m, str) and re.fullmatch(r"\d{4}-\d{2}", m) for m in months):
        return metric, None, "bad_months"
    return metric, sorted(set(months)), None


def score_s(rec, text, q, keys):
    metric, months, err = parse_route(text, keys)
    rec["route"] = {"metric": metric, "months": months}
    if err:
        rec["status"] = err
        return
    if metric == UNAN:
        rec["value"], rec["status"], rec["correct"] = UNAN_VALUE, "unanswerable", q["metric"] is None
        return
    try:
        rec["value"], rec["status"] = R.execute_readonly(gold_sql(metric, months))
    except sqlite3.Error as e:
        rec["status"], rec["error"] = "sql_error", str(e)[:200]
    rec["correct"] = q["metric"] is not None and R.is_match(q["metric"], rec["value"], q["gold_value"])


def score_b(rec, text, q):
    rec["sql"] = sql = R.extract_sql(text)
    block = R.safety_check(sql)
    if block:
        rec["status"] = f"blocked:{block}"
    else:
        try:
            rec["value"], rec["status"] = R.execute_readonly(sql)
        except sqlite3.Error as e:
            rec["status"], rec["error"] = "sql_error", str(e)[:200]
    rec["correct"] = R.is_match(q["metric"], rec["value"], q["gold_value"])


class MockClient:
    """파이프라인 검증 전용 가짜 LLM. 결과를 실험 결과로 쓰면 안 된다."""
    temperature = 0.0

    def __init__(self, model="MOCK"):
        self.model = model

    def chat(self, system, user, meta):
        if meta["cond"] == "B":
            return R.MockClient(self.model).chat(system, user, meta)
        r, q = random.Random(f"{self.model}|{meta['run_key']}").random(), meta["question"]
        if r < 0.03:
            text = "죄송하지만 알 수 없습니다."
        elif r < 0.85:
            text = json.dumps({"metric": q["metric"] or UNAN, "months": q["months"]})
        else:
            text = json.dumps({"metric": "net_revenue", "months": ["2026-01"]})
        return {"text": text, "latency_s": 0.0, "prompt_tokens": None, "completion_tokens": None, "model": "MOCK"}


# ---- 채점·판정 ----
def judge(s_acc, s_cons, gain_pp):
    if s_acc >= S_SUCCESS_ACC and s_cons >= S_SUCCESS_CONS:
        return "성공"
    return "실패" if gain_pp < S_FAIL_GAIN_PP else "불명"


def overall(verdicts):
    vs = set(verdicts.values())
    return vs.pop() if len(vs) == 1 and len(verdicts) >= 2 else "불명"


def cond_stats(runs, qs):
    """(정확도, 질문 단위 일치율 평균)."""
    by_q = defaultdict(list)
    for r in runs:
        by_q[r["qid"]].append(r)
    cons = [R.consistency([x["value"] for x in v], R.TOLERANCE.get(qs[qid]["metric"], 0.5)) for qid, v in by_q.items()]
    return sum(r["correct"] for r in runs) / len(runs), sum(cons) / len(cons)


def pct(x):
    return f"{x * 100:.1f}%"


def share(k, n):
    return pct(k / n) if n else "해당 없음"  # 부분 실행(--limit)에서 비는 묶음이 있어도 요약이 죽지 않게


def evaluate(paths):
    qs = {q["id"]: q for q in json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))}
    verdicts = {}
    for path in paths:
        recs = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
        meta = recs[0]
        # 장애로 실패한 호출은 이어서 실행할 때 같은 run_key 로 다시 기록되므로, 마지막 기록만 센다
        runs = list({r["run_key"]: r for r in recs[1:] if r.get("type") == "run"}.values())
        s, b = ([r for r in runs if r["cond"] == c] for c in "SB")
        print(f"\n== {Path(path).name}  모델={meta['model']} 온도={meta['temperature']}"
              f"{'  *** MOCK: 실험 결과가 아님 ***' if meta['model'].startswith('MOCK') else ''}")
        if s:
            s_acc, s_cons = cond_stats(s, qs)
            ans = [r for r in s if qs[r["qid"]]["metric"]]
            una = [r for r in s if not qs[r["qid"]]["metric"]]
            route_ok = [r for r in ans if r.get("route") and r["route"]["metric"] == qs[r["qid"]]["metric"]]
            print(f"S 전체 정확도 {pct(s_acc)} ({sum(r['correct'] for r in s)}/{len(s)})  일치율 {pct(s_cons)}")
            print(f"  답할 수 있는 질문 정확도 {share(sum(r['correct'] for r in ans), len(ans))}, 지표 고르기만 {share(len(route_ok), len(ans))}"
                  f" / 답할 수 없는 질문에서 '답할 수 없음' {share(sum(r['correct'] for r in una), len(una))} (n={len(una)})")
            for g in ("main", "holdout"):
                rs = [r for r in s if qs[r["qid"]]["group"] == g]
                print(f"  {g:8s} 정확도 {share(sum(r['correct'] for r in rs), len(rs))} (n={len(rs)})")
            print(f"  상태: {dict(Counter(r['status'] for r in s))}")
        if b:
            b_acc, b_cons = cond_stats(b, qs)
            print(f"B(답할 수 있는 24개) 정확도 {pct(b_acc)} ({sum(r['correct'] for r in b)}/{len(b)})  일치율 {pct(b_cons)}")
        done = len(s) == N_ALL * REPS and len(b) == N_ANSWERABLE * REPS
        if s and b and meta["temperature"] > 0 and done:
            gain = (sum(r["correct"] for r in ans) / len(ans) - b_acc) * 100
            verdicts[meta["model"]] = judge(s_acc, s_cons, gain)
            print(f"[모델 판정] S-B(답할 수 있는 24개) {gain:+.1f}%p → {verdicts[meta['model']]}")
        elif s and b:
            print("[모델 판정 없음] 온도 0 참고용이거나 호출이 덜 끝남")
    if verdicts:
        print(f"\n[종합 판정 (D-041)] {overall(verdicts)}  모델별: {verdicts}")


# ---- 실행 ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--conds", default="S,B")
    ap.add_argument("--rpm", type=int, default=30)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--run-id")
    ap.add_argument("--merge-system", action="store_true")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--mock-model", default="MOCK")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--summarize", nargs="+")
    a = ap.parse_args()
    if a.summarize:
        return evaluate(a.summarize)

    questions = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    d = json.loads(DICT_FILE.read_text(encoding="utf-8"))
    ctx = {"dict_s": render_keyed(d), "dict_b": R.render_dictionary(d), "schema": R.load_schema()}
    conds = a.conds.split(",")
    if a.dry_run:
        q = next(q for q in questions if q["id"] == "S19")
        print(f"===== 조건 S =====\n[system]\n{SYSTEM_S}\n\n[user]\n{build_prompt_s(ctx['dict_s'], q['question'])}\n")
        print(f"===== 조건 B =====\n[system]\n{R.SYSTEM_PROMPT}\n\n[user]\n"
              f"{R.build_user_prompt('B', ctx['schema'], ctx['dict_b'], q['question'])}")
        return

    plan = [(c, q, r) for q in questions for r in range(1, REPS + 1) for c in conds if c == "S" or q["metric"]]
    random.Random(SHUFFLE_SEED).shuffle(plan)
    if a.limit:
        plan = plan[:a.limit]

    if a.mock:
        client, outdir = MockClient(a.mock_model), R.RUNS_MOCK_DIR
    else:
        if not a.model:
            sys.exit("--model 이 필요합니다 (python experiment/llm_client.py models 로 확인)")
        from llm_client import LLMClient  # noqa: E402
        client = LLMClient(a.model, a.temperature, max_tokens=a.max_tokens, rpm=a.rpm, timeout=a.timeout, merge_system=a.merge_system)
        outdir = RUNS_DIR
    outdir.mkdir(exist_ok=True)
    temp = 0.0 if a.mock else a.temperature
    slug = re.sub(r"[^A-Za-z0-9.-]+", "_", client.model.split("/")[-1])[:24]
    run_id = a.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + f"-sem-{slug}-t{temp:g}" + ("-mock" if a.mock else "")
    path = outdir / f"{run_id}.jsonl"
    expected = {"model": client.model, "temperature": temp, "conds": conds, "max_tokens": None if a.mock else a.max_tokens,
                "questions_hash": R.sha(json.dumps(questions, ensure_ascii=False, sort_keys=True)),
                "dictionary_hash": R.sha(ctx["dict_b"]), "system_s_hash": R.sha(SYSTEM_S)}
    done = set()
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        old = json.loads(lines[0])
        diff = [f"{k}: 기록 {old.get(k)!r} / 지금 {v!r}" for k, v in expected.items() if old.get(k) != v]
        if diff:
            sys.exit("이어서 실행할 수 없습니다. 기존 기록과 실험 조건이 다릅니다.\n  " + "\n  ".join(diff))
        old_runs = [json.loads(l) for l in lines[1:]]
        done = {r["run_key"] for r in old_runs if r.get("type") == "run" and r.get("status") != "llm_error"}  # 장애 호출은 이어서 다시 시도
    else:
        path.write_text(json.dumps({"type": "meta", "run_id": run_id, "started_at": datetime.now().isoformat(timespec="seconds"),
                                    "reps": REPS, "tolerance": R.TOLERANCE, **expected}, ensure_ascii=False) + "\n", encoding="utf-8")

    todo = [(c, q, r) for c, q, r in plan if f"{c}|{q['id']}|{r}" not in done]
    print(f"실행 ID={run_id}  남은 호출 {len(todo)}/{len(plan)}  모델={client.model}  -> {path}")
    lock, abort, keys = threading.Lock(), threading.Event(), set(d["metrics"])

    def do_one(task):
        if abort.is_set():
            return
        cond, q, rep = task
        run_key = f"{cond}|{q['id']}|{rep}"
        rec = {"type": "run", "run_key": run_key, "cond": cond, "qid": q["id"], "rep": rep, "ts": datetime.now().isoformat(timespec="seconds"),
               "value": None, "status": None, "correct": False, "error": None}
        system, user = ((SYSTEM_S, build_prompt_s(ctx["dict_s"], q["question"])) if cond == "S" else
                        (R.SYSTEM_PROMPT, R.build_user_prompt("B", ctx["schema"], ctx["dict_b"], q["question"])))
        try:
            resp = client.chat(system, user, {"run_key": run_key, "cond": cond, "gold_sql": q["gold_sql"], "question": q})
            rec.update({"latency_s": resp["latency_s"], "finish_reason": resp.get("finish_reason"), "raw_text": resp["text"][:2000],
                        "prompt_tokens": resp["prompt_tokens"], "completion_tokens": resp["completion_tokens"]})
            score_s(rec, resp["text"], q, keys) if cond == "S" else score_b(rec, resp["text"], q)
        except Exception as e:  # 호출 실패는 오답으로 기록하되 실험은 계속(인증 실패만 중단)
            if type(e).__name__ == "LLMError" and "HTTP 401" in str(e):
                abort.set()
                raise
            rec["status"], rec["error"] = "llm_error", str(e)[:200]
        with lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    n = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
            for _ in pool.map(do_one, todo):
                n += 1
                if n % 10 == 0 or n == len(todo):
                    print(f"  진행 {n}/{len(todo)}", flush=True)
    except Exception as e:
        sys.exit(f"중단(인증 실패 등): {e}")
    print("완료. 요약: python experiment/run_semantic.py --summarize", path)


if __name__ == "__main__":
    main()
