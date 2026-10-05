# investment_opinion.py
# AI 투자의견 (규칙 기반)
#
# 실적 성장 / 분기 추세 / 재무 건전성 / 밸류에이션 / 수급 / 컨센서스 / 공시 / 기술적·뉴스
# 8개 항목을 각각 -2~+2점으로 채점하고 합산해 매수 우위·중립·매도 우위를 정한다.
# 대시보드와 점수 기록 스크립트(scripts/record_scores.py)가 같이 쓰므로 Streamlit에
# 의존하지 않는 순수 함수만 둔다.
#
# 아래 기준값들은 경험적으로 정한 것이고 아직 수익률로 검증되지 않았다.
# 점수 기록이 쌓이면 score_validation.py의 결과를 보고 조정한다.

import pandas as pd

CATEGORY_LIMIT = 2

# 합산 점수가 만점의 이 비율 이상이면 매수 우위, 반대쪽이면 매도 우위.
# 평가에서 빠진 항목이 있으면 만점이 줄어드는 만큼 기준도 같이 낮아진다.
VERDICT_RATIO = 0.3
MIN_VERDICT_THRESHOLD = 2

MA_WINDOWS = [5, 7, 10, 15, 20]

# 실적 성장
REVENUE_GROWTH_PCT = 10
OPERATING_INCOME_GROWTH_PCT = 15

# 재무 건전성
OPERATING_MARGIN_GOOD_PCT = 10
DEBT_RATIO_LOW_PCT = 100
DEBT_RATIO_HIGH_PCT = 200
ROE_GOOD_PCT = 10

# 분기 추세
REVENUE_ACCELERATION_PCTP = 10
MARGIN_TREND_PCTP = 3

# 밸류에이션: 같은 업종의 중앙값과 비교한다. 업종 통계가 없으면 아래 절대 기준을 쓴다.
SECTOR_DISCOUNT_RATIO = 0.7
SECTOR_PREMIUM_RATIO = 1.5
SECTOR_MIN_COUNT = 10
PER_LOW = 10
PER_HIGH = 30
PBR_LOW = 1
PBR_HIGH = 5

# 수급: 최근 20거래일 누적 순매수가 시가총액의 이 비율(%)을 넘으면 뚜렷한 매수·매도로 본다.
FLOW_DAYS = 20
FLOW_RATIO_PCT = 0.5

# 컨센서스
CONSENSUS_DAYS = 90
TARGET_UPSIDE_PCT = 20
BUY_OPINIONS = ("buy", "매수", "outperform", "overweight", "비중확대")

# 공시 보고서명 키워드 규칙: (키워드, 점수, 표시 이름). 위에서부터 먼저 걸리는 규칙 하나만 적용한다.
# 점수 0은 "다른 규칙의 키워드를 포함하지만 의미가 달라 무시"하려는 용도다.
DISCLOSURE_RULES = [
    ("상장폐지", -2, "상장폐지 관련 공시"),
    ("관리종목", -2, "관리종목 관련 공시"),
    ("회생절차", -2, "회생절차 관련 공시"),
    ("횡령", -2, "횡령·배임 관련 공시"),
    ("배임", -2, "횡령·배임 관련 공시"),
    ("불성실공시", -2, "불성실공시 관련 공시"),
    ("감자결정", -1, "감자 결정"),
    ("유상증자결정", -1, "유상증자 결정(지분 희석 가능성)"),
    ("유무상증자결정", -1, "유무상증자 결정(지분 희석 가능성)"),
    ("전환사채권발행결정", -1, "전환사채 발행 결정(잠재적 지분 희석)"),
    ("신주인수권부사채권발행결정", -1, "신주인수권부사채 발행 결정(잠재적 지분 희석)"),
    ("교환사채권발행결정", -1, "교환사채 발행 결정"),
    ("공급계약해지", -1, "공급계약 해지"),
    ("소송등의제기", -1, "소송 제기"),
    ("자기주식취득신탁계약해지", 0, ""),
    ("자기주식취득", 1, "자기주식 취득(주주환원)"),
    ("주식소각결정", 1, "주식 소각 결정(주주환원)"),
    ("무상증자결정", 1, "무상증자 결정"),
    ("공급계약체결", 1, "공급계약 체결"),
    ("배당결정", 1, "배당 결정(주주환원)"),
]


