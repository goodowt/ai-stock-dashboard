# investment_opinion.py
# AI 투자의견 (규칙 기반)
#
# 실적 성장 / 재무 건전성 / 밸류에이션 / 공시 / 기술적·뉴스 5개 항목을 각각 -2~+2점으로
# 채점하고 합산해 매수 우위·중립·매도 우위를 정한다. Streamlit에 의존하지 않는 순수 함수만 둔다.

import pandas as pd

CATEGORY_LIMIT = 2

# 합산 점수가 이 값 이상이면 매수 우위, -이 값 이하면 매도 우위
VERDICT_THRESHOLD = 3

# 실적 성장
REVENUE_GROWTH_PCT = 10
OPERATING_INCOME_GROWTH_PCT = 15

# 재무 건전성
OPERATING_MARGIN_GOOD_PCT = 10
DEBT_RATIO_LOW_PCT = 100
DEBT_RATIO_HIGH_PCT = 200
ROE_GOOD_PCT = 10

# 밸류에이션 (업종 평균이 아닌 절대 수준 기준)
PER_LOW = 10
PER_HIGH = 30
PBR_LOW = 1
PBR_HIGH = 5

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


def evaluate_health(financials, price_info):
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

    eps, bps = price_info.get("EPS"), price_info.get("BPS")
    if eps and bps and bps > 0:
        roe = eps / bps * 100
        if roe >= ROE_GOOD_PCT:
            score += 1
            reasons.append(f"ROE {roe:.1f}%(EPS÷BPS)로 자본 효율이 높습니다.")
        elif roe < 0:
            score -= 1
            reasons.append(f"ROE {roe:.1f}%(EPS÷BPS)로 자기자본이 줄어드는 구조입니다.")
        else:
            reasons.append(f"ROE는 {roe:.1f}%(EPS÷BPS)입니다.")

    if not reasons:
        return _unavailable(name, "재무 데이터를 가져오지 못해 평가하지 않았습니다.")
    return _category(name, score, reasons)


def evaluate_valuation(price_info):
    name = "밸류에이션"
    score = 0
    reasons = []

    per, pbr, eps = price_info.get("PER"), price_info.get("PBR"), price_info.get("EPS")

    if per and per > 0:
        if per <= PER_LOW:
            score += 1
            reasons.append(f"PER {per:.1f}배로 이익 대비 주가가 낮은 편입니다.")
        elif per >= PER_HIGH:
            score -= 1
            reasons.append(f"PER {per:.1f}배로 이익 대비 주가 부담이 큽니다.")
        else:
            reasons.append(f"PER은 {per:.1f}배입니다.")
    elif eps and eps < 0:
        reasons.append("순손실 상태라 PER을 산정할 수 없습니다.")

    if pbr and pbr > 0:
        if pbr < PBR_LOW:
            score += 1
            reasons.append(f"PBR {pbr:.2f}배로 순자산 가치보다 낮게 거래되고 있습니다.")
        elif pbr >= PBR_HIGH:
            score -= 1
            reasons.append(f"PBR {pbr:.2f}배로 순자산 대비 주가 부담이 큽니다.")
        else:
            reasons.append(f"PBR은 {pbr:.2f}배입니다.")

    if not reasons:
        return _unavailable(name, "PER·PBR 데이터가 없어 평가하지 않았습니다.")
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
    df, news_score, news_label, price_info, financials, disclosures, disclosure_days, disclosure_count=None
):
    """항목별 평가와 종합 의견을 만든다.

    financials / disclosures가 None이면(DART 키 없음, 조회 실패 등) 해당 항목은
    '평가 제외'로 두고 나머지 항목만으로 판단한다.
    """

    categories = [
        evaluate_growth(financials),
        evaluate_health(financials, price_info),
        evaluate_valuation(price_info),
        evaluate_disclosures(disclosures, disclosure_days, disclosure_count),
        evaluate_technical(df, news_score, news_label),
    ]

    evaluated = [c for c in categories if c["available"]]
    total = sum(c["score"] for c in evaluated)
    max_total = CATEGORY_LIMIT * len(evaluated)

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

    if total >= VERDICT_THRESHOLD:
        label, css, conclusion = "🔥 매수 우위", "opinion-buy", "'매수 우위'로"
    elif total <= -VERDICT_THRESHOLD:
        label, css, conclusion = "⚠️ 매도 우위", "opinion-sell", "'매도 우위'로"
    else:
        label, css, conclusion = "➖ 중립", "opinion-neutral", "'중립'으로"

    return {
        "label": label,
        "css": css,
        "total": total,
        "max_total": max_total,
        "categories": categories,
        "verdict": f"{summary} 종합 {format_score(total)}점으로 {conclusion} 판단됩니다.",
    }
