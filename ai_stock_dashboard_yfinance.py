# ai_stock_dashboard_yfinance.py

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import feedparser

import kis_api
from kis_api import KISAPIError

st.set_page_config(page_title="AI 주식 대시보드", layout="wide")

st.markdown("""
<style>
.hts-card {
    background-color: rgba(127,127,127,0.06);
    border: 1px solid rgba(127,127,127,0.25);
    border-radius: 10px;
    padding: 16px 18px;
    margin-bottom: 14px;
}
.hts-card h4 {
    margin-top: 0;
    margin-bottom: 10px;
}
.price-up { color: #d43f3f; }
.price-down { color: #3f6fd4; }
.opinion-badge {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 999px;
    font-weight: 700;
    font-size: 1.05rem;
}
.opinion-buy { background-color: rgba(212,63,63,0.15); color: #d43f3f; }
.opinion-sell { background-color: rgba(63,111,212,0.15); color: #3f6fd4; }
.opinion-neutral { background-color: rgba(127,127,127,0.18); color: #808080; }
</style>
""", unsafe_allow_html=True)

st.title("📊 AI 주식 트레이딩 대시보드 (HTS PRO)")

# ----------------------
# 종목 CSV 불러오기
# ----------------------
@st.cache_data
def load_ticker_csv():
    try:
        df = pd.read_csv("krx_tickers.csv")
        return df
    except FileNotFoundError:
        st.error("❌ krx_tickers.csv 파일 없음 (GitHub 업로드 필요)")
        st.stop()

ticker_df = load_ticker_csv()

# ----------------------
# 사이드바: 종목 검색 / 조건 설정
# ----------------------
search = st.sidebar.text_input("종목 검색 (예: 삼성전자)")

filtered = ticker_df[
    ticker_df["회사명"].str.contains(search, case=False, na=False)
]

if not filtered.empty:
    options = filtered["회사명"].tolist()
    selected_name = st.sidebar.selectbox("종목 선택", options)
    ticker = ticker_df[ticker_df["회사명"] == selected_name]["티커"].values[0]
    code = kis_api.strip_market_suffix(ticker)
else:
    st.sidebar.warning("종목을 찾을 수 없습니다")
    st.stop()

chart_type = st.sidebar.selectbox(
    "봉 타입",
    ["일봉", "분봉(당일, 1분)", "분봉(당일, 5분)", "분봉(당일, 15분)"]
)

is_intraday = chart_type != "일봉"

if not is_intraday:
    start_date = st.sidebar.date_input("시작일", pd.to_datetime("2023-01-01"))
    end_date = st.sidebar.date_input("종료일", pd.to_datetime("today"))
else:
    st.sidebar.info("분봉은 KIS API 특성상 '당일' 데이터만 제공됩니다.")

refresh = st.sidebar.button("🔄 새로고침")

# ----------------------
# 데이터 로드 (KIS API)
# ----------------------
def load_chart_data():
    if not is_intraday:
        return kis_api.fetch_daily_chart(code, start_date, end_date)

    df = kis_api.fetch_minute_chart(code)
    if df.empty:
        return df

    resample_map = {
        "분봉(당일, 1분)": None,
        "분봉(당일, 5분)": "5min",
        "분봉(당일, 15분)": "15min",
    }
    rule = resample_map[chart_type]
    if rule is None:
        return df

    return df.resample(rule).agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Open", "High", "Low", "Close"])


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
# 뉴스 + 감성분석
# ----------------------
@st.cache_data(ttl=600)
def get_news(query):
    url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
    news = feedparser.parse(url)

    results = []
    for entry in news.entries[:10]:
        results.append({"title": entry.title, "link": entry.link})

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
        return "🔥 긍정", 1
    elif score < -1:
        return "⚠️ 부정", -1
    else:
        return "➖ 중립", 0

# ----------------------
# AI 투자의견 (규칙 기반 기술적 신호)
# ----------------------
def generate_ai_opinion(df, news_score, news_label):
    score = 0
    reasons = []

    latest = df.iloc[-1]
    ma5, ma20, ma60 = latest.get("MA5"), latest.get("MA20"), latest.get("MA60")
    close = latest["Close"]

    if pd.notna(ma5) and pd.notna(ma20) and pd.notna(ma60):
        if ma5 > ma20 > ma60:
            score += 1
            reasons.append(
                f"이동평균이 정배열(MA5 {ma5:,.0f} > MA20 {ma20:,.0f} > MA60 {ma60:,.0f})을 "
                "보이고 있어 단기 상승 추세로 해석됩니다."
            )
        elif ma5 < ma20 < ma60:
            score -= 1
            reasons.append(
                f"이동평균이 역배열(MA5 {ma5:,.0f} < MA20 {ma20:,.0f} < MA60 {ma60:,.0f})을 "
                "보이고 있어 단기 하락 추세로 해석됩니다."
            )

    if pd.notna(ma5) and pd.notna(ma20) and len(df) >= 4:
        diff = df["MA5"] - df["MA20"]
        recent_diff = diff.tail(4)
        if recent_diff.iloc[0] < 0 and recent_diff.iloc[-1] > 0:
            score += 1
            reasons.append("최근 3거래일 이내 MA5가 MA20을 상향 돌파하는 골든크로스가 발생했습니다.")
        elif recent_diff.iloc[0] > 0 and recent_diff.iloc[-1] < 0:
            score -= 1
            reasons.append("최근 3거래일 이내 MA5가 MA20을 하향 돌파하는 데드크로스가 발생했습니다.")

    upper, lower = latest.get("ENV_UPPER"), latest.get("ENV_LOWER")
    if pd.notna(upper) and close >= upper * 0.98:
        score -= 1
        reasons.append(f"현재가({close:,.0f})가 Envelope 상단({upper:,.0f})에 근접해 단기 과열 구간으로 판단됩니다.")
    elif pd.notna(lower) and close <= lower * 1.02:
        score += 1
        reasons.append(f"현재가({close:,.0f})가 Envelope 하단({lower:,.0f})에 근접해 단기 저평가 구간으로 판단됩니다.")

    if news_score != 0:
        reasons.append(
            f"최근 뉴스 감성분석 결과가 '{news_label}'로 나타나 "
            f"{'긍정적' if news_score > 0 else '부정적'} 요인으로 반영됩니다."
        )
    score += news_score

    if score >= 2:
        label, css = "🔥 매수 우위", "opinion-buy"
        verdict = "매수 신호가 매도 신호보다 우세하여 '매수 우위'로 판단됩니다."
    elif score <= -2:
        label, css = "⚠️ 매도 우위", "opinion-sell"
        verdict = "매도 신호가 매수 신호보다 우세하여 '매도 우위'로 판단됩니다."
    else:
        label, css = "➖ 중립", "opinion-neutral"
        verdict = "매수·매도 신호가 뚜렷하지 않거나 서로 엇갈려 '중립'으로 판단됩니다."

    if not reasons:
        reasons = ["뚜렷한 기술적 매수/매도 신호가 관측되지 않았습니다."]

    return label, css, reasons, verdict