def format_amount(value):
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_0000_0000_0000:
        return f"{sign}{value / 1_0000_0000_0000:,.2f}조"
    if value >= 1_0000_0000:
        return f"{sign}{value / 1_0000_0000:,.0f}억"
    if value >= 1_0000:
        return f"{sign}{value / 1_0000:,.0f}만"
    return f"{sign}{value:,.0f}"


def format_score(score):
    return f"{score:+d}" if score else "0"


def add_indicators(df):
    """의견·차트에 쓰는 이동평균과 Envelope(20일선 ±20%)를 붙인다."""

    for window in MA_WINDOWS:
        df[f"MA{window}"] = df["Close"].rolling(window).mean()
    df["ENV_UPPER"] = df["MA20"] * 1.2
    df["ENV_LOWER"] = df["MA20"] * 0.8
    return df


def _trailing_net_income(financials):
    """최근 4개 분기 순이익. 직전 연간 + 올해 누적 - 작년 같은 기간 누적으로 구한다."""

    if financials["기준"].endswith("연간"):
        return financials["당기순이익"]

    trend = financials.get("연간추이") or []
    current, previous = financials["당기순이익"], financials["당기순이익_전년동기"]
    if not trend or current is None or previous is None:
        return None
    last_year = trend[-1]
    if last_year["연도"] != str(int(financials["기준"][:4]) - 1) or last_year["당기순이익"] is None:
        return None
    return last_year["당기순이익"] + current - previous


def valuation_metrics(price_info, financials):
    """PER·PBR·ROE. KIS 값이 있으면 그대로 쓰고, 없는 종목은 DART 재무제표와 시가총액으로 계산한다.

    KIS는 일부 종목(최근 상장·합병 등)의 PER·PBR·EPS·BPS를 모두 0으로 준다.
    반환: {"PER", "PBR", "ROE"(없으면 None), "적자": bool, "출처": "KIS" | "DART" | None}
    """

    per, pbr = price_info.get("PER"), price_info.get("PBR")
    eps, bps = price_info.get("EPS"), price_info.get("BPS")
    if pbr or bps:
        return {
            "PER": per if per and per > 0 else None,
            "PBR": pbr if pbr and pbr > 0 else None,
            "ROE": eps / bps * 100 if eps and bps and bps > 0 else None,
            "적자": bool(eps and eps < 0),
            "출처": "KIS",
        }

    market_cap = (price_info.get("시가총액") or 0) * 1_0000_0000  # KIS 시가총액은 억원 단위
    if not financials or not market_cap:
        return {"PER": None, "PBR": None, "ROE": None, "적자": False, "출처": None}

    equity = financials["자본총계"]
    net_income = _trailing_net_income(financials)
    has_equity = equity is not None and equity > 0
    return {
        "PER": market_cap / net_income if net_income and net_income > 0 else None,
        "PBR": market_cap / equity if has_equity else None,
        "ROE": net_income / equity * 100 if net_income is not None and has_equity else None,
        "적자": net_income is not None and net_income < 0,
        "출처": "DART",
    }


def _growth_pct(current, previous):
    """전년 대비 증감률(%). 전년 값이 0 이하면 증감률이 의미가 없어 None."""

    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous * 100


def _category(name, score, reasons, available=True):
    return {
        "name": name,
        "score": max(-CATEGORY_LIMIT, min(CATEGORY_LIMIT, score)),
        "reasons": reasons,
        "available": available,
    }


def _unavailable(name, reason):
    return _category(name, 0, [reason], available=False)


