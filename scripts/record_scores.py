# scripts/record_scores.py
#
# 매일 장마감 후 1회 실행: 대형주 후보(candidates.json)와 따로 지정한 종목(score_extra_codes.json)의
# AI 투자의견 점수를 score_history.csv에 한 줄씩 쌓는다.
#
# 점수가 높은 종목이 실제로 더 오르는지는 기록이 쌓여야만 검증할 수 있어서(score_validation.py),
# 대시보드와 똑같은 채점 함수로 매일 기록해 둔다. 다만 뉴스 감성은 종목마다 뉴스 검색이 필요해
# 여기서는 빼고(0점) 기록한다.

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import investment_opinion  # noqa: E402
import kis_api  # noqa: E402
import score_validation  # noqa: E402

KST = timezone(timedelta(hours=9))

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
CANDIDATES_PATH = os.path.join(SCRIPTS_DIR, "candidates.json")
EXTRA_CODES_PATH = os.path.join(SCRIPTS_DIR, "score_extra_codes.json")
DART_DATA_PATH = os.path.join(SCRIPTS_DIR, "dart_data.json")
SECTOR_PATH = os.path.join(SCRIPTS_DIR, "sector_valuation.json")
TICKERS_CSV = os.path.join(REPO_ROOT, "krx_tickers.csv")

CHART_DAYS = 90
OPINION_DAYS = 180


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_targets():
    """기록할 종목 [(코드, 이름)]. 대형주 후보 + 따로 지정한 종목."""

    names = {}
    with open(TICKERS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            names[kis_api.strip_market_suffix(row["티커"])] = row["회사명"]

    codes = [c["code"] for c in load_json(CANDIDATES_PATH, {}).get("candidates", [])]
    codes += [code for code in load_json(EXTRA_CODES_PATH, []) if code not in codes]
    return [(code, names.get(code, code)) for code in codes]


def score_stock(code, today, dart_stocks, sector_stats):
    """(캔들 거래일, 종가, 의견). 차트가 비어 있으면 None."""

    price_info = kis_api.fetch_current_price(code)
    df = kis_api.fetch_daily_chart(code, today - timedelta(days=CHART_DAYS), today)
    if df.empty:
        return None
    investment_opinion.add_indicators(df)

    investor = kis_api.fetch_investor_trend(code)
    opinions = kis_api.fetch_invest_opinions(code, today - timedelta(days=OPINION_DAYS), today)

    # 실행 시각이 아니라 실제 마지막 캔들의 거래일로 기록한다(휴장일·자정 넘긴 실행 대비).
    candle_date = df.index[-1]
    record = dart_stocks.get(code) or {}
    opinion = investment_opinion.generate_opinion(
        df, price_info,
        financials=record.get("financials"),
        disclosures=record.get("disclosures"),
        disclosure_count=record.get("disclosure_count"),
        investor=investor,
        opinions=opinions,
        sector_stats=sector_stats,
        today=candle_date,
    )
    return candle_date.strftime("%Y-%m-%d"), float(df["Close"].iloc[-1]), opinion


def main():
    today = datetime.now(KST).replace(tzinfo=None)
    targets = load_targets()
    dart_stocks = load_json(DART_DATA_PATH, {}).get("stocks", {})
    sector_stats = load_json(SECTOR_PATH, {}).get("sectors")

    history = score_validation.load_history()
    recorded = set(zip(history["date"], history["code"]))
    print(f"기록 대상: {len(targets)}개 종목 (기존 기록 {len(history)}건)")

    rows = []
    for i, (code, name) in enumerate(targets):
        try:
            result = score_stock(code, today, dart_stocks, sector_stats)
        except kis_api.KISAPIError as e:
            print(f"{name}({code}) 조회 실패: {e}")
            continue
        if result is None:
            continue

        date, close, opinion = result
        if (date, code) in recorded:
            continue

        row = {
            "date": date, "code": code, "name": name, "close": close,
            "verdict": opinion["verdict_name"], "total": opinion["total"], "max_total": opinion["max_total"],
        }
        for category in opinion["categories"]:
            row[category["name"]] = category["score"] if category["available"] else ""
        rows.append(row)

        if (i + 1) % 50 == 0:
            print(f"[{i + 1}/{len(targets)}] 진행 중 (새 기록 {len(rows)}건)")

    is_new_file = not os.path.exists(score_validation.HISTORY_PATH)
    with open(score_validation.HISTORY_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=score_validation.COLUMNS, lineterminator="\n")
        if is_new_file:
            writer.writeheader()
        writer.writerows(rows)

    dates = sorted({row["date"] for row in rows})
    print(f"완료: 새 기록 {len(rows)}건 ({', '.join(dates) or '추가 없음'}) -> {score_validation.HISTORY_PATH}")


if __name__ == "__main__":
    main()
