# investor_flow.py
# 투자자별 매매동향 표: 외국인·기관의 보유 물량과 추정 평단가
#
# 외국인 보유주수·보유율은 KIS가 주는 실제 값이다. 기관은 보유 물량이 공개되지 않아
# 최근 WINDOW거래일의 순매수 합계로 대신한다. 추정 평단가는 두 쪽 모두 같은 기간에
# 매수한 물량의 평균 단가다(보유 물량 전체의 매입가는 알 수 없다).

import pandas as pd

WINDOW = 60  # 순매수 합계·추정 평단가를 계산하는 기간(거래일)
ROWS = 60  # 표에 보여 주는 날짜 수(거래일)
PAGES = (WINDOW + ROWS) // 30  # kis_api.fetch_investor_daily는 한 번에 30거래일을 준다
INSTITUTION_NET = f"기관 {WINDOW}일 순매수"

# 투자자별 매수금액은 백만원 단위로 반올림돼 있어, 금액이 작은 날은 단가 오차가 커진다.
# 이보다 작으면 그 투자자의 매수 단가 대신 당일 평균가를 쓴다.
MIN_BUY_AMOUNT = 100_000_000

# 소진율은 소수 둘째 자리까지라, 지분율과 이만큼 차이 나는 것은 반올림으로 본다.
EXHAUSTION_TOLERANCE = 0.01


def estimate_average_price(daily, name):
    """날짜별로, 그날까지 WINDOW거래일 동안 매수한 물량의 평균 단가. 매수가 없으면 NaN."""

    buy = daily[f"{name}_매수"]
    amount = daily[f"{name}_매수금액"]
    cost = amount.where(amount >= MIN_BUY_AMOUNT, buy * daily["평균가"])
    bought = buy.rolling(WINDOW, min_periods=1).sum()
    return cost.rolling(WINDOW, min_periods=1).sum() / bought.where(bought > 0)


def foreign_holding_ratio(exhaustion, price_info):
    """일별 외국인 보유율(상장주식 대비 %). 한도가 있는 종목은 소진율을 지분율로 환산한다."""

    listed = price_info["상장주식수"]
    current = price_info["외국인소진율"]
    if not listed:
        return exhaustion * float("nan")

    ratio_now = price_info["외국인보유주수"] / listed * 100
    limited = current > 0 and abs(ratio_now - current) > EXHAUSTION_TOLERANCE
    ratio = exhaustion * (ratio_now / current) if limited else exhaustion.astype(float)
    if len(ratio) and exhaustion.iloc[-1] == current:
        ratio.iloc[-1] = ratio_now
    return ratio


def build_table(daily, exhaustion, price_info):
    """날짜별(최신순) 투자자 매매동향 표. 값은 모두 숫자이고, 없는 칸은 NaN이다."""

    listed = price_info["상장주식수"] or float("nan")
    foreign_ratio = foreign_holding_ratio(exhaustion, price_info).reindex(daily.index)
    institution_net = daily["기관_순매수"].rolling(WINDOW, min_periods=1).sum()

    table = pd.DataFrame({
        "종가": daily["종가"],
        "외국인 순매수": daily["외국인_순매수"],
        "외국인 보유주수": (foreign_ratio / 100 * listed).round(),
        "외국인 보유율": foreign_ratio,
        "외국인 추정평단": estimate_average_price(daily, "외국인"),
        "기관 순매수": daily["기관_순매수"],
        INSTITUTION_NET: institution_net,
        "기관 상장주식 대비": institution_net / listed * 100,
        "기관 추정평단": estimate_average_price(daily, "기관"),
        "개인 순매수": daily["개인_순매수"],
    })
    return table.tail(ROWS).sort_index(ascending=False)