# ----------------------
# 실행
# ----------------------
if refresh:

    try:
        price_info = kis_api.fetch_current_price(code)
        df = load_chart_data()
    except KISAPIError as e:
        st.error(f"❌ {e}")
        st.stop()

    if df.empty:
        st.error("데이터 없음")
        st.stop()

    ma_list = [5, 10, 20, 60, 120]
    for ma in ma_list:
        df[f"MA{ma}"] = df["Close"].rolling(ma).mean()

    df["ENV_UPPER"] = df["MA20"] * 1.2
    df["ENV_LOWER"] = df["MA20"] * 0.8
    df["Value"] = ((df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4) * df["Volume"]

    news_list = get_news(selected_name)
    news_label, news_score = analyze_news(news_list)

    # ----------------------
    # 상단 요약 스트립
    # ----------------------
    change = price_info["전일대비"]
    change_pct = price_info["등락률"]
    price_class = "price-up" if change >= 0 else "price-down"
    sign = "+" if change >= 0 else ""

    st.subheader(f"{selected_name} ({code})")

    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**현재가**<br><span class='{price_class}' style='font-size:1.4rem'>{price_info['현재가']:,.0f}</span>", unsafe_allow_html=True)
    c2.markdown(f"**전일대비**<br><span class='{price_class}' style='font-size:1.4rem'>{sign}{change:,.0f} ({sign}{change_pct:.2f}%)</span>", unsafe_allow_html=True)
    c3.markdown(f"**당일 거래대금**<br><span style='font-size:1.4rem'>{format_korean_money(price_info['누적거래대금'])}</span>", unsafe_allow_html=True)

    st.divider()

    # ----------------------
    # 메인 영역: 차트(좌) + AI의견(우)
    # ----------------------
    col_chart, col_side = st.columns([6, 4])

    with col_chart:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3])

        fig.add_trace(go.Candlestick(
            x=df.index,
            open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
            increasing_line_color='red', decreasing_line_color='blue',
            name="가격",
            hovertemplate=(
                "%{x}<br>"
                "시가: %{open:,.0f}원<br>"
                "고가: %{high:,.0f}원<br>"
                "저가: %{low:,.0f}원<br>"
                "종가: %{close:,.0f}원"
                "<extra></extra>"
            ),
        ), row=1, col=1)

        for ma in ma_list:
            fig.add_trace(go.Scatter(x=df.index, y=df[f"MA{ma}"], name=f"MA{ma}"), row=1, col=1)

        fig.add_trace(go.Scatter(x=df.index, y=df["ENV_UPPER"], name="Env 상단", line=dict(color="black")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=df["ENV_LOWER"], name="Env 하단", line=dict(color="black")), row=1, col=1)

        fig.add_trace(go.Bar(
            x=df.index, y=df["Value"], name="거래대금",
            hovertemplate="%{x}<br>거래대금: %{y:,.0f}원<extra></extra>",
        ), row=2, col=1)

        fig.update_layout(height=650, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, use_container_width=True)

    with col_side:
        label, css, reasons, verdict = generate_ai_opinion(df, news_score, news_label)

        st.markdown(f"""
        <div class="hts-card">
            <h4>🤖 AI 투자의견</h4>
            <span class="opinion-badge {css}">{label}</span>
            <ul style="margin-top:10px;">{"".join(f"<li>{r}</li>" for r in reasons)}</ul>
            <div style="font-weight:700; margin-top:6px;">→ {verdict}</div>
            <div style="font-size:0.8rem; opacity:0.7; margin-top:8px;">
                본 의견은 기술적 지표 기반 참고용이며 투자 조언이 아닙니다.
            </div>
        </div>
        """, unsafe_allow_html=True)

    # ----------------------
    # 뉴스 + 감성분석
    # ----------------------
    st.divider()
    st.subheader("📰 뉴스 + 감성분석")
    st.info(f"뉴스 종합 감성: {news_label}")

    for n in news_list:
        st.markdown(f"- [{n['title']}]({n['link']})")

else:
    st.info("왼쪽 사이드바에서 종목과 조건을 선택한 뒤 '새로고침' 버튼을 눌러주세요.")