def evaluate_growth(financials):
    name = "실적 성장"
    if not financials:
        return _unavailable(name, "재무제표 데이터를 가져오지 못해 평가하지 않았습니다.")

    score = 0
    reasons = []
    basis = financials["기준"]

    revenue_growth = _growth_pct(financials["매출액"], financials["매출액_전년동기"])
    if revenue_growth is not None:
        text = (
            f"{basis} 매출액 {format_amount(financials['매출액'])}"
            f"(전년 동기 대비 {revenue_growth:+.1f}%)"
        )
        if revenue_growth >= REVENUE_GROWTH_PCT:
            score += 1
            reasons.append(f"{text}로 외형 성장이 뚜렷합니다.")
        elif revenue_growth <= -REVENUE_GROWTH_PCT:
            score -= 1
            reasons.append(f"{text}로 외형이 줄었습니다.")
        else:
            reasons.append(f"{text}로 전년과 비슷한 수준입니다.")

    current, previous = financials["영업이익"], financials["영업이익_전년동기"]
    if current is not None and previous is not None:
        text = f"{basis} 영업이익 {format_amount(current)}: 전년 동기 {format_amount(previous)}에서"
        if previous <= 0 < current:
            score += 1
            reasons.append(f"{text} 흑자 전환했습니다.")
        elif current <= 0 < previous:
            score -= 1
            reasons.append(f"{text} 적자 전환했습니다.")
        elif current <= 0:
            trend = "축소" if current > previous else "확대"
            reasons.append(f"{text} 적자가 이어지며 적자 폭이 {trend}됐습니다.")
        else:
            growth = _growth_pct(current, previous)
            text = f"{basis} 영업이익 {format_amount(current)}(전년 동기 대비 {growth:+.1f}%)"
            if growth >= OPERATING_INCOME_GROWTH_PCT:
                score += 1
                reasons.append(f"{text}로 이익이 크게 늘었습니다.")
            elif growth <= -OPERATING_INCOME_GROWTH_PCT:
                score -= 1
                reasons.append(f"{text}로 이익이 크게 줄었습니다.")
            else:
                reasons.append(f"{text}로 전년과 비슷한 수준입니다.")

    if not reasons:
        return _unavailable(name, "매출액·영업이익의 전년 동기 수치가 없어 평가하지 않았습니다.")
    return _category(name, score, reasons)


def evaluate_trend(financials):
    """누적 실적(실적 성장)과 달리, 최근 분기들의 방향이 좋아지는지 나빠지는지를 본다."""

    name = "분기 추세"
    quarters = (financials or {}).get("분기추이") or []
    if len(quarters) < 2:
        return _unavailable(name, "분기별 실적을 2개 분기 이상 구하지 못해 평가하지 않았습니다.")

    score = 0
    reasons = []
    latest, before = quarters[-1], quarters[-2]

    # 매출액 계정이 없는 금융업은 영업이익으로 본다.
    account = "매출액" if latest["매출액"] is not None else "영업이익"
    growth = _growth_pct(latest[account], latest[f"{account}_전년"])
    growth_before = _growth_pct(before[account], before[f"{account}_전년"])
    if growth is not None and growth_before is not None:
        text = (
            f"{account}의 전년 동기 대비 증가율이 {before['분기']} {growth_before:+.1f}%에서 "
            f"{latest['분기']} {growth:+.1f}%로"
        )
        if growth - growth_before >= REVENUE_ACCELERATION_PCTP:
            score += 1
            reasons.append(f"{text} 높아져 성장이 빨라지고 있습니다.")
        elif growth - growth_before <= -REVENUE_ACCELERATION_PCTP:
            score -= 1
            reasons.append(f"{text} 낮아져 성장이 둔화되고 있습니다.")
        else:
            reasons.append(f"{text} 비슷한 흐름입니다.")

    margins = [
        q["영업이익"] / q["매출액"] * 100
        for q in quarters
        if q["매출액"] and q["영업이익"] is not None
    ]
    if len(margins) == len(quarters) and len(margins) >= 3:
        earlier = sum(margins[:-1]) / len(margins[:-1])
        text = f"{latest['분기']} 영업이익률 {margins[-1]:.1f}%(앞선 {len(margins) - 1}개 분기 평균 {earlier:.1f}%)로"
        if margins[-1] - earlier >= MARGIN_TREND_PCTP:
            score += 1
            reasons.append(f"{text} 수익성이 좋아지고 있습니다.")
        elif margins[-1] - earlier <= -MARGIN_TREND_PCTP:
            score -= 1
            reasons.append(f"{text} 수익성이 나빠지고 있습니다.")
        else:
            reasons.append(f"{text} 수익성은 비슷한 수준입니다.")

    if not reasons:
        return _unavailable(name, "분기별 증가율·이익률을 계산할 수 없어 평가하지 않았습니다.")
    return _category(name, score, reasons)


