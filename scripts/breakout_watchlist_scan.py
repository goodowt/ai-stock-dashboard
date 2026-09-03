# scripts/breakout_watchlist_scan.py
#
# 매일 장마감 후 1회 실행: 코스피/코스닥 전 종목을 대상으로,
# (1) 오늘 종가가 최근 3개월(직전 60거래일) 최고 종가(전고점)를 돌파하고
# (2) 오늘 캔들이 양봉(종가 > 시가)이며
# (3) 전일 종가 대비 등락률이 15% 이상이고
# (4) 오늘 거래대금(거래량 x 종가로 추정)이 1000억원 이상인
# 종목을 찾아 watchlist_breakout.json에 등록하고, 신규 등록 종목을 텔레그램으로도
# 알린다. 이 목록은 breakout_ma_monitor.py가 장중에 주기적으로 읽어, 각 종목이
# 5일선/10일선을 이탈하는 순간을 별도로 감지해 다시 텔레그램으로 알린다.
#
# 전 종목을 대상으로 종목당 일봉 조회를 1회씩 하므로(약 2,700개), reversal_new_high_alert.py와
# 마찬가지로 실행에 시간이 오래 걸릴 수 있다.

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, SCRIPT_DIR)
import kis_api  # noqa: E402
import build_candidates as bc  # noqa: E402 (krx_tickers.csv 로딩 로직 재사용)

KST = timezone(timedelta(hours=9))
WINDOW = 60  # '3개월 전고점'을 볼 직전 거래일 수 (오늘 제외)
FETCH_DAYS = 130  # 60거래일을 넉넉히 확보하기 위한 조회 캘린더 일수(주말/공휴일 감안)
MIN_CHANGE_PCT = 15.0  # 전일 종가 대비 최소 등락률(%)
MIN_TURNOVER = 100_000_000_000  # 거래대금 1000억원
EXPIRE_DAYS = 120  # 이 기간(달력일) 안에 5일/10일선을 모두 이탈하지 않으면 감시 목록에서 자동 제거

STATE_PATH = os.path.join(SCRIPT_DIR, "watchlist_breakout.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 설정되지 않아 알림을 보낼 수 없습니다.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    if res.status_code != 200:
        print(f"텔레그램 전송 실패: {res.status_code} {res.text}")
        return False
    return True


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"items": []}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_ticker(t, start, today):
    """전고점 돌파 + 양봉 + 등락률 15%+ + 거래대금 1000억+ 조건을 만족하면 결과 dict를, 아니면 None을 반환."""

    try:
        df = kis_api.fetch_daily_chart(t["code"], start, today)
    except kis_api.KISAPIError as e:
        print(f"{t['name']}({t['code']}) 차트 조회 실패: {e}")
        return None

    # 오늘 + 직전 WINDOW거래일 + 등락률 계산용 전일 1개, 최소한 이만큼은 있어야 함
    if len(df) < WINDOW + 1:
        return None

    latest = df.iloc[-1]
    prev_close = df["Close"].iloc[-2]

    # 양봉: 종가가 시가보다 높음
    if not (latest["Close"] > latest["Open"]):
        return None

    # 등락률 15% 이상 (전일 종가 대비)
    if prev_close <= 0:
        return None
    change_pct = (latest["Close"] - prev_close) / prev_close * 100
    if change_pct < MIN_CHANGE_PCT:
        return None

    # 거래대금 1000억 이상 (일봉 API에 거래대금 필드가 없어 거래량 x 종가로 추정)
    turnover = float(latest["Volume"]) * float(latest["Close"])
    if turnover < MIN_TURNOVER:
        return None

    # 오늘 이전 직전 WINDOW거래일 중 최고 종가(전고점)를 오늘 종가가 돌파했는지
    prior_window_close = df["Close"].iloc[-(WINDOW + 1):-1]
    prior_high = prior_window_close.max()
    if latest["Close"] <= prior_high:
        return None

    return {
        **t,
        "trigger_date": today.isoformat(),
        "trigger_close": float(latest["Close"]),
        "trigger_change_pct": float(change_pct),
        "trigger_turnover": float(turnover),
        "prior_3m_high": float(prior_high),
        "alerted_5d": False,
        "alerted_10d": False,
    }


def main():
    state = load_state()
    items = state.get("items", [])

    today = datetime.now(KST).date()

    # 오래 감시했는데도 5일/10일선을 한 번도 이탈하지 않은 종목은 자동 만료시켜
    # 목록이 무한정 쌓이지 않게 한다.
    kept = []
    for it in items:
        trigger_date = datetime.fromisoformat(it["trigger_date"]).date()
        if (today - trigger_date).days > EXPIRE_DAYS:
            print(f"{it['name']}({it['code']}) 감시 만료({EXPIRE_DAYS}일 경과) - 목록에서 제거")
            continue
        kept.append(it)
    items = kept

    existing_codes = {it["code"] for it in items}

    tickers = bc.load_tickers()
    print(f"전체 종목 수: {len(tickers)}")

    start = today - timedelta(days=FETCH_DAYS)

    new_hits = []
    for i, t in enumerate(tickers):
        if t["code"] in existing_codes:
            continue

        result = check_ticker(t, start, today)
        if result:
            new_hits.append(result)
            print(f"[{i + 1}/{len(tickers)}] {t['name']}({t['code']}) 조건 충족 -> 감시 목록 등록")

        if (i + 1) % 200 == 0:
            print(f"[{i + 1}/{len(tickers)}] 진행 중 (신규 발견 {len(new_hits)}개)")

    print(f"스캔 완료: 신규 {len(new_hits)}개, 기존 감시 중 {len(items)}개")

    if new_hits:
        new_hits.sort(key=lambda h: h["trigger_change_pct"], reverse=True)
        MAX_LINES = 30
        lines = [
            f"🚀 전고점 돌파 감시 목록 신규 등록 ({len(new_hits)}개)",
            f"3개월 전고점을 거래대금 1000억+ / {MIN_CHANGE_PCT:.0f}%+ 양봉으로 돌파한 종목입니다.",
            "5일선·10일선을 이탈하면 별도로 알려드립니다.",
            "",
        ]
        for h in new_hits[:MAX_LINES]:
            lines.append(
                f"• {h['name']}({h['code']}, {h['market']})\n"
                f"  종가 {h['trigger_close']:,.0f} ({h['trigger_change_pct']:+.2f}%) / "
                f"거래대금 {h['trigger_turnover'] / 100_000_000:,.0f}억 / "
                f"3개월 전고점 {h['prior_3m_high']:,.0f}"
            )
        if len(new_hits) > MAX_LINES:
            lines.append(f"\n...외 {len(new_hits) - MAX_LINES}개 더")

        sent = send_telegram("\n".join(lines))
        print("신규 등록 알림 발송 완료" if sent else "신규 등록 알림 발송 실패")

    items.extend(new_hits)
    state["items"] = items
    save_state(state)


if __name__ == "__main__":
    main()
