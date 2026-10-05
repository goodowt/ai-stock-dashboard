# dart_api.py
# 금융감독원 전자공시(OPEN DART) API 래퍼
#
# kis_api.py와 같은 방식으로, 키는 st.secrets에 있으면 쓰고 없으면 환경변수로 대체한다.
# 대시보드의 AI 투자의견(investment_opinion.py)에 넣을 재무제표·공시 목록을 가져온다.

import io
import os
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

import requests

try:
    import streamlit as st
except ImportError:
    st = None

BASE_URL = "https://opendart.fss.or.kr/api"

# 보고서 코드: 1분기 / 반기 / 3분기 / 사업보고서
REPORT_Q1 = "11013"
REPORT_HALF = "11012"
REPORT_Q3 = "11014"
REPORT_ANNUAL = "11011"

# 보고서 코드 -> 그 보고서가 새로 담는 분기
REPORT_QUARTER = {REPORT_Q1: 1, REPORT_HALF: 2, REPORT_Q3: 3, REPORT_ANNUAL: 4}

REPORT_LABELS = {
    REPORT_Q1: "1분기",
    REPORT_HALF: "상반기 누적",
    REPORT_Q3: "3분기 누적",
    REPORT_ANNUAL: "연간",
}

# 보고서 코드별 결산 기준일(월, 일). 12월 결산 법인 기준이며, 최신 보고서를 찾을 때
# "아직 기간이 끝나지도 않은 보고서"를 조회 후보에서 빼는 용도로만 쓴다.
_REPORT_PERIOD_END = {
    REPORT_Q1: (3, 31),
    REPORT_HALF: (6, 30),
    REPORT_Q3: (9, 30),
    REPORT_ANNUAL: (12, 31),
}

STATUS_OK = "000"
STATUS_NO_DATA = "013"

DISCLOSURE_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


class DARTAPIError(Exception):
    pass


class DARTResponseError(DARTAPIError):
    """상태 코드는 200인데 본문이 JSON이 아닌 경우(DART가 오류 안내 페이지를 돌려줄 때)."""


def _get_key():
    if st is not None:
        try:
            key = st.secrets["DART_API_KEY"]
            if key:
                return key
        except Exception:
            pass

    return os.environ.get("DART_API_KEY")


def has_key() -> bool:
    return bool(_get_key())


def _require_key():
    key = _get_key()
    if not key:
        raise DARTAPIError(
            "DART API 키가 설정되지 않았습니다. `.streamlit/secrets.toml`(앱) 또는 "
            "환경변수에 DART_API_KEY를 설정해주세요."
        )
    return key


MAX_NETWORK_RETRIES = 1

# (연결, 응답) 제한 시간. 해외 서버(배포 환경)에서는 DART 연결이 아예 안 붙는 때가 있는데,
# 그때 화면이 오래 멈추지 않도록 연결 쪽은 짧게 잡는다.
REQUEST_TIMEOUT = (5, 15)