def evaluate_health(financials, metrics):
    name = "재무 건전성"
    score = 0
    reasons = []

    if financials:
        revenue, operating_income = financials["매출액"], financials["영업이익"]
        if revenue and operating_income is not None:
            margin = operating_income / revenue * 100
            if margin >= OPERATING_MARGIN_GOOD_PCT:
                score += 1
                reasons.append(f"영업이익률 {margin:.1f}%로 수익성이 양호합니다.")
            elif margin < 0:
                score -= 1
                reasons.append(f"영업이익률 {margin:.1f}%로 본업에서 손실이 나고 있습니다.")
            else:
                reasons.append(f"영업이익률은 {margin:.1f}%입니다.")

        debt, equity = financials["부채총계"], financials["자본총계"]
        if debt is not None and equity is not None:
            if equity <= 0:
                score -= 2
                reasons.append("자본총계가 0 이하인 자본잠식 상태입니다.")
            elif not financials["유동성구분"]:
                reasons.append("금융업 재무제표로 보여 부채비율은 평가에서 제외했습니다.")
            else:
                debt_ratio = debt / equity * 100
                if debt_ratio <= DEBT_RATIO_LOW_PCT:
                    score += 1
                    reasons.append(f"부채비율 {debt_ratio:.0f}%로 재무구조가 안정적입니다.")
                elif debt_ratio >= DEBT_RATIO_HIGH_PCT:
                    score -= 1
                    reasons.append(f"부채비율 {debt_ratio:.0f}%로 재무 부담이 큽니다.")
                else:
                    reasons.append(f"부채비율은 {debt_ratio:.0f}%입니다.")

        cash_flow, net_income = financials["영업활동현금흐름"], financials["당기순이익"]
        if cash_flow is not None and net_income is not None and cash_flow < 0 < net_income:
            score -= 1
            reasons.append(
                f"순이익은 흑자({format_amount(net_income)})인데 영업활동현금흐름은 "
                f"마이너스({format_amount(cash_flow)})여서 이익의 현금 뒷받침이 약합니다."
            )

    roe = metrics["ROE"]
    if roe is not None:
        basis = "EPS÷BPS" if metrics["출처"] == "KIS" else "최근 4개 분기 순이익÷자본"
        if roe >= ROE_GOOD_PCT:
            score += 1
            reasons.append(f"ROE {roe:.1f}%({basis})로 자본 효율이 높습니다.")
        elif roe < 0:
            score -= 1
            reasons.append(f"ROE {roe:.1f}%({basis})로 자기자본이 줄어드는 구조입니다.")
        else:
            reasons.append(f"ROE는 {roe:.1f}%({basis})입니다.")

    if not reasons:
        return _unavailable(name, "재무 데이터를 가져오지 못해 평가하지 않았습니다.")
    return _category(name, score, reasons)


def _sector_median(sector_stats, sector, key):
    """업종 중앙값. 통계가 없거나 표본이 적으면 None."""

    stats = (sector_stats or {}).get(sector)
    if not stats or not stats.get(key) or stats.get(f"{key}_count", 0) < SECTOR_MIN_COUNT:
        return None
    return stats[key]


