# scripts/breakout_ma_monitor.py
#
# 장중 주기적으로 실행(09:00~15:30 KST, 15분 간격): breakout_watchlist_scan.py가
# 등록해둔 watchlist_breakout.json의 종목들에 대해 현재가를 조회하고,
# 5일선/10일선(직전 거래일까지의 종가로 계산, 오늘 캔들은 미완성이므로 제외)을
# 새로 이탈했는지 확인한다. 5일선 이탈과 10일선 이탈은 각각 독립적인 이벤트로
# 보고, 종목당 한 번씩만 알린다. 둘 다 알림을 보낸 종목은 감시 목록에서 제거한다.

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import kis_api  # noqa: E402

KST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(SCRIPT_DIR, "watchlist_breakout.json")
FETCH_DAYS = 40  # 10일 이동평균 계산에 넉넉한 캘린더 일수(주말/공휴일 감안)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"items": []}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    """전송 성공 여부를 bool로 반환한다. 실패하면 알림 대상을 '이미 보냄'으로
    기록하면 안 되므로(다음 실행에서 재시도해야 하므로), 성공 여부를 호출부에 알려준다."""

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 설정되지 않아 알림을 보낼 수 없습니다.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    if res.status_code != 200:
        print(f"텔레그램 전송 실패: {res.status_code} {res.text}")
        return False
    return True


def check_item(item, start, yesterday):
    """(current_price, ma5, ma10) 또는 조회 실패 시 None을 반환.

    이동평균은 '어제까지'의 종가로 계산한다 - 오늘 일봉은 장중에는 아직
    확정되지 않았으므로, 오늘의 실시간 현재가를 어제까지 계산한 이동평균선과
    비교해야 '이동평균선을 이탈하는 순간'을 제대로 잡아낼 수 있다.
    """

    try:
        df = kis_api.fetch_daily_chart(item["code"], start, yesterday)
    except kis_api.KISAPIError as e:
        print(f"{item['name']}({item['code']}) 차트 조회 실패: {e}")
        return None

    if len(df) < 10:
        return None

    ma5 = df["Close"].rolling(5).mean().iloc[-1]
    ma10 = df["Close"].rolling(10).mean().iloc[-1]

    try:
        info = kis_api.fetch_current_price(item["code"])
    except kis_api.KISAPIError as e:
        print(f"{item['name']}({item['code']}) 현재가 조회 실패: {e}")
        return None

    current_price = info["현재가"]
    if current_price <= 0:
        return None

    return current_price, float(ma5), float(ma10)


def main():
    state = load_state()
    items = state.get("items", [])
    pending = [it for it in items if not (it["alerted_5d"] and it["alerted_10d"])]

    if not pending:
        print("감시 중인 종목이 없습니다.")
        return

    today = datetime.now(KST).date()
    yesterday = today - timedelta(days=1)
    start = today - timedelta(days=FETCH_DAYS)

    hits_5d = []
    hits_10d = []

    for item in pending:
        result = check_item(item, start, yesterday)
        if result is None:
            continue
        current_price, ma5, ma10 = result

        if not item["alerted_5d"] and current_price < ma5:
            hits_5d.append({**item, "current_price": current_price, "ma5": ma5})

        if not item["alerted_10d"] and current_price < ma10:
            hits_10d.append({**item, "current_price": current_price, "ma10": ma10})

    if not hits_5d and not hits_10d:
        print("신규로 5일선/10일선을 이탈한 종목이 없습니다.")
        return

    lines = ["📉 전고점 돌파 종목 이동평균선 이탈", ""]
    if hits_5d:
        lines.append(f"[5일선 이탈] {len(hits_5d)}개")
        for h in hits_5d:
            lines.append(
                f"• {h['name']}({h['code']}, {h['market']})\n"
                f"  현재가 {h['current_price']:,.0f} / 5일선 {h['ma5']:,.0f} "
                f"(돌파일 {h['trigger_date']} 종가 {h['trigger_close']:,.0f}, "
                f"{h['trigger_change_pct']:+.2f}%)"
            )
        lines.append("")
    if hits_10d:
        lines.append(f"[10일선 이탈] {len(hits_10d)}개")
        for h in hits_10d:
            lines.append(
                f"• {h['name']}({h['code']}, {h['market']})\n"
                f"  현재가 {h['current_price']:,.0f} / 10일선 {h['ma10']:,.0f} "
                f"(돌파일 {h['trigger_date']} 종가 {h['trigger_close']:,.0f}, "
                f"{h['trigger_change_pct']:+.2f}%)"
            )

    sent = send_telegram("\n".join(lines))
    if sent:
        hit_codes_5d = {h["code"] for h in hits_5d}
        hit_codes_10d = {h["code"] for h in hits_10d}
        for it in items:
            if it["code"] in hit_codes_5d:
                it["alerted_5d"] = True
            if it["code"] in hit_codes_10d:
                it["alerted_10d"] = True
        print(f"5일선 {len(hits_5d)}개 / 10일선 {len(hits_10d)}개 알림 발송 완료")
    else:
        print("알림 발송 실패 (다음 실행에서 재시도됩니다)")

    # 두 알림을 모두 보낸 종목은 감시 목록에서 제거
    state["items"] = [it for it in items if not (it["alerted_5d"] and it["alerted_10d"])]
    save_state(state)


if __name__ == "__main__":
    main()
