# ai_stock_dashboard_yfinance.py

import html

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import feedparser

import dart_api
import investment_opinion
import kis_api
from dart_api import DARTAPIError
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
.opinion-total { margin-left: 8px; font-weight: 700; }
.opinion-cat {
    border-top: 1px solid rgba(127,127,127,0.25);
    margin-top: 10px;
    padding-top: 8px;
}
.opinion-cat-head { display: flex; justify-content: space-between; font-weight: 700; }
.opinion-cat ul { margin: 4px 0 0 0; padding-left: 18px; font-size: 0.9rem; }
.opinion-skip { opacity: 0.6; font-weight: 400; }
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
# 재무제표 + 공시 (DART)
# ----------------------
DISCLOSURE_DAYS = 90

@st.cache_data(ttl=3600, show_spinner=False)
def load_fundamentals(code):
    corp_code = dart_api.fetch_corp_code(code)
    if corp_code is None:
        return None, None
    return (
        dart_api.fetch_financials(corp_code),
        dart_api.fetch_disclosures(corp_code, DISCLOSURE_DAYS),
    )

def format_ratio(value, suffix, digits=1):
    return f"{value:,.{digits}f}{suffix}" if value else "-"

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

    ma_list = [5, 7, 10, 15, 20]
    for ma in ma_list:
        df[f"MA{ma}"] = df["Close"].rolling(ma).mean()

    df["ENV_UPPER"] = df["MA20"] * 1.2
    df["ENV_LOWER"] = df["MA20"] * 0.8
    df["Value"] = ((df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4) * df["Volume"]
    df["Value_억"] = df["Value"] / 1_0000_0000

    news_list = get_news(selected_name)
    news_label, news_score = analyze_news(news_list)

    financials, disclosures, dart_notice = None, None, None
    if not dart_api.has_key():
        dart_notice = "DART_API_KEY가 설정되지 않아 실적·공시 분석은 제외했습니다."
    else:
        try:
            financials, disclosures = load_fundamentals(code)
        except DARTAPIError as e:
            dart_notice = f"DART 조회에 실패해 실적·공시 분석은 제외했습니다. ({e})"
        else:
            if financials is None and disclosures is None:
                dart_notice = "DART에 등록된 기업이 아니어서(ETF 등) 실적·공시 분석은 제외했습니다."

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
            fig.add_trace(go.Scatter(
                x=df.index, y=df[f"MA{ma}"], name=f"MA{ma}", hoverinfo="skip",
            ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df.index, y=df["ENV_UPPER"], name="Env 상단", line=dict(color="black"), hoverinfo="skip",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df.index, y=df["ENV_LOWER"], name="Env 하단", line=dict(color="black"), hoverinfo="skip",
        ), row=1, col=1)

        fig.add_trace(go.Bar(
            x=df.index, y=df["Value_억"], name="거래대금",
            hovertemplate="%{x}<br>거래대금: %{y:,.0f}억원<extra></extra>",
        ), row=2, col=1)

        fig.update_xaxes(showticklabels=True, rangeslider_visible=False, row=1, col=1)
        fig.update_xaxes(showticklabels=True, row=2, col=1)
        fig.update_yaxes(tickformat=",.0f", ticksuffix="억", row=2, col=1)

        fig.update_layout(height=650, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, use_container_width=True)

    with col_side:
        opinion = investment_opinion.generate_opinion(
            df, news_score, news_label, price_info, financials, disclosures, DISCLOSURE_DAYS
        )

        category_html = ""
        for category in opinion["categories"]:
            if not category["available"]:
                score_html = '<span class="opinion-skip">평가 제외</span>'
            else:
                score_class = "price-up" if category["score"] > 0 else "price-down" if category["score"] < 0 else ""
                score_text = f'{category["score"]:+d}' if category["score"] else "0"
                score_html = f'<span class="{score_class}">{score_text}</span>'
            items = "".join(f"<li>{html.escape(r)}</li>" for r in category["reasons"])
            category_html += (
                '<div class="opinion-cat">'
                f'<div class="opinion-cat-head"><span>{category["name"]}</span>{score_html}</div>'
                f"<ul>{items}</ul>"
                "</div>"
            )

        st.markdown(
            '<div class="hts-card">'
            "<h4>🤖 AI 투자의견</h4>"
            f'<span class="opinion-badge {opinion["css"]}">{opinion["label"]}</span>'
            f'<span class="opinion-total">종합 {opinion["total"]:+d}점 (±{opinion["max_total"]}점 만점)</span>'
            f"{category_html}"
            f'<div class="opinion-cat" style="font-weight:700;">→ {html.escape(opinion["verdict"])}</div>'
            '<div style="font-size:0.8rem; opacity:0.7; margin-top:8px;">'
            "본 의견은 재무제표·공시·기술적 지표를 정해진 규칙으로 채점한 참고용이며 투자 조언이 아닙니다. "
            "밸류에이션은 업종 평균과 비교하지 않고 절대 수준으로 판단합니다."
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )

        if dart_notice:
            st.caption(f"ℹ️ {dart_notice}")

    # ----------------------
    # 재무 요약 + 주요 공시
    # ----------------------
    st.divider()
    st.subheader("📑 재무 요약")

    eps, bps = price_info["EPS"], price_info["BPS"]
    roe = eps / bps * 100 if eps and bps > 0 else None
    margin = debt_ratio = None
    if financials:
        if financials["매출액"] and financials["영업이익"] is not None:
            margin = financials["영업이익"] / financials["매출액"] * 100
        if financials["부채총계"] is not None and financials["자본총계"]:
            debt_ratio = financials["부채총계"] / financials["자본총계"] * 100

    f1, f2, f3, f4, f5 = st.columns(5)
    f1.metric("PER", format_ratio(price_info["PER"], "배"))
    f2.metric("PBR", format_ratio(price_info["PBR"], "배", 2))
    f3.metric("ROE", format_ratio(roe, "%"))
    f4.metric("영업이익률", format_ratio(margin, "%"))
    f5.metric("부채비율", format_ratio(debt_ratio, "%", 0))

    if financials:
        st.caption(f"영업이익률·부채비율은 {financials['기준']} {financials['재무제표']}재무제표 기준입니다.")
        if financials["연간추이"]:
            # 금융업처럼 매출액 계정이 없으면 열 전체가 None이라 숫자형으로 맞춰 둔다.
            trend = pd.DataFrame(financials["연간추이"]).set_index("연도").apply(pd.to_numeric)
            revenue = trend["매출액"].where(trend["매출액"] > 0)
            trend["영업이익률"] = (trend["영업이익"] / revenue * 100).map(
                lambda v: f"{v:.1f}%" if pd.notna(v) else "-"
            )
            for col in ["매출액", "영업이익", "당기순이익"]:
                trend[col] = trend[col].map(
                    lambda v: investment_opinion.format_amount(v) if pd.notna(v) else "-"
                )
            st.dataframe(trend)

    if disclosures is not None:
        st.subheader(f"📢 최근 {DISCLOSURE_DAYS}일 주요 공시")
        flagged = investment_opinion.classify_disclosures(disclosures)
        if not flagged:
            st.info("주가에 영향을 줄 만한 주요 공시가 없습니다.")
        # 대형주는 수백 건이라 건별로 그리지 않고 한 번에 그린다.
        st.markdown("\n".join(
            f"- {'🔴' if d['점수'] > 0 else '🔵'} {d['접수일']} [{d['보고서명']}]({d['링크']}) — {d['분류']}"
            for d in flagged
        ))

        with st.expander(f"전체 공시 {len(disclosures)}건 보기"):
            st.markdown("\n".join(
                f"- {d['접수일']} [{d['보고서명']}]({d['링크']})" for d in disclosures
            ))

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