def _evaluate_multiple(label, value, digits, median, sector, low, high, cheap_text, expensive_text):
    """PER·PBR 한 지표의 (점수, 근거). 업종 중앙값이 있으면 그것과, 없으면 절대 기준과 비교한다."""

    text = f"{label} {value:.{digits}f}배"
    if median:
        ratio = value / median
        compare = f"{text}로 {sector} 업종 중앙값 {median:.{digits}f}배"
        if ratio <= SECTOR_DISCOUNT_RATIO:
            return 1, f"{compare}보다 {(1 - ratio) * 100:.0f}% 낮아 {cheap_text}"
        if ratio >= 2:
            return -1, f"{compare}의 {ratio:.1f}배 수준이어서 {expensive_text}"
        if ratio >= SECTOR_PREMIUM_RATIO:
            return -1, f"{compare}보다 {(ratio - 1) * 100:.0f}% 높아 {expensive_text}"
        return 0, f"{compare}와 비슷한 수준입니다."

    if value <= low:
        return 1, f"{text}로 {cheap_text}"
    if value >= high:
        return -1, f"{text}로 {expensive_text}"
    return 0, f"{label}은 {value:.{digits}f}배입니다."


def evaluate_valuation(metrics, sector, sector_stats):
    name = "밸류에이션"
    score = 0
    reasons = []

    if metrics["PER"] is not None:
        points, reason = _evaluate_multiple(
            "PER", metrics["PER"], 1, _sector_median(sector_stats, sector, "per"), sector,
            PER_LOW, PER_HIGH, "이익 대비 주가가 낮은 편입니다.", "이익 대비 주가 부담이 큽니다.",
        )
        score += points
        reasons.append(reason)
    elif metrics["적자"]:
        reasons.append("순손실 상태라 PER을 산정할 수 없습니다.")

    if metrics["PBR"] is not None:
        points, reason = _evaluate_multiple(
            "PBR", metrics["PBR"], 2, _sector_median(sector_stats, sector, "pbr"), sector,
            PBR_LOW, PBR_HIGH, "순자산 대비 주가가 낮은 편입니다.", "순자산 대비 주가 부담이 큽니다.",
        )
        score += points
        reasons.append(reason)

    if not reasons:
        return _unavailable(name, "PER·PBR 데이터가 없어 평가하지 않았습니다.")
    if metrics["출처"] == "DART":
        reasons.append("KIS에 수치가 없는 종목이라 DART 재무제표와 시가총액으로 계산했습니다.")
    return _category(name, score, reasons)


def evaluate_flow(investor, price_info):
    """investor: kis_api.fetch_investor_trend()의 일별 순매수(원) DataFrame."""

    name = "수급"
    market_cap = (price_info.get("시가총액") or 0) * 1_0000_0000
    if investor is None or len(investor) < 5 or not market_cap:
        return _unavailable(name, "투자자별 매매 데이터를 가져오지 못해 평가하지 않았습니다.")

    recent = investor.tail(FLOW_DAYS)
    score = 0
    reasons = []
    for who in ("외국인", "기관"):
        net = recent[who].sum()
        ratio = net / market_cap * 100
        side = "순매수" if net >= 0 else "순매도"
        text = f"{who}이 최근 {len(recent)}거래일간 {format_amount(abs(net))} {side}(시가총액의 {abs(ratio):.2f}%)"
        if ratio >= FLOW_RATIO_PCT:
            score += 1
            reasons.append(f"{text}해 매수세가 뚜렷합니다.")
        elif ratio <= -FLOW_RATIO_PCT:
            score -= 1
            reasons.append(f"{text}해 매도세가 뚜렷합니다.")
        else:
            reasons.append(f"{text}로 뚜렷한 방향이 없습니다.")
    return _category(name, score, reasons)


def latest_opinions(opinions, today, days=CONSENSUS_DAYS):
    """최근 `days`일 안에 나온 리포트 중 증권사별 가장 최근 것(최신순)."""

    since = (pd.Timestamp(today) - pd.Timedelta(days=days)).strftime("%Y%m%d")
    latest = {}
    for opinion in opinions:  # 최신순
        if opinion["날짜"] >= since:
            latest.setdefault(opinion["증권사"], opinion)
    return list(latest.values())


