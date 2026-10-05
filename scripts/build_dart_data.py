# scripts/build_dart_data.py
#
# 하루 두 번 실행: 전 종목의 DART 재무제표·공시를 받아 dart_data.json에 저장한다.
#
# 배포된 대시보드(해외 서버)에서는 DART 연결이 ConnectTimeout으로 실패하는 일이 잦아,
# 앱이 DART를 직접 부르지 않고 이 파일을 읽게 한다. 종목별로 따로 조회하면 하루 수천 번을
# 불러야 하므로, 재무제표는 100개사씩·공시는 시장 전체를 한꺼번에 받는다.
# 영업활동현금흐름만은 종목별 조회라, 최신 보고서가 바뀐 종목에 한해서만 다시 받는다.

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import dart_api  # noqa: E402
import investment_opinion  # noqa: E402
import kis_api  # noqa: E402

KST = timezone(timedelta(hours=9))
DISCLOSURE_DAYS = 90

TICKERS_CSV = os.path.join(REPO_ROOT, "krx_tickers.csv")
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dart_data.json")

# 배치는 화면이 멈출 걱정이 없으니 순간적인 연결 실패에는 넉넉히 재시도한다.
dart_api.MAX_NETWORK_RETRIES = 5


def load_codes():
    with open(TICKERS_CSV, encoding="utf-8-sig") as f:
        return [kis_api.strip_market_suffix(row["티커"]) for row in csv.DictReader(f)]


def load_previous():
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            return json.load(f)["stocks"]
    except (OSError, ValueError, KeyError):
        return {}


def collect_reports(corp_codes, today):
    """회사별 최신 정기보고서와, 3개년·분기 추이에 필요한 그 이전 보고서들을 받는다.

    반환: ({고유번호: 최신순 보고서 목록 [(연도, 보고서코드)]}, {(연도, 보고서코드): {고유번호: 행}})
    """

    candidates = dart_api.candidate_reports(today)

    latest_index = {}
    reports = {}
    for index, (year, reprt_code) in enumerate(candidates):
        wanted = []
        for corp_code in corp_codes:
            if corp_code in latest_index:
                periods = candidates[latest_index[corp_code]:]
                if index < latest_index[corp_code] + dart_api.report_history(periods):
                    wanted.append(corp_code)
            elif index < dart_api.MAX_REPORT_LOOKUPS:
                wanted.append(corp_code)
        if not wanted:
            break

        found = dart_api.fetch_key_accounts_multi(sorted(wanted), year, reprt_code)
        reports[(year, reprt_code)] = {
            corp_code: dart_api.trim_key_accounts(rows) for corp_code, rows in found.items()
        }
        for corp_code in found:
            latest_index.setdefault(corp_code, index)
        print(
            f"{year}년 {dart_api.REPORT_LABELS[reprt_code]} 보고서: "
            f"{len(wanted)}개사 조회, {len(found)}개사 제출"
        )

    periods = {corp_code: candidates[index:] for corp_code, index in latest_index.items()}
    return periods, reports


def collect_disclosures(today):
    disclosures = {}
    for corp_cls, market in (("Y", "유가증권"), ("K", "코스닥")):
        found = dart_api.fetch_market_disclosures(corp_cls, DISCLOSURE_DAYS, today)
        disclosures.update(found)
        print(f"{market} 최근 {DISCLOSURE_DAYS}일 공시: {sum(len(v) for v in found.values())}건 ({len(found)}개사)")
    return disclosures


def compact_numbers(value):
    """금액은 원 단위 정수라 소수점(.0)을 떼어 파일 크기를 줄인다."""

    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: compact_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [compact_numbers(v) for v in value]
    return value


# 현금흐름 조회가 연달아 이만큼 실패하면 DART가 막힌 것으로 보고 남은 종목은 다음 실행으로 미룬다.
# (실패 한 건이 재시도까지 30초 넘게 걸려서, 계속 시도하면 작업 제한 시간을 넘겨 버린다.)
MAX_CASH_FLOW_FAILURES = 10
_cash_flow_failures = {"consecutive": 0}


def fetch_cash_flow(code, corp_code, year, reprt_code, fs_div):
    """(값, 다음 실행 때 다시 받아야 하는지)"""

    if _cash_flow_failures["consecutive"] >= MAX_CASH_FLOW_FAILURES:
        return None, True
    try:
        value = dart_api.fetch_operating_cash_flow(corp_code, year, reprt_code, fs_div)
    except dart_api.DARTAPIError as e:
        _cash_flow_failures["consecutive"] += 1
        print(f"{code} 영업활동현금흐름 조회 실패: {e}")
        if _cash_flow_failures["consecutive"] == MAX_CASH_FLOW_FAILURES:
            print("현금흐름 조회가 연달아 실패해 남은 종목은 다음 실행으로 미룹니다.")
        return None, True
    _cash_flow_failures["consecutive"] = 0
    return value, False


