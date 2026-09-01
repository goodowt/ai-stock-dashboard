# scripts/reversal_new_high_alert.py
#
# 매일 장마감 후 1회 실행: 코스피/코스닥 전 종목을 대상으로,
# (1) 전일까지 이동평균이 역배열(MA5 < MA10 < MA20, 단기 하락 추세) 상태였다가
# (2) 오늘 종가가 최근 60거래일(오늘 포함) 중 최고 종가를 기록하고
# (3) 오늘 캔들이 양봉(종가 > 시가)인
# 종목, 즉 "역배열 하락추세에서 시작해 오늘 60일 신고 종가를 양봉으로 돌파한" 종목을
# 찾아 텔레그램으로 알린다. 역배열 여부를 오늘이 아닌 전일 기준으로 보는 이유는,
# 60일 신고가를 만드는 강한 양봉이 있는 날은 그 하루만으로 MA5가 MA10을
# 웃돌아버리는 경우가 흔해서, 오늘 값까지 포함해 판정하면 정작 찾으려는
# "역배열에서 막 돌파한" 상황을 거의 잡아내지 못하기 때문이다.
#
# 전 종목을 대상으로 종목당 일봉 조회를 1회씩 하므로(약 2,700개), KIS 모의투자
# 초당 호출 제한 때문에 실행에 시간이 오래 걸릴 수 있다(build_candidates.py와
# 비슷한 수준). 그래서 GitHub Actions에서 넉넉한 타임아웃으로 하루 한 번만 돌린다.

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
WINDOW = 60  # 최근 며칠(거래일) 중 최고 종가를 볼지
FETCH_DAYS = 130  # 60거래일을 넉넉히 확보하기 위한 조회 캘린더 일수(주말/공휴일 감안)

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


def check_ticker(t, start, today):
    """역배열 + 60거래일 신고 종가 + 양봉 조건을 만족하면 결과 dict를, 아니면 None을 반환."""

    try:
        df = kis_api.fetch_daily_chart(t["code"], start, today)
    except kis_api.KISAPIError as e:
        print(f"{t['name']}({t['code']}) 차트 조회 실패: {e}")
        return None

    if len(df) < WINDOW:
        return None

    ma5 = df["Close"].rolling(5).mean()
    ma10 = df["Close"].rolling(10).mean()
    ma20 = df["Close"].rolling(20).mean()

    latest = df.iloc[-1]

    # 역배열 여부는 '어제까지'의 이동평균으로 판단한다. 오늘처럼 60일 신고가를
    # 만드는 강한 양봉이 낀 날은 그 하루만으로 MA5가 MA10 위로 튀어 오르는 경우가
    # 흔해서, 오늘 값까지 포함해 판정하면 "역배열에서 막 돌파한" 상황을 거의
    # 못 잡아낸다. "역배열에서 시작해서 돌파했다"는 요청 의도에 맞게, 돌파 직전
    # 시점(전일)의 이동평균 정렬로 하락 추세였는지를 확인한다.
    ma5_prev, ma10_prev, ma20_prev = ma5.iloc[-2], ma10.iloc[-2], ma20.iloc[-2]
    if any(pd.isna(v) for v in (ma5_prev, ma10_prev, ma20_prev)):
        return None

    # 역배열: 단기 이평이 중기·장기 이평보다 아래에 있는 하락 추세
    if not (ma5_prev < ma10_prev < ma20_prev):
        return None

    # 양봉: 종가가 시가보다 높음
    if not (latest["Close"] > latest["Open"]):
        return None

    # 오늘 종가가 최근 60거래일(오늘 포함) 중 최고 종가인지
    window_close = df["Close"].tail(WINDOW)
    if latest["Close"] < window_close.max():
        return None

    prev_close = df["Close"].iloc[-2] if len(df) >= 2 else None
    change_pct = (
        (latest["Close"] - prev_close) / prev_close * 100 if prev_close else None
    )

    return {
        **t,
        "close": float(latest["Close"]),
        "change_pct": change_pct,
        "ma5_prev": float(ma5_prev),
        "ma10_prev": float(ma10_prev),
        "ma20_prev": float(ma20_prev),
    }


def main():
    tickers = bc.load_tickers()
    print(f"전체 종목 수: {len(tickers)}")

    today = datetime.now(KST).date()
    start = today - timedelta(days=FETCH_DAYS)

    hits = []
    for i, t in enumerate(tickers):
        result = check_ticker(t, start, today)
        if result:
            hits.append(result)
            print(f"[{i + 1}/{len(tickers)}] {t['name']}({t['code']}) 조건 충족")

        if (i + 1) % 200 == 0:
            print(f"[{i + 1}/{len(tickers)}] 진행 중 (발견 {len(hits)}개)")

    print(f"스캔 완료: {len(hits)}개 종목이 조건을 만족했습니다.")

    if not hits:
        print("역배열 + 60거래일 신고 종가 + 양봉 조건을 만족한 종목이 없습니다.")
        return

    hits.sort(key=lambda h: h["change_pct"] or 0, reverse=True)

    MAX_LINES = 30
    lines = [
        f"🔄 역배열 60일 신고 종가 양봉 돌파 ({len(hits)}개)",
        "전일까지 이동평균 역배열(MA5<MA10<MA20) 상태였다가, 오늘 최근 60거래일",
        "중 최고 종가를 양봉으로 갱신한 종목입니다.",
        "",
    ]
    for h in hits[:MAX_LINES]:
        pct_text = f"{h['change_pct']:+.2f}%" if h["change_pct"] is not None else "N/A"
        lines.append(
            f"• {h['name']}({h['code']}, {h['market']})\n"
            f"  종가 {h['close']:,.0f} ({pct_text}) / 전일 기준 "
            f"MA5 {h['ma5_prev']:,.0f} < MA10 {h['ma10_prev']:,.0f} < MA20 {h['ma20_prev']:,.0f}"
        )
    if len(hits) > MAX_LINES:
        lines.append(f"\n...외 {len(hits) - MAX_LINES}개 더")

    sent = send_telegram("\n".join(lines))
    print("알림 발송 완료" if sent else "알림 발송 실패")


if __name__ == "__main__":
    main()
