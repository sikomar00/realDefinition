"""INT-008 시맨틱 레이어 시험용 새 질문 30개와 골드 답을 만든다 (D-041).

질문은 Claude가 썼다(사전·골드와 작성자가 같다 = 작성자 미분리, 한계). 골드는 gold.py의 검증된 식을 그대로 쓴다.
metric 이 None 이면 '답할 수 없음'이 정답이다(사전에 없는 지표, 데이터 기간 밖, 모호한 질문).

실행: python experiment/gold_semantic.py   → questions_S.json 생성
"""
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from gold import DB, Q1, gold_sql  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# (id, 묶음, 질문, 지표, 월, 노리는 점)
QUESTIONS = [
    ("S01", "main", "1월에 우리가 번 돈이 얼마야?", "net_revenue", ["2026-01"], "별칭('벌어들인 돈'류)"),
    ("S02", "main", "2026년 2월 매출액 좀 뽑아줘", "net_revenue", ["2026-02"], "별칭(매출액)"),
    ("S03", "main", "1월부터 3월까지 순매출 총합은?", "net_revenue", Q1, "다중 월"),
    ("S04", "main", "2월과 3월 매출을 합치면?", "net_revenue", ["2026-02", "2026-03"], "다중 월(연속 두 달)"),
    ("S05", "main", "1월 결제된 주문이 몇 건이었지?", "paid_orders", ["2026-01"], "결제 건수 별칭"),
    ("S06", "main", "3월에 팔린 주문 건수 알려줘", "paid_orders", ["2026-03"], "별칭(팔린 주문)"),
    ("S07", "main", "2월 주문당 평균 금액이 궁금해", "aov", ["2026-02"], "객단가 별칭"),
    ("S08", "main", "분기 전체로 보면 주문 하나당 평균 매출은?", "aov", Q1, "다중 월, 별칭"),
    ("S09", "main", "3월에 환불이 매출의 몇 %였어?", "refund_rate", ["2026-03"], "환불률을 돌려 말함"),
    ("S10", "main", "1분기 환불 비율은?", "refund_rate", Q1, "'분기' → 3개월"),
    ("S11", "main", "2월에 취소된 주문 비율", "cancel_rate", ["2026-02"], "취소율"),
    ("S12", "main", "1월~3월 전체 취소율", "cancel_rate", Q1, "범위 표현"),
    ("S13", "main", "1월 평균 주문 금액은 얼마지?", "aov", ["2026-01"], "객단가 별칭"),
    ("S14", "main", "3월에 결제 완료된 주문 수", "paid_orders", ["2026-03"], "결제 완료 표현"),
    ("S15", "main", "1월에 환불된 금액은 얼마야?", None, [], "사전에 없는 지표(환불액). 환불률이 아님"),
    ("S16", "main", "4월 매출은 얼마야?", None, [], "데이터 기간(1~3월) 밖"),
    ("S17", "main", "재구매율이 어떻게 돼?", None, [], "사전에 없는 지표"),
    ("S18", "main", "이번 분기 숫자 좀 정리해줘", None, [], "지표가 모호함"),
    ("S19", "main", "2월에 주문이 몇 건 들어왔어?", "paid_orders", ["2026-02"], "수식어 없는 '주문 수'(과거 오답 함정)"),
    ("S20", "main", "3월 환불 얼마나 됐어? 비율로 알려줘", "refund_rate", ["2026-03"], "금액 아닌 비율 지정"),
    ("S21", "holdout", "작년 12월 매출은?", None, [], "데이터 기간 밖"),
    ("S22", "holdout", "배송비 총액이 얼마야?", None, [], "사전에 없는 지표"),
    ("S23", "holdout", "3월 매출은 얼마였어? 부가세 빼고", "net_revenue", ["2026-03"], "부가세 제외 명시"),
    ("S24", "holdout", "1월 환불비중", "refund_rate", ["2026-01"], "환불률 별칭, 짧은 문장"),
    ("S25", "holdout", "2월 1건당 평균 판매액", "aov", ["2026-02"], "객단가 별칭"),
    ("S26", "holdout", "1~2월 두 달간 취소 비율", "cancel_rate", ["2026-01", "2026-02"], "다중 월(1~2월)"),
    ("S27", "holdout", "분기 내내 팔린 주문은 총 몇 건?", "paid_orders", Q1, "'분기 내내' → 3개월"),
    ("S28", "holdout", "2월 벌어들인 돈", "net_revenue", ["2026-02"], "별칭, 명사형"),
    ("S29", "holdout", "3월 환불 비중은?", "refund_rate", ["2026-03"], "환불 비중 별칭"),
    ("S30", "holdout", "1월 팔린 주문 하나당 평균 매출", "aov", ["2026-01"], "객단가 별칭"),
]


def build(con):
    out = []
    for qid, group, text, metric, months, note in QUESTIONS:
        q = {"id": qid, "group": group, "question": text, "metric": metric, "months": months,
             "note": note, "gold_sql": None, "gold_value": None}
        if metric:
            q["gold_sql"] = gold_sql(metric, months)
            q["gold_value"] = con.execute(q["gold_sql"]).fetchone()[0]
        out.append(q)
    ledger = dict(con.execute("SELECT period_month, net_revenue_ex_vat FROM ledger"))
    for q in out:  # 매출 골드는 원장과 맞아야 한다(gold.py와 같은 자기 검증)
        if q["metric"] == "net_revenue":
            assert round(q["gold_value"]) == sum(ledger[m] for m in q["months"]), q["id"]
    return out


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    questions = build(con)
    with open(HERE / "questions_S.json", "w", encoding="utf-8", newline="\n") as f:  # LF 고정: 해시가 바뀌지 않게
        f.write(json.dumps(questions, ensure_ascii=False, indent=2))
    n_none = sum(q["metric"] is None for q in questions)
    print(f"질문 {len(questions)}개 (답할 수 있음 {len(questions) - n_none}, 답할 수 없음 {n_none})")
    for q in questions:
        v = "-" if q["gold_value"] is None else f"{q['gold_value']:,.2f}"
        print(f"{q['id']} {q['group']:7s} {str(q['metric']):12s} {v:>14s}  {q['question']}")


if __name__ == "__main__":
    main()