def _get(path, params, _retry_count=0):
    try:
        return requests.get(
            f"{BASE_URL}/{path}",
            params={"crtfc_key": _require_key(), **params},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        if _retry_count < MAX_NETWORK_RETRIES:
            time.sleep(1.0)
            return _get(path, params, _retry_count=_retry_count + 1)
        # 예외 메시지에는 요청 URL(=API 키)이 들어가므로 종류만 남긴다.
        raise DARTAPIError(f"DART API 네트워크 오류: {type(e).__name__}")


def _request_page(path, params):
    """JSON API 호출. 조회 결과 없음(013)은 오류가 아니라 빈 목록으로 돌려준다.

    (목록, 전체 페이지 수)를 반환한다. 페이지 개념이 없는 API는 전체 페이지 수가 1이다.
    """

    res = _get(path, params)
    if res.status_code != 200:
        raise DARTAPIError(f"DART API 요청 실패 ({res.status_code})")

    try:
        data = res.json()
    except ValueError:
        raise DARTResponseError("DART API 응답을 해석할 수 없습니다.")

    status = data.get("status")
    if status == STATUS_NO_DATA:
        return [], 1
    if status != STATUS_OK:
        raise DARTAPIError(f"DART API 오류 ({status}): {data.get('message')}")

    return data.get("list", []), int(data.get("total_page") or 1)


def _request_json(path, params):
    rows, _ = _request_page(path, params)
    return rows


# 종목코드 -> DART 고유번호 매핑. 전체 목록(zip, 수 MB)을 한 번만 받아 프로세스 전체가 공유한다.
_corp_code_store = {"map": None, "lock": threading.Lock()}


def _load_corp_codes():
    res = _get("corpCode.xml", {})
    if res.status_code != 200:
        raise DARTAPIError(f"DART 고유번호 목록 요청 실패 ({res.status_code})")

    try:
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
    except zipfile.BadZipFile:
        # 키 오류 등은 zip이 아니라 상태 코드가 담긴 본문으로 온다.
        raise DARTAPIError(f"DART 고유번호 목록 오류: {res.text[:200]}")

    mapping = {}
    for item in ElementTree.fromstring(xml_bytes).iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        if stock_code:
            mapping[stock_code] = item.findtext("corp_code").strip()
    return mapping


def corp_code_map():
    """상장사 전체의 {종목코드: DART 고유번호}."""

    with _corp_code_store["lock"]:
        if _corp_code_store["map"] is None:
            _corp_code_store["map"] = _load_corp_codes()
        return _corp_code_store["map"]


def fetch_corp_code(stock_code: str):
    """종목코드(6자리)에 해당하는 DART 고유번호(8자리). 없으면 None."""

    return corp_code_map().get(stock_code)


def _to_number(value):
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pick_statement_rows(rows):
    """연결재무제표(CFS)가 있으면 연결, 없으면(종속회사 없는 법인) 개별(OFS)을 쓴다."""

    cfs = [r for r in rows if r.get("fs_div") == "CFS"]
    if cfs:
        return cfs, "CFS"
    return [r for r in rows if r.get("fs_div") == "OFS"], "OFS"


def _find_account(rows, name_prefix):
    # "당기순이익" / "당기순이익(손실)"처럼 표기가 조금씩 달라 앞부분으로 찾는다.
    for row in rows:
        if (row.get("account_nm") or "").strip().startswith(name_prefix):
            return row
    return None


def _flow_amounts(row):
    """손익 항목의 (당기, 전년 동기). 분·반기보고서는 누적 금액이 있으면 누적끼리 비교한다."""

    if row is None:
        return None, None
    current_cum = _to_number(row.get("thstrm_add_amount"))
    previous_cum = _to_number(row.get("frmtrm_add_amount"))
    if current_cum is not None and previous_cum is not None:
        return current_cum, previous_cum
    return _to_number(row.get("thstrm_amount")), _to_number(row.get("frmtrm_amount"))


def _stock_amount(rows, name_prefix):
    row = _find_account(rows, name_prefix)
    return _to_number(row.get("thstrm_amount")) if row else None


def candidate_reports(today: date):
    """기간이 이미 끝난 보고서를 최신순으로 나열한다(제출 여부는 조회해 봐야 안다)."""

    candidates = []
    for year in (today.year, today.year - 1, today.year - 2):
        for reprt_code, (month, day) in _REPORT_PERIOD_END.items():
            period_end = date(year, month, day)
            if period_end < today:
                candidates.append((period_end, str(year), reprt_code))
    candidates.sort(reverse=True)
    return [(year, reprt_code) for _, year, reprt_code in candidates]


MAX_REPORT_LOOKUPS = 5


def _fetch_key_accounts(corp_code, year, reprt_code):
    return _request_json(
        "fnlttSinglAcnt.json",
        {"corp_code": corp_code, "bsns_year": year, "reprt_code": reprt_code},
    )


MULTI_ACCOUNT_BATCH = 100


def fetch_key_accounts_multi(corp_codes, year, reprt_code):
    """여러 회사의 주요 계정을 한꺼번에 받아 {고유번호: 행 목록}으로 묶는다.

    전 종목을 도는 수집 스크립트(scripts/build_dart_data.py)용. 호출 한 번에 100개사까지 된다.
    해당 보고서를 내지 않은 회사는 결과에 없다.
    """

    grouped = {}
    for i in range(0, len(corp_codes), MULTI_ACCOUNT_BATCH):
        for row in _fetch_key_accounts_batch(corp_codes[i:i + MULTI_ACCOUNT_BATCH], year, reprt_code):
            grouped.setdefault(row["corp_code"], []).append(row)
    return grouped


def _fetch_key_accounts_batch(corp_codes, year, reprt_code):
    # 특정 회사 조합에서는 DART가 JSON 대신 오류 안내 페이지를 돌려준다(같은 묶음을 반으로
    # 나누면 정상). 그럴 때는 묶음을 쪼개 다시 받는다.
    try:
        return _request_json(
            "fnlttMultiAcnt.json",
            {"corp_code": ",".join(corp_codes), "bsns_year": year, "reprt_code": reprt_code},
        )
    except DARTResponseError:
        if len(corp_codes) == 1:
            raise
        half = len(corp_codes) // 2
        return (
            _fetch_key_accounts_batch(corp_codes[:half], year, reprt_code)
            + _fetch_key_accounts_batch(corp_codes[half:], year, reprt_code)
        )


def statement_division(all_rows):
    """주요 계정 응답에서 실제로 쓸 재무제표 구분(CFS 연결 / OFS 개별)."""

    return _pick_statement_rows(all_rows)[1]


def fetch_operating_cash_flow(corp_code, year, reprt_code, fs_div):
    """현금흐름표의 영업활동현금흐름(누적). 현금흐름표에 해당 계정이 없으면 None."""

    rows = _request_json(
        "fnlttSinglAcntAll.json",
        {"corp_code": corp_code, "bsns_year": year, "reprt_code": reprt_code, "fs_div": fs_div},
    )

    for row in rows:
        if row.get("account_id") == "ifrs-full_CashFlowsFromUsedInOperatingActivities":
            return _to_number(row.get("thstrm_amount"))
    for row in rows:
        name = (row.get("account_nm") or "").replace(" ", "")
        if row.get("sj_div") == "CF" and "영업활동" in name and "현금흐름" in name:
            return _to_number(row.get("thstrm_amount"))
    return None


def _annual_trend(rows, year):
    """사업보고서 한 건에 담긴 당기/전기/전전기로 최근 3개년 추이를 만든다(과거 -> 최근)."""

    accounts = {
        "매출액": _find_account(rows, "매출액"),
        "영업이익": _find_account(rows, "영업이익"),
        "당기순이익": _find_account(rows, "당기순이익"),
    }
    trend = []
    for offset, field in ((2, "bfefrmtrm_amount"), (1, "frmtrm_amount"), (0, "thstrm_amount")):
        entry = {"연도": str(int(year) - offset)}
        for name, row in accounts.items():
            entry[name] = _to_number(row.get(field)) if row else None
        if any(entry[name] is not None for name in accounts):
            trend.append(entry)
    return trend


# 대시보드가 실제로 쓰는 계정·필드. 전 종목의 여러 분기 보고서를 한꺼번에 메모리에 올리는
# 수집 스크립트에서, 응답 원본 대신 이것만 남겨 둔다.
_KEY_ACCOUNT_PREFIXES = ("매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계", "유동자산")
_KEY_ROW_FIELDS = (
    "fs_div", "account_nm", "thstrm_amount", "thstrm_add_amount",
    "frmtrm_amount", "frmtrm_add_amount", "bfefrmtrm_amount",
)


def trim_key_accounts(all_rows):
    rows, _ = _pick_statement_rows(all_rows)
    return [
        {field: row.get(field) for field in _KEY_ROW_FIELDS}
        for row in rows
        if (row.get("account_nm") or "").strip().startswith(_KEY_ACCOUNT_PREFIXES)
    ]


def _difference(total, part):
    return total - part if total is not None and part is not None else None


def _quarter_amounts(row, reprt_code, q3_row):
    """손익 계정의 해당 분기 3개월치 (당기, 전년 동기). 구할 수 없으면 (None, None)."""

    if row is None:
        return None, None

    if reprt_code == REPORT_ANNUAL:
        # 4분기는 따로 공시되지 않아, 연간 금액에서 3분기 누적을 빼서 구한다.
        if q3_row is None:
            return None, None
        return (
            _difference(_to_number(row.get("thstrm_amount")), _to_number(q3_row.get("thstrm_add_amount"))),
            _difference(_to_number(row.get("frmtrm_amount")), _to_number(q3_row.get("frmtrm_add_amount"))),
        )

    if reprt_code != REPORT_Q1 and _to_number(row.get("thstrm_add_amount")) is None:
        # 누적 금액이 따로 없으면 당기 금액이 3개월치인지 누적인지 알 수 없다.
        return None, None
    return _to_number(row.get("thstrm_amount")), _to_number(row.get("frmtrm_amount"))


QUARTERLY_TREND_COUNT = 4


def build_quarterly_trend(periods, reports):
    """최근 분기부터 연속된 최대 4개 분기의 3개월치 매출액·영업이익(과거 -> 최근).

    periods: 이 회사의 최신 보고서부터 과거 순으로 나열한 [(연도, 보고서코드)]
    reports: {(연도, 보고서코드): 주요 계정 행}
    """

    trend = []
    for year, reprt_code in periods[:QUARTERLY_TREND_COUNT]:
        all_rows = reports.get((year, reprt_code))
        if not all_rows:
            break
        rows, _ = _pick_statement_rows(all_rows)
        q3_rows = []
        if reprt_code == REPORT_ANNUAL:
            q3_rows, _ = _pick_statement_rows(reports.get((year, REPORT_Q3)) or [])

        entry = {"분기": f"{year} {REPORT_QUARTER[reprt_code]}Q"}
        for name in ("매출액", "영업이익"):
            current, previous = _quarter_amounts(
                _find_account(rows, name), reprt_code, _find_account(q3_rows, name)
            )
            entry[name] = current
            entry[f"{name}_전년"] = previous
        if entry["매출액"] is None and entry["영업이익"] is None:
            break
        trend.append(entry)
    return trend[::-1]


def build_financials(periods, reports, cash_flow):
    """한 회사의 주요 계정 응답을 대시보드용 수치로 정리한다.

    periods: 이 회사의 최신 보고서부터 과거 순으로 나열한 [(연도, 보고서코드)]. periods[0]이 기준 보고서다.
    reports: {(연도, 보고서코드): 주요 계정 행}. periods[0]은 반드시 들어 있어야 한다.
    cash_flow: fetch_operating_cash_flow()로 따로 구한 값.
    """

    year, reprt_code = periods[0]
    rows, fs_div = _pick_statement_rows(reports[(year, reprt_code)])

    # 최근 3개년 추이는 사업보고서에서만 나온다. 연초에는 직전 연도 사업보고서가 아직
    # 안 나왔을 수 있으므로, 받아 둔 것 중 가장 최근 사업보고서를 쓴다.
    annual_trend = []
    for annual_year, code in periods:
        if code == REPORT_ANNUAL and reports.get((annual_year, code)):
            annual_rows, _ = _pick_statement_rows(reports[(annual_year, code)])
            annual_trend = _annual_trend(annual_rows, annual_year)
            break

    revenue, revenue_prev = _flow_amounts(_find_account(rows, "매출액"))
    operating_income, operating_income_prev = _flow_amounts(_find_account(rows, "영업이익"))
    net_income, net_income_prev = _flow_amounts(_find_account(rows, "당기순이익"))

    return {
        "기준": f"{year}년 {REPORT_LABELS[reprt_code]}",
        "재무제표": "연결" if fs_div == "CFS" else "개별",
        "매출액": revenue,
        "매출액_전년동기": revenue_prev,
        "영업이익": operating_income,
        "영업이익_전년동기": operating_income_prev,
        "당기순이익": net_income,
        "당기순이익_전년동기": net_income_prev,
        "자산총계": _stock_amount(rows, "자산총계"),
        "부채총계": _stock_amount(rows, "부채총계"),
        "자본총계": _stock_amount(rows, "자본총계"),
        # 은행·보험·증권 등은 유동/비유동을 구분하지 않는다. 부채비율 해석을 건너뛰는 데 쓴다.
        "유동성구분": _find_account(rows, "유동자산") is not None,
        "영업활동현금흐름": cash_flow,
        "연간추이": annual_trend,
        "분기추이": build_quarterly_trend(periods, reports),
    }


def report_history(periods):
    """재무 수치를 만드는 데 필요한 보고서 수(최신 보고서 포함).

    분기 추이 4개 분기에 보고서 4개가 필요하고, 그중 가장 오래된 것이 사업보고서면
    4분기 금액을 구하려고 같은 해 3분기 보고서가 하나 더 필요하다.
    """

    count = QUARTERLY_TREND_COUNT
    if len(periods) >= count and periods[count - 1][1] == REPORT_ANNUAL:
        count += 1
    return count


def fetch_financials(corp_code: str, today: date = None):
    """가장 최근에 제출된 정기보고서 기준 주요 재무 수치. 제출된 보고서가 없으면 None."""

    today = today or datetime.now().date()
    candidates = candidate_reports(today)

    periods = None
    reports = {}
    for i, (year, reprt_code) in enumerate(candidates[:MAX_REPORT_LOOKUPS]):
        rows = _fetch_key_accounts(corp_code, year, reprt_code)
        if rows:
            periods = candidates[i:]
            reports[(year, reprt_code)] = rows
            break
    if periods is None:
        return None

    # 3개년 추이·분기 추이용 과거 보고서. 부가 정보라 실패하면 있는 것만으로 만든다.
    for year, reprt_code in periods[1:report_history(periods)]:
        try:
            reports[(year, reprt_code)] = _fetch_key_accounts(corp_code, year, reprt_code)
        except DARTAPIError:
            break

    year, reprt_code = periods[0]
    try:
        cash_flow = fetch_operating_cash_flow(
            corp_code, year, reprt_code, statement_division(reports[(year, reprt_code)])
        )
    except DARTAPIError:
        cash_flow = None

    return build_financials(periods, reports, cash_flow)


MAX_DISCLOSURE_PAGES = 10


def fetch_disclosures(corp_code: str, days: int = 90, today: date = None):
    """최근 `days`일간 공시 목록(최신순). 각 항목: 보고서명 / 접수일 / 링크."""

    today = today or datetime.now().date()
    params = {
        "corp_code": corp_code,
        "bgn_de": (today - timedelta(days=days)).strftime("%Y%m%d"),
        "end_de": today.strftime("%Y%m%d"),
        "page_count": 100,
    }

    # 대형주는 임원 지분 보고 등으로 90일간 100건을 넘기도 해서 여러 페이지를 이어 받는다.
    rows, page_no = [], 1
    while True:
        page_rows, total_page = _request_page("list.json", {**params, "page_no": page_no})
        rows.extend(page_rows)
        if page_no >= min(total_page, MAX_DISCLOSURE_PAGES):
            break
        page_no += 1

    return [_disclosure_item(row) for row in rows]


def _disclosure_item(row):
    return {
        "보고서명": (row.get("report_nm") or "").strip(),
        "접수일": row.get("rcept_dt", ""),
        "링크": DISCLOSURE_URL.format(rcept_no=row.get("rcept_no", "")),
    }


def fetch_market_disclosures(corp_cls: str, days: int = 90, today: date = None):
    """시장 전체(corp_cls Y: 유가증권, K: 코스닥)의 최근 공시를 {종목코드: 공시 목록}으로 묶는다.

    전 종목을 도는 수집 스크립트용. 회사를 지정하지 않는 조회는 기간이 3개월까지만 허용된다.
    """

    today = today or datetime.now().date()
    params = {
        "corp_cls": corp_cls,
        "bgn_de": (today - timedelta(days=days)).strftime("%Y%m%d"),
        "end_de": today.strftime("%Y%m%d"),
        "page_count": 100,
    }

    grouped, page_no = {}, 1
    while True:
        rows, total_page = _request_page("list.json", {**params, "page_no": page_no})
        for row in rows:
            stock_code = (row.get("stock_code") or "").strip()
            if stock_code:
                grouped.setdefault(stock_code, []).append(_disclosure_item(row))
        if page_no >= total_page:
            break
        page_no += 1
    return grouped
