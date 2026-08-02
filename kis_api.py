# kis_api.py
# 한국투자증권 KIS Open API (모의투자) 래퍼
#
# Streamlit 앱(ai_stock_dashboard_yfinance.py)과, Streamlit 없이 도는
# GitHub Actions 스크립트(scripts/envelope_alert.py) 양쪽에서 공용으로 쓴다.
# 그래서 키/캐시 저장소는 st.secrets·st.cache_resource에 강하게 의존하지 않고,
# 있으면 쓰고 없으면 환경변수·모듈 전역 변수로 대체한다.

import os
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

try:
    import streamlit as st
except ImportError:
    st = None

# 모의투자 도메인 (실전 계좌는 openapi.koreainvestment.com:9443)
BASE_URL = "https://openapivts.koreainvestment.com:29443"

TR_ID_CURRENT_PRICE = "FHKST01010100"
TR_ID_DAILY_CHART = "FHKST03010100"
TR_ID_MINUTE_CHART = "FHKST03010200"


class KISAPIError(Exception):
    pass


def _get_keys():
    if st is not None:
        try:
            app_key = st.secrets["KIS_APP_KEY"]
            app_secret = st.secrets["KIS_APP_SECRET"]
            if app_key and app_secret:
                return app_key, app_secret
        except Exception:
            pass

    app_key = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    if not app_key or not app_secret:
        raise KISAPIError(
            "KIS API 키가 설정되지 않았습니다. `.streamlit/secrets.toml`(앱) 또는 "
            "환경변수(스크립트)에 KIS_APP_KEY / KIS_APP_SECRET을 설정해주세요."
        )
    return app_key, app_secret


# 세션(session_state)이 아니라 프로세스 전체가 공유하는 저장소.
# KIS 토큰 발급은 앱키 기준 1분당 1회 제한이라, 세션/실행별로 따로 캐싱하면
# 동시 접속자·반복 실행이 늘어날 때마다 재발급이 충돌해 403이 난다.
# 모듈 전역 변수는 같은 프로세스(Streamlit 서버 하나, 혹은 스크립트 한 번 실행) 안에서
# 자연히 공유되므로 st.cache_resource 없이도 동일하게 동작한다.
_token_store_singleton = {"access_token": None, "expires_at": 0.0, "lock": threading.RLock()}


def _token_store():
    return _token_store_singleton


def get_access_token(force_refresh=False, _retry_count=0):
    """OAuth 토큰을 앱 전체에서 공유 캐싱하고, 만료 전까지 재사용한다.

    앱(Streamlit 세션)과 GitHub Actions 스크립트가 같은 앱키를 쓰다 보면
    1분당 1회 제한(EGW00133)에 우연히 동시에 걸릴 수 있어, 그 경우 한 번은
    65초 기다렸다가 자동 재시도한다(대화형 앱이 아닌 배치 스크립트에도 쓰이므로 허용).
    """

    store = _token_store()

    with store["lock"]:
        if not force_refresh and store["access_token"] and store["expires_at"] > time.time() + 60:
            return store["access_token"]

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
            if "EGW00133" in res.text and _retry_count < 1:
                time.sleep(65)
                return get_access_token(force_refresh=force_refresh, _retry_count=_retry_count + 1)
            raise KISAPIError(f"KIS 토큰 발급 실패 ({res.status_code}): {res.text}")

        data = res.json()
        access_token = data.get("access_token")
        expires_in = int(data.get("expires_in", 86400))

        if not access_token:
            raise KISAPIError(f"KIS 토큰 발급 응답에 access_token이 없습니다: {data}")

        store["access_token"] = access_token
        store["expires_at"] = time.time() + expires_in
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


# 모의투자는 초당 호출 제한이 엄격해서, 앱 전체에서 호출 간격을 최소치로 강제한다.
MIN_REQUEST_INTERVAL = 0.5


_rate_limiter_singleton = {"last_call": 0.0, "lock": threading.Lock()}


def _rate_limiter():
    return _rate_limiter_singleton


def _throttle():
    limiter = _rate_limiter()
    with limiter["lock"]:
        wait = limiter["last_call"] + MIN_REQUEST_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        limiter["last_call"] = time.time()


def _request(path, tr_id, params, _retry_count=0):
    """401(토큰 만료) 및 초당 호출 제한 초과(EGW00201) 시 자동 재시도한다."""

    _throttle()
    headers = _headers(tr_id)
    res = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)

    if res.status_code == 401:
        token = get_access_token(force_refresh=True)
        _throttle()
        headers = _headers(tr_id, retry_token=token)
        res = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)

    try:
        data = res.json()
    except ValueError:
        data = None

    # 초당 호출 제한(EGW00201)은 HTTP 상태코드가 200이 아닌 경우에도 응답 본문에 담겨 오므로
    # status_code 체크보다 먼저 확인해서 재시도한다.
    if isinstance(data, dict) and data.get("msg_cd") == "EGW00201" and _retry_count < 3:
        time.sleep(1.0)
        return _request(path, tr_id, params, _retry_count=_retry_count + 1)

    if res.status_code != 200:
        raise KISAPIError(f"KIS API 요청 실패 ({res.status_code}): {res.text}")

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
        "오늘저가": to_float("stck_lwpr"),
        "오늘고가": to_float("stck_hgpr"),
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