def build_stock(code, corp_code, periods, reports, disclosures, previous):
    record = {"financials": None, "cash_flow_pending": False}

    if corp_code in periods:
        company_periods = periods[corp_code]
        company_reports = {
            period: reports[period][corp_code]
            for period in company_periods[:dart_api.report_history(company_periods)]
            if corp_code in reports.get(period, {})
        }
        year, reprt_code = company_periods[0]
        rows = company_reports[(year, reprt_code)]
        financials = dart_api.build_financials(company_periods, company_reports, None)

        # 같은 보고서의 현금흐름을 이미 받아 뒀으면 그대로 쓰고, 보고서가 바뀐 종목만 새로 받는다.
        prev = previous.get(code) or {}
        prev_financials = prev.get("financials") or {}
        same_report = (
            prev_financials.get("기준") == financials["기준"]
            and prev_financials.get("재무제표") == financials["재무제표"]
        )
        if same_report and not prev.get("cash_flow_pending"):
            financials["영업활동현금흐름"] = prev_financials.get("영업활동현금흐름")
        else:
            financials["영업활동현금흐름"], record["cash_flow_pending"] = fetch_cash_flow(
                code, corp_code, year, reprt_code, dart_api.statement_division(rows)
            )
        record["financials"] = financials

    # 전체 공시를 다 저장하면 파일이 너무 커져서, 의견에 반영되는 주요 공시와 전체 건수만 남긴다.
    stock_disclosures = disclosures.get(code, [])
    record["disclosures"] = [
        {key: item[key] for key in ("보고서명", "접수일", "링크")}
        for item in investment_opinion.classify_disclosures(stock_disclosures)
    ]
    record["disclosure_count"] = len(stock_disclosures)

    return compact_numbers(record)


def write_output(stocks, now):
    # 종목당 한 줄로 써서, 매일 커밋해도 바뀐 종목만 diff에 잡히게 한다.
    lines = [
        f"  {json.dumps(code)}: {json.dumps(record, ensure_ascii=False)}"
        for code, record in sorted(stocks.items())
    ]
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("{\n")
        f.write(f' "updated": {json.dumps(now.strftime("%Y-%m-%d %H:%M"))},\n')
        f.write(f' "disclosure_days": {DISCLOSURE_DAYS},\n')
        f.write(' "stocks": {\n')
        f.write(",\n".join(lines))
        f.write("\n }\n}\n")


def main():
    now = datetime.now(KST)
    today = now.date()

    codes = load_codes()
    corp_codes = dart_api.corp_code_map()
    registered = [code for code in codes if code in corp_codes]
    print(f"전체 종목 수: {len(codes)} (DART 등록 {len(registered)})")

    previous = load_previous()
    periods, reports = collect_reports([corp_codes[code] for code in registered], today)
    disclosures = collect_disclosures(today)

    stocks = {}
    fetched_before = time.time()
    for i, code in enumerate(codes):
        if code not in corp_codes:
            # DART에 없는 종목(ETF 등)임을 앱이 알 수 있게 null로 남긴다.
            stocks[code] = None
            continue
        stocks[code] = build_stock(code, corp_codes[code], periods, reports, disclosures, previous)
        if (i + 1) % 200 == 0:
            print(f"[{i + 1}/{len(codes)}] 종목별 정리 진행 중 ({time.time() - fetched_before:.0f}초)")

    write_output(stocks, now)

    with_financials = sum(1 for s in stocks.values() if s and s["financials"])
    with_flagged = sum(1 for s in stocks.values() if s and s["disclosures"])
    pending = sum(1 for s in stocks.values() if s and s["cash_flow_pending"])
    print(
        f"완료: {len(stocks)}개 종목 (재무 {with_financials}, 주요 공시 있음 {with_flagged}, "
        f"현금흐름 재시도 대상 {pending}) -> {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    try:
        main()
    except dart_api.DARTAPIError as e:
        # 중간에 실패하면 파일을 건드리지 않고 끝내, 앱이 직전 수집분을 계속 쓰게 한다.
        print(f"DART 수집 실패: {e}")
        sys.exit(1)