def evaluate_consensus(opinions, price, today):
    """opinions: kis_api.fetch_invest_opinions()의 리포트 목록(최신순, 최근 6개월치)."""

    name = "컨센서스"
    if opinions is None:
        return _unavailable(name, "증권사 투자의견을 가져오지 못해 평가하지 않았습니다.")

    recent = latest_opinions(opinions, today)
    targets = [o["목표가"] for o in recent if o["목표가"] > 0]
    if not targets or not price:
        return _unavailable(name, f"최근 {CONSENSUS_DAYS}일간 목표주가를 제시한 증권사 리포트가 없어 평가하지 않았습니다.")

    score = 0
    reasons = []

    average = sum(targets) / len(targets)
    upside = (average / price - 1) * 100
    text = f"증권사 {len(targets)}곳의 평균 목표주가 {average:,.0f}원(현재가 대비 {upside:+.1f}%)"
    if upside >= TARGET_UPSIDE_PCT:
        score += 1
        reasons.append(f"{text}로 상승 여력이 큽니다.")
    elif upside <= 0:
        score -= 1
        reasons.append(f"{text}로 주가가 이미 목표주가를 넘어섰습니다.")
    else:
        reasons.append(f"{text}로 상승 여력이 크지 않습니다.")

    # 같은 증권사의 직전 리포트와 비교한 목표주가 변경 방향
    raised = lowered = 0
    for opinion in recent:
        previous = next(
            (o for o in opinions
             if o["증권사"] == opinion["증권사"] and o["날짜"] < opinion["날짜"] and o["목표가"] > 0),
            None,
        )
        if previous and opinion["목표가"] > 0:
            raised += opinion["목표가"] > previous["목표가"]
            lowered += opinion["목표가"] < previous["목표가"]
    if raised or lowered:
        text = f"목표주가를 올린 증권사 {raised}곳, 내린 증권사 {lowered}곳"
        if raised > lowered:
            score += 1
            reasons.append(f"{text}으로 눈높이가 높아지고 있습니다.")
        elif lowered > raised:
            score -= 1
            reasons.append(f"{text}으로 눈높이가 낮아지고 있습니다.")
        else:
            reasons.append(f"{text}으로 엇갈립니다.")

    buys = sum(any(word in o["의견"].lower() for word in BUY_OPINIONS) for o in recent)
    reasons.append(f"최근 {CONSENSUS_DAYS}일 리포트를 낸 {len(recent)}곳 중 {buys}곳이 매수 의견입니다(참고용, 점수 미반영).")

    return _category(name, score, reasons)


def classify_disclosures(disclosures):
    """규칙에 걸리는 공시만 추려 점수·표시 이름을 붙인다(입력 순서 유지)."""

    flagged = []
    for item in disclosures:
        title = item["보고서명"].replace(" ", "")
        for keyword, points, label in DISCLOSURE_RULES:
            if keyword in title:
                if points != 0:
                    flagged.append({**item, "점수": points, "분류": label})
                break
    return flagged


def evaluate_disclosures(disclosures, days, total_count=None):
    """total_count: 주요 공시만 추려 넘길 때(수집 파일)의 원래 전체 공시 건수."""

    name = "공시"
    if disclosures is None:
        return _unavailable(name, "공시 목록을 가져오지 못해 평가하지 않았습니다.")
    if total_count is None:
        total_count = len(disclosures)

    # 정정 공시 등으로 같은 종류가 여러 건 올라와도 한 번만 반영한다.
    by_label = {}
    for item in classify_disclosures(disclosures):
        entry = by_label.setdefault(item["분류"], {"점수": item["점수"], "건수": 0, "접수일": item["접수일"]})
        entry["건수"] += 1

    if not by_label:
        return _category(
            name, 0, [f"최근 {days}일간 주가에 영향을 줄 만한 주요 공시가 없습니다(전체 {total_count}건)."]
        )

    score = 0
    reasons = []
    for label, entry in by_label.items():
        score += entry["점수"]
        date_text = pd.to_datetime(entry["접수일"], format="%Y%m%d", errors="coerce")
        date_text = f"{date_text:%m/%d}" if pd.notna(date_text) else entry["접수일"]
        count_text = f", {entry['건수']}건" if entry["건수"] > 1 else ""
        effect = "긍정" if entry["점수"] > 0 else "부정"
        reasons.append(f"{label} (최근 {date_text}{count_text}) — {effect} 요인")
    return _category(name, score, reasons)


