# scripts/build_candidates.py
#
# 매일 장마감 후 1회 실행: 코스피/코스닥 전 종목 중 시가총액 1조2천억 이상인
# 종목을 추려, MA20/Envelope 하단값과 함께 candidates.json에 저장한다.
# 이렇게 미리 후보를 걸러두면, 장중 알림 스캔(envelope_alert.py)은
# 전체 종목이 아니라 이 후보 목록만 조회하면 되어 훨씬 빠르고 API 호출이 적다.

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import kis_api  # noqa: E402

KST = timezone(timedelta(hours=9))
MARKET_CAP_THRESHOLD = 1_2000_0000_0000  # 1조 2천억원

TICKERS_CSV = os.path.join(REPO_ROOT, "krx_tickers.csv")
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candidates.json")


def load_tickers():
    tickers = []
    with open(TICKERS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["회사명"]
            ticker = row["티커"]
            if ticker.endswith(".KS"):
                market = "KOSPI"
            elif ticker.endswith(".KQ"):
                market = "KOSDAQ"
            else:
                continue
            tickers.append({"name": name, "code": kis_api.strip_market_suffix(ticker), "market": market})
    return tickers


def filter_large_caps(tickers):
    large_caps = []
    for i, t in enumerate(tickers):
        try:
            info = kis_api.fetch_current_price(t["code"])
        except kis_api.KISAPIError as e:
            print(f"[{i + 1}/{len(tickers)}] {t['name']}({t['code']}) 시세 조회 실패: {e}")
            continue

        market_cap = info["시가총액"] * 1_0000_0000  # KIS hts_avls는 억원 단위
        if market_cap >= MARKET_CAP_THRESHOLD:
            large_caps.append(t)

        if (i + 1) % 200 == 0:
            print(f"[{i + 1}/{len(tickers)}] 진행 중 (대형주 후보 {len(large_caps)}개 발견)")

    return large_caps


def build_candidates(large_caps):
    today = datetime.now(KST).date()
    start = today - timedelta(days=60)

    candidates = []
    for i, t in enumerate(large_caps):
        try:
            df = kis_api.fetch_daily_chart(t["code"], start, today)
        except kis_api.KISAPIError as e:
            print(f"{t['name']}({t['code']}) 차트 조회 실패: {e}")
            continue

        if len(df) < 20:
            print(f"{t['name']}({t['code']}) 거래일수 부족({len(df)}일), 제외")
            continue

        ma20 = df["Close"].rolling(20).mean().iloc[-1]
        if pd.isna(ma20):
            continue

        env_lower = ma20 * 0.8
        candidates.append({
            "name": t["name"],
            "code": t["code"],
            "market": t["market"],
            "ma20": round(float(ma20), 2),
            "env_lower": round(float(env_lower), 2),
        })

        if (i + 1) % 50 == 0:
            print(f"[{i + 1}/{len(large_caps)}] MA20 계산 진행 중")

    return candidates


def main():
    tickers = load_tickers()
    print(f"전체 종목 수: {len(tickers)}")

    large_caps = filter_large_caps(tickers)
    print(f"시가총액 {MARKET_CAP_THRESHOLD / 1_0000_0000_0000:.1f}조 이상 종목: {len(large_caps)}개")

    candidates = build_candidates(large_caps)
    print(f"MA20 계산 완료 종목: {len(candidates)}개")

    result = {
        "generated_at": datetime.now(KST).isoformat(),
        "market_cap_threshold": MARKET_CAP_THRESHOLD,
        "candidates": candidates,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"완료: {len(candidates)}개 종목 -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
