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
# 🔥 KRX 전체 종목 불러오기
# ----------------------
@st.cache_data
def load_krx_ticker_list():

    def get_market(market_type):
        url = f"https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType={market_type}"
        res = requests.get(url)
        res.encoding = "cp949"

        df = pd.read_html(res.text, header=0)[0]
        df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
        return df

    kospi = get_market("stockMkt")
    kosdaq = get_market("kosdaqMkt")

    kospi["티커"] = kospi["종목코드"] + ".KS"
    kosdaq["티커"] = kosdaq["종목코드"] + ".KQ"

    df = pd.concat([kospi, kosdaq])
    return df[["회사명", "티커"]]

ticker_df = load_krx_ticker_list()

# ----------------------
# 🔍 종목 검색
# ----------------------
search = st.sidebar.text_input("종목 검색 (예: 삼성전자)")
filtered = ticker_df[ticker_df["회사명"].str.contains(search, case=False, na=False)]

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
# 거래대금 포맷
# ----------------------
def format_korean_money(value):
    if value >= 1_0000_0000_0000:
        return f"{value / 1_0000_0000_0000:.2f}조"
    elif value >= 1_0000_0000:
        return f"{value / 1_0000_0000:.2f}억"
    else:
        return f"{value:,.0f}원"

# ----------------------
# 📰 뉴스 가져오기
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

# ----------------------
# 🤖 뉴스 분석
# ----------------------
def analyze_news(news_list):

    text = " ".join([n["title"] for n in news_list])

    positive_words = ["상승", "호재", "성장", "수혜", "강세", "기대"]
    negative_words = ["하락", "악재", "위기", "급락", "우려", "매도"]

    score = 0

    for word in positive_words:
        if word in text:
            score += 1

    for word in negative_words:
        if word in text:
            score -= 1

    if score > 1:
        return "🔥 긍정 (매수 우위)"
    elif score < -1:
        return "⚠️ 부정 (매도 우위)"
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

    st.subheader(f"📊 {selected_name} ({ticker}) / {interval}")

    # 이동평균선
    ma_list = [5, 7, 10, 15, 20, 60, 120]
    for ma in ma_list:
        df[f"MA{ma}"] = df["Close"].rolling(ma).mean()

    # 🔥 Envelope (중심선 제외)
    df["ENV_UPPER"] = df["MA20"] * 1.2
    df["ENV_LOWER"] = df["MA20"] * 0.8

    # 거래대금
    df["Value"] = ((df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4) * df["Volume"]

    # ----------------------
    # 차트
    # ----------------------
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.03
    )

    # 캔들
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"],
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        increasing_line_color='red',
        decreasing_line_color='blue',
        name="캔들"
    ), row=1, col=1)

    # 이동평균선
    for ma in ma_list:
        fig.add_trace(go.Scatter(
            x=df.index,
            y=df[f"MA{ma}"],
            name=f"MA{ma}",
            line=dict(width=1)
        ), row=1, col=1)

    # 🔥 Envelope (상단/하단만)
    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["ENV_UPPER"],
        name="Envelope 상단",
        line=dict(color="black", width=1, dash="dot")
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["ENV_LOWER"],
        name="Envelope 하단",
        line=dict(color="black", width=1, dash="dot")
    ), row=1, col=1)

    # 거래량
    fig.add_trace(go.Bar(
        x=df.index,
        y=df["Volume"],
        name="거래량",
        opacity=0.5
    ), row=2, col=1)

    # 거래대금
    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["Value"],
        name="거래대금",
        line=dict(width=1)
    ), row=2, col=1)

    fig.update_layout(
        height=900,
        xaxis_rangeslider_visible=False,
        yaxis=dict(tickformat=",", title="가격 (₩)"),
        legend=dict(orientation="h"),
        hovermode="x unified"
    )

    st.plotly_chart(fig, use_container_width=True)

    # ----------------------
    # 현재가 정보
    # ----------------------
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest["Close"])
    prev_close = float(prev["Close"])
    change = ((close - prev_close) / prev_close) * 100
    value = int(latest["Value"])

    col1, col2, col3 = st.columns(3)

    col1.metric("현재가", f"₩{close:,.0f}")
    col2.metric("변동률", f"{change:.2f}%")
    col3.metric("거래대금", format_korean_money(value))

    # ----------------------
    # AI 매매 시그널
    # ----------------------
    st.subheader("🤖 AI 매매 시그널")

    if latest["MA5"] > latest["MA20"]:
        st.success("🔥 단기 상승")
    else:
        st.warning("⚠️ 단기 약세")

    if latest["MA20"] > latest["MA60"]:
        st.success("🔥 중기 상승")
    else:
        st.warning("⚠️ 중기 약세")

    # ----------------------
    # 📰 뉴스
    # ----------------------
    st.subheader("📰 최신 뉴스 분석")

    news_list = get_news(selected_name)

    if not news_list:
        st.warning("뉴스 없음")
    else:
        for n in news_list:
            st.markdown(f"- [{n['title']}]({n['link']})")

        sentiment = analyze_news(news_list)

        st.subheader("🤖 뉴스 기반 AI 판단")
        st.info(sentiment)

    # ----------------------
    # 데이터
    # ----------------------
    st.subheader("📋 데이터")
    st.dataframe(df.tail(50))