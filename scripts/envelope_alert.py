# scripts/envelope_alert.py
#
# 장중 주기적으로 실행: build_candidates.py가 만들어둔 후보 목록(candidates.json)의
# 종목들에 대해 '오늘 저가'를 조회하고, Envelope 하단(MA20 * 0.8)에 닿았는지 확인한다.
# 새로 닿은 종목이 있으면 텔레그램으로 알리고, 같은 날 중복 알림을 보내지 않도록
# alerted_today.json에 상태를 기록한다.

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
CANDIDATES_PATH = os.path.join(SCRIPT_DIR, "candidates.json")
STATE_PATH = os.path.join(SCRIPT_DIR, "alerted_today.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def load_candidates():
    if not os.path.exists(CANDIDATES_PATH):
        print("candidates.json이 없습니다. build_candidates.py를 먼저 실행해야 합니다.")
        return []
    with open(CANDIDATES_PATH, encoding="utf-8") as f:
        return json.load(f).get("candidates", [])


def load_state():
    today = datetime.now(KST).date().isoformat()
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
    return {"date": today, "alerted_codes": []}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 설정되지 않아 알림을 보낼 수 없습니다.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    if res.status_code != 200:
        print(f"텔레그램 전송 실패: {res.status_code} {res.text}")


def main():
    candidates = load_candidates()
    if not candidates:
        return

    state = load_state()
    alerted = set(state["alerted_codes"])

    new_hits = []
    for c in candidates:
        if c["code"] in alerted:
            continue
        try:
            info = kis_api.fetch_current_price(c["code"])
        except kis_api.KISAPIError as e:
            print(f"{c['name']}({c['code']}) 시세 조회 실패: {e}")
            continue

        today_low = info["오늘저가"]
        if today_low <= 0:
            continue

        if today_low <= c["env_lower"]:
            new_hits.append({**c, "today_low": today_low, "current_price": info["현재가"]})
            alerted.add(c["code"])

    if new_hits:
        lines = [f"📉 Envelope 하단 터치 종목 ({len(new_hits)}개, 시가총액 1.2조 이상)", ""]
        for h in new_hits:
            lines.append(
                f"• {h['name']}({h['code']}, {h['market']})\n"
                f"  오늘 저가 {h['today_low']:,.0f} / 현재가 {h['current_price']:,.0f} "
                f"/ Envelope 하단 {h['env_lower']:,.0f}"
            )
        send_telegram("\n".join(lines))
        print(f"{len(new_hits)}개 종목 알림 발송 완료")
    else:
        print("신규로 Envelope 하단을 터치한 종목이 없습니다.")

    state["alerted_codes"] = sorted(alerted)
    save_state(state)


if __name__ == "__main__":
    main()
