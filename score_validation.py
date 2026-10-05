# score_validation.py
# AI 투자의견 점수 검증
#
# scripts/record_scores.py가 매일 쌓는 점수 기록(score_history.csv)으로, 점수가 높았던 종목이
# 실제로 그 뒤에 더 올랐는지 확인한다. investment_opinion.py의 기준값을 조정할 근거로 쓴다.
# 수익률은 같은 파일에 기록된 이후 종가로 계산하므로 추가 API 호출이 없다.

import os

import pandas as pd

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "score_history.csv")

# 며칠(기록된 거래일 기준) 뒤의 수익률을 볼지
HORIZONS = (5, 20)

BASE_COLUMNS = ["date", "code", "name", "close", "verdict", "total", "max_total"]
CATEGORY_COLUMNS = ["실적 성장", "분기 추세", "재무 건전성", "밸류에이션", "수급", "컨센서스", "공시", "기술적·뉴스"]
COLUMNS = BASE_COLUMNS + CATEGORY_COLUMNS

VERDICT_ORDER = ["매수 우위", "중립", "매도 우위"]

# 이보다 표본이 적은 구간은 우연일 가능성이 커서 표에서 따로 표시한다.
MIN_SAMPLES = 30


def load_history(path=HISTORY_PATH):
    """점수 기록. 파일이 없으면 빈 DataFrame."""

    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path, dtype={"code": str, "date": str}).sort_values(["code", "date"])


def with_forward_return(history, horizon):
    """각 기록에 `horizon` 거래일 뒤 수익률(%)을 붙이고, 아직 그날이 오지 않은 기록은 뺀다."""

    future_close = history.groupby("code")["close"].shift(-horizon)
    result = history.assign(forward_return=(future_close / history["close"] - 1) * 100)
    return result.dropna(subset=["forward_return"])


def summarize_by_verdict(history, horizon):
    """의견(매수 우위/중립/매도 우위)별 이후 수익률. 기록이 부족하면 None."""

    data = with_forward_return(history, horizon)
    if data.empty:
        return None

    overall = data["forward_return"].mean()
    rows = []
    for verdict in VERDICT_ORDER:
        group = data.loc[data["verdict"] == verdict, "forward_return"]
        if group.empty:
            continue
        rows.append({
            "의견": verdict,
            "표본 수": len(group),
            "평균 수익률(%)": round(group.mean(), 2),
            "전체 평균 대비(%p)": round(group.mean() - overall, 2),
            "오른 비율(%)": round((group > 0).mean() * 100, 1),
        })
    return pd.DataFrame(rows).set_index("의견")


def summarize_by_category(history, horizon):
    """항목별 점수와 이후 수익률의 순위 상관계수. 0에 가까우면 그 항목은 수익률을 설명하지 못한 것이다."""

    data = with_forward_return(history, horizon)
    rows = []
    for column in CATEGORY_COLUMNS + ["total"]:
        scored = data.dropna(subset=[column])
        if len(scored) < MIN_SAMPLES or scored[column].nunique() < 2:
            continue
        rows.append({
            "항목": "종합 점수" if column == "total" else column,
            "표본 수": len(scored),
            # 순위끼리의 상관(스피어만). pandas의 method="spearman"은 scipy가 필요해서 직접 순위를 매긴다.
            "상관계수": round(scored[column].rank().corr(scored["forward_return"].rank()), 3),
        })
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("항목")
