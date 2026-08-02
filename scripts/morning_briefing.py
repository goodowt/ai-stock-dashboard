# scripts/morning_briefing.py
#
# 매일 한국 장 시작 전(07:30 KST)에 실행: 미국 증시 종가, 원달러 환율,
# 국제유가(WTI), 반도체 관련주(엔비디아/마이크론), 그리고 연준·미국 대통령·
# 경제지표 관련 주요 뉴스를 모아 텔레그램으로 요약 발송한다.
# LLM을 쓰지 않는 규칙 기반 다이제스트라 별도 API 비용이 들지 않는다.

import html
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import requests
import yfinance as yf

KST = timezone(timedelta(hours=9))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

INDEX_TICKERS = {
    "다우": "^DJI",
    "나스닥": "^IXIC",
    "S&P500": "^GSPC",
}
FX_TICKER = "KRW=X"
OIL_TICKER = "CL=F"
SEMI_TICKERS = {
    "엔비디아(NVDA)": "NVDA",
    "마이크론(MU)": "MU",
}

NEWS_QUERIES = {
    "연준/금리": "연준 파월 금리",
    "미국 대통령": "미국 대통령",
    "미국 경제지표": "미국 경제지표 발표",
}


def fetch_change(ticker):
    """직전 2개 종가로 (마지막 종가, 등락률%)을 반환. 실패하면 None."""

    try:
        df = yf.download(ticker, period="5d", progress=False)
        if df.empty or len(df) < 2:
            return None
        close = df["Close"]
        if hasattr(close, "iloc") and close.ndim == 2:
            close = close.iloc[:, 0]
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        change_pct = (last - prev) / prev * 100
        return last, change_pct
    except Exception as e:
        print(f"{ticker} 조회 실패: {e}")
        return None


def format_line(label, result, prefix="", decimals=2):
    # parse_mode=HTML로 보내므로 &, <, > 등은 반드시 이스케이프해야 한다
    # (예: "S&P500"의 '&'가 그대로 있으면 텔레그램이 메시지 전체를 거부한다).
    label = html.escape(label)
    if result is None:
        return f"{label}: 조회 실패"
    value, pct = result
    arrow = "🔺" if pct >= 0 else "🔻"
    sign = "+" if pct >= 0 else ""
    return f"{label}: {prefix}{value:,.{decimals}f} ({arrow}{sign}{pct:.2f}%)"


def fetch_news(query, limit=3):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    parsed = feedparser.parse(url)
    return [{"title": e.title, "link": e.link} for e in parsed.entries[:limit]]


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 설정되지 않아 알림을 보낼 수 없습니다.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )
    if res.status_code != 200:
        print(f"텔레그램 전송 실패: {res.status_code} {res.text}")
        return False
    return True


def build_message():
    today = datetime.now(KST).strftime("%m월 %d일 (%a)")
    lines = [f"🌅 {today} 프리마켓 브리핑", ""]

    lines.append("🇺🇸 미국 증시 (전일 종가)")
    for label, ticker in INDEX_TICKERS.items():
        lines.append(format_line(label, fetch_change(ticker)))
    lines.append("")

    lines.append(format_line("💱 원달러 환율", fetch_change(FX_TICKER), decimals=1))
    lines.append(format_line("🛢 WTI유가", fetch_change(OIL_TICKER), prefix="$", decimals=2))
    lines.append("")

    lines.append("🔧 반도체 관련주")
    for label, ticker in SEMI_TICKERS.items():
        lines.append(format_line(label, fetch_change(ticker), prefix="$"))
    lines.append("")

    lines.append("📰 주요 뉴스")
    for topic, query in NEWS_QUERIES.items():
        news = fetch_news(query)
        if not news:
            continue
        lines.append(f"[{html.escape(topic)}]")
        for n in news:
            lines.append(f'- <a href="{n["link"]}">{html.escape(n["title"])}</a>')
        lines.append("")

    return "\n".join(lines).strip()


def main():
    message = build_message()

    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", errors="replace").decode("ascii"))

    if send_telegram(message):
        print("브리핑 발송 완료")
    else:
        print("브리핑 발송 실패")


if __name__ == "__main__":
    main()