def evaluate_technical(df, news_score, news_label):
    name = "기술적·뉴스"
    score = 0
    reasons = []

    latest = df.iloc[-1]
    ma5, ma10, ma20 = latest.get("MA5"), latest.get("MA10"), latest.get("MA20")
    close = latest["Close"]

    if pd.notna(ma5) and pd.notna(ma10) and pd.notna(ma20):
        if ma5 > ma10 > ma20:
            score += 1
            reasons.append(
                f"이동평균이 정배열(MA5 {ma5:,.0f} > MA10 {ma10:,.0f} > MA20 {ma20:,.0f})을 "
                "보이고 있어 단기 상승 추세로 해석됩니다."
            )
        elif ma5 < ma10 < ma20:
            score -= 1
            reasons.append(
                f"이동평균이 역배열(MA5 {ma5:,.0f} < MA10 {ma10:,.0f} < MA20 {ma20:,.0f})을 "
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

    if not reasons:
        reasons = ["뚜렷한 기술적 매수/매도 신호가 관측되지 않았습니다."]
    return _category(name, score, reasons)


def generate_opinion(
    df, price_info, *, news_score=0, news_label="", financials=None, disclosures=None,
    disclosure_days=90, disclosure_count=None, investor=None, opinions=None,
    sector_stats=None, today=None,
):
    """항목별 평가와 종합 의견을 만든다.

    df는 add_indicators()를 거친 차트 데이터. 나머지 입력이 None이면(조회 실패, 데이터 없음 등)
    해당 항목은 '평가 제외'로 두고 나머지 항목만으로 판단한다.
    """

    today = today or pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
    metrics = valuation_metrics(price_info, financials)

    categories = [
        evaluate_growth(financials),
        evaluate_trend(financials),
        evaluate_health(financials, metrics),
        evaluate_valuation(metrics, price_info.get("업종"), sector_stats),
        evaluate_flow(investor, price_info),
        evaluate_consensus(opinions, price_info.get("현재가"), today),
        evaluate_disclosures(disclosures, disclosure_days, disclosure_count),
        evaluate_technical(df, news_score, news_label),
    ]

    evaluated = [c for c in categories if c["available"]]
    total = sum(c["score"] for c in evaluated)
    max_total = CATEGORY_LIMIT * len(evaluated)
    threshold = max(MIN_VERDICT_THRESHOLD, round(VERDICT_RATIO * max_total))

    positives = ", ".join(c["name"] for c in evaluated if c["score"] > 0)
    negatives = ", ".join(c["name"] for c in evaluated if c["score"] < 0)
    if positives and negatives:
        summary = f"{positives} 항목이 긍정적이고 {negatives} 항목이 부정적입니다."
    elif positives:
        summary = f"{positives} 항목이 긍정적이고 부정적인 항목은 없습니다."
    elif negatives:
        summary = f"{negatives} 항목이 부정적이고 긍정적인 항목은 없습니다."
    else:
        summary = "모든 항목이 중립입니다."

    if total >= threshold:
        verdict, icon, css, conclusion = "매수 우위", "🔥", "opinion-buy", "'매수 우위'로"
    elif total <= -threshold:
        verdict, icon, css, conclusion = "매도 우위", "⚠️", "opinion-sell", "'매도 우위'로"
    else:
        verdict, icon, css, conclusion = "중립", "➖", "opinion-neutral", "'중립'으로"

    return {
        "label": f"{icon} {verdict}",
        "verdict_name": verdict,
        "css": css,
        "total": total,
        "max_total": max_total,
        "threshold": threshold,
        "categories": categories,
        "metrics": metrics,
        "verdict": (
            f"{summary} 종합 {format_score(total)}점(기준 ±{threshold}점)으로 {conclusion} 판단됩니다."
        ),
    }
