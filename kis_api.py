# kis_api.py
# 한국투자증권 KIS Open API (모의투자) 래퍼

import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st

# 모의투자 도메인 (실전 계좌는 openapi.koreainvestment.com:9443)
BASE_URL = "https://openapivts.koreainvestment.com:29443"

TR_ID_CURRENT_PRICE = "FHKST01010100"
TR_ID_DAILY_CHART = "FHKST03010100"
TR_ID_MINUTE_CHART = "FHKST03010200"


class KISAPIError(Exception):
    pass


def _get_keys():
    try:
        app_key = st.secrets["KIS_APP_KEY"]
        app_secret = st.secrets["KIS_APP_SECRET"]
    except (KeyError, FileNotFoundError):
        raise KISAPIError(
            "KIS API 키가 설정되지 않았습니다. `.streamlit/secrets.toml`에 "
            "KIS_APP_KEY / KIS_APP_SECRET을 입력해주세요."
        )
    if not app_key or not app_secret:
        raise KISAPIError("KIS_APP_KEY / KIS_APP_SECRET 값이 비어 있습니다.")
    return app_key, app_secret


def get_access_token():
    """OAuth 토큰을 발급받아 session_state에 캐싱하고, 만료 전까지 재사용한다."""

    cached = st.session_state.get("kis_token")
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["access_token"]

    app_key, app_secret = _get_keys()

    res = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": app_key,
            "appsecret": app_secret,
        },
        timeout=10,
    )

    if res.status_code != 200:
        raise KISAPIError(f"KIS 토큰 발급 실패 ({res.status_code}): {res.text}")

    data = res.json()
    access_token = data.get("access_token")
    expires_in = int(data.get("expires_in", 86400))

    if not access_token:
        raise KISAPIError(f"KIS 토큰 발급 응답에 access_token이 없습니다: {data}")

    st.session_state["kis_token"] = {
        "access_token": access_token,
        "expires_at": time.time() + expires_in,
    }
    return access_token


def _headers(tr_id, retry_token=None):
    app_key, app_secret = _get_keys()
    token = retry_token or get_access_token()
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }


def _request(path, tr_id, params):
    """401(토큰 만료) 시 토큰을 재발급받아 한 번 더 시도한다."""

    headers = _headers(tr_id)
    res = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)

    if res.status_code == 401:
        st.session_state.pop("kis_token", None)
        headers = _headers(tr_id)
        res = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)

    if res.status_code != 200:
        raise KISAPIError(f"KIS API 요청 실패 ({res.status_code}): {res.text}")

    data = res.json()
    if data.get("rt_cd") not in (None, "0"):
        raise KISAPIError(f"KIS API 오류 ({data.get('rt_cd')}): {data.get('msg1')}")

    return data


def strip_market_suffix(ticker: str) -> str:
    """'005930.KS' -> '005930', '0010F0.KQ' -> '0010F0'"""
    return ticker.split(".")[0]


def fetch_current_price(code: str) -> dict:
    """주식현재가 시세 조회. 현재가/등락률/거래량/52주 최고·최저/시가총액 등을 반환."""

    data = _request(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        TR_ID_CURRENT_PRICE,
        {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
    )
    output = data.get("output", {})

    def to_float(key, default=0.0):
        try:
            return float(output.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "현재가": to_float("stck_prpr"),
        "전일대비": to_float("prdy_vrss"),
        "등락률": to_float("prdy_ctrt"),
        "누적거래량": to_float("acml_vol"),
        "누적거래대금": to_float("acml_tr_pbmn"),
        "52주최고": to_float("w52_hgpr"),
        "52주최저": to_float("w52_lwpr"),
        "시가총액": to_float("hts_avls"),
    }


def fetch_daily_chart(code: str, start: datetime, end: datetime) -> pd.DataFrame:
    """국내주식 기간별시세(일봉). 기존 yfinance 코드와 동일한 컬럼 스키마로 반환."""

    data = _request(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        TR_ID_DAILY_CHART,
        {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": code,
            "fid_input_date_1": start.strftime("%Y%m%d"),
            "fid_input_date_2": end.strftime("%Y%m%d"),
            "fid_period_div_code": "D",
            "fid_org_adj_prc": "1",
        },
    )

    rows = data.get("output2", [])
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "stck_bsop_date": "Date",
        "stck_oprc": "Open",
        "stck_hgpr": "High",
        "stck_lwpr": "Low",
        "stck_clpr": "Close",
        "acml_vol": "Volume",
    })

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d")
    df = df.set_index("Date").sort_index()
    df = df.dropna(subset=["Open", "High", "Low", "Close"])

    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_minute_chart(code: str) -> pd.DataFrame:
    """국내주식 당일 분봉조회. KIS API 특성상 '당일' 데이터만 제공된다."""

    data = _request(
        "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        TR_ID_MINUTE_CHART,
        {
            "fid_etc_cls_code": "",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": code,
            "fid_input_hour_1": "153000",
            "fid_pw_data_incu_yn": "Y",
        },
    )

    rows = data.get("output2", [])
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "stck_cntg_hour": "Time",
        "stck_oprc": "Open",
        "stck_hgpr": "High",
        "stck_lwpr": "Low",
        "stck_prpr": "Close",
        "cntg_vol": "Volume",
    })

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    today = datetime.now().strftime("%Y%m%d")
    df["Date"] = pd.to_datetime(today + df["Time"], format="%Y%m%d%H%M%S")
    df = df.set_index("Date").sort_index()
    df = df.dropna(subset=["Open", "High", "Low", "Close"])

    return df[["Open", "High", "Low", "Close", "Volume"]]
