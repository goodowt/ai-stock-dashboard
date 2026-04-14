# ai_stock_dashboard_final_news.py

import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import feedparser

st.set_page_config(page_title="AI 주식 대시보드", layout="wide")

st.title("📊 AI 주식 트레이딩 대시보드 (HTS PRO FINAL + NEWS)")

# ----------------------
# 🔥 CSV로 종목 불러오기 (핵심 수정)
# ----------------------
@st.cache_data
def load_ticker_csv():
    try:
        df = pd.read_csv("krx_tickers.csv")
        return df
    except:
        st.error("❌ krx_tickers.csv 파일 없음 (GitHub 업로드 필요)")
        st.stop()

ticker_df = load_ticker_csv()

# ----------------------
# 🔍 종목 검색
# ----------------------
search = st.sidebar.text_input("종목 검색 (예: 삼성전자)")

filtered = ticker_df[
    ticker_df["회사명"].str.contains(search, case=False, na=False)
]

if not filtered.empty:
    options = filtered["회사명"].tolist()
    selected_name = st.sidebar.selectbox("종목 선택", options)
    ticker = ticker_df[ticker_df["회사명"] == selected_name]["티커"].values[0]
else:
    st.sidebar.warning("종목을 찾을 수 없습니다")
    st.stop()

# ----------------------
# 설정
# ----------------------
interval = st.sidebar.selectbox(
    "봉 타입",
    ["1d", "1h", "15m", "5m", "1m"]
)

start_date = st.sidebar.date_input("시작일", pd.to_datetime("2023-01-01"))
end_date = st.sidebar.date_input("종료일", pd.to_datetime("today"))

# ----------------------
# 데이터 로드
# ----------------------
@st.cache_data
def load_data(ticker, start, end, interval):

    if interval in ["1m", "5m", "15m"]:
        df = yf.download(ticker, period="1d", interval=interval)
    else:
        df = yf.download(
            ticker,
            start=start,
            end=end + pd.Timedelta(days=1),
            interval=interval
        )

    if df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.columns = [col.capitalize() for col in df.columns]
    df.index = pd.to_datetime(df.index)

    try:
        df.index = df.index.tz_localize("UTC").tz_convert("Asia/Seoul")
    except:
        df.index = df.index.tz_convert("Asia/Seoul")

    if interval in ["1m", "5m", "15m"]:
        df = df.between_time("09:00", "15:30")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])

    return df

# ----------------------
# 거래대금
# ----------------------
def format_korean_money(value):
    if value >= 1_0000_0000_0000:
        return f"{value / 1_0000_0000_0000:.2f}조"
    elif value >= 1_0000_0000:
        return f"{value / 1_0000_0000:.2f}억"
    else:
        return f"{value:,.0f}원"

# ----------------------
# 뉴스
# ----------------------
@st.cache_data(ttl=600)
def get_news(query):
    url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
    news = feedparser.parse(url)

    results = []
    for entry in news.entries[:10]:
        results.append({
            "title": entry.title,
            "link": entry.link
        })

    return results

def analyze_news(news_list):

    text = " ".join([n["title"] for n in news_list])

    positive = ["상승", "호재", "성장", "수혜", "강세"]
    negative = ["하락", "악재", "위기", "급락", "우려"]

    score = 0

    for p in positive:
        if p in text:
            score += 1
    for n in negative:
        if n in text:
            score -= 1

    if score > 1:
        return "🔥 긍정"
    elif score < -1:
        return "⚠️ 부정"
    else:
        return "➖ 중립"

# ----------------------
# 실행
# ----------------------
if st.sidebar.button("조회하기"):

    df = load_data(ticker, start_date, end_date, interval)

    if df.empty:
        st.error("데이터 없음")
        st.stop()

    st.subheader(f"📊 {selected_name} ({ticker})")

    # 이동평균
    ma_list = [5, 10, 20, 60, 120]
    for ma in ma_list:
        df[f"MA{ma}"] = df["Close"].rolling(ma).mean()

    # 🔥 Envelope (핵심 정확 계산)
    df["ENV_UPPER"] = df["MA20"] * 1.2
    df["ENV_LOWER"] = df["MA20"] * 0.8

    # 거래대금
    df["Value"] = ((df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4) * df["Volume"]

    # ----------------------
    # 차트
    # ----------------------
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True)

    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"],
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        increasing_line_color='red',
        decreasing_line_color='blue'
    ), row=1, col=1)

    for ma in ma_list:
        fig.add_trace(go.Scatter(x=df.index, y=df[f"MA{ma}"], name=f"MA{ma}"), row=1, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=df["ENV_UPPER"], name="상단", line=dict(color="black")), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["ENV_LOWER"], name="하단", line=dict(color="black")), row=1, col=1)

    fig.add_trace(go.Bar(x=df.index, y=df["Volume"], name="거래량"), row=2, col=1)

    st.plotly_chart(fig, use_container_width=True)

    # ----------------------
    # 현재가
    # ----------------------
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest["Close"])
    change = ((close - prev["Close"]) / prev["Close"]) * 100

    st.metric("현재가", f"{close:,.0f}")
    st.metric("변동률", f"{change:.2f}%")

    # ----------------------
    # 뉴스
    # ----------------------
    st.subheader("📰 뉴스")

    news_list = get_news(selected_name)

    for n in news_list:
        st.markdown(f"- [{n['title']}]({n['link']})")

    st.info(analyze_news(news_list))