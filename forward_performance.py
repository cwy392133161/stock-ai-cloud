"""Forward returns on a frozen equal-weight cohort, using verified daily bars."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite


HORIZONS = (5, 20)
BUY_COST = .001
SELL_COST = .0015
METHODS = {
    "published": "当日发布榜单", "legacy": "原规则", "revised": "修订估值",
    "industry": "行业内排名", "sensitivity": "活跃度加权", "benchmark": "当日候选池等权",
}
CST = timezone(timedelta(hours=8))


def target_turnover(codes, previous):
    """One-way turnover of equal-weight targets, excluding market drift."""
    if not codes or not previous:
        return None
    common = len(set(codes).intersection(previous))
    return round(100 * (1 - common / max(len(codes), len(previous))), 6)


def _number(value):
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if isfinite(value) else None


@dataclass(frozen=True)
class Quote:
    opening: float | None
    close: float | None
    previous: float | None
    buy: str
    sell: str


def read_bars(table, day):
    """Keep only decision/mark data in the rolling evaluation cache."""
    bars = {}
    index = {field: i for i, field in enumerate(table["columns"])}
    for row in table["rows"]:
        def get(field):
            return row[index[field]] if field in index else None

        code = get("代码")
        if get("报价当日") is False:
            continue
        if "报价时间" in index:
            stamp = str(get("报价时间"))
            if (len(stamp) != 14 or not stamp.isascii() or not stamp.isdigit() or
                    stamp[:8] != day.replace("-", "") or not "150000" <= stamp[8:] <= "235959"):
                continue
        elif "f124" in index:
            stamp = _number(get("f124"))
            try:
                seen = datetime.fromtimestamp(stamp, CST) if stamp else None
            except (ValueError, OverflowError, OSError):
                seen = None
            if not seen or seen.date().isoformat() != day or seen.hour < 15:
                continue
        vol, cash = (_number(get(f)) for f in ("成交量", "成交额"))
        volumes = [v for v in (vol, cash) if v is not None]
        if not volumes or min(volumes) < 0:
            continue
        if all(v == 0 for v in volumes):
            bars[code] = Quote(None, None, None, "suspended", "suspended")
            continue
        if min(volumes) == 0:
            continue
        op, close, prev, hi, lo, up, down = (
            _number(get(f)) for f in ("今开", "最新价", "昨收", "最高", "最低", "涨停价", "跌停价"))
        if not close or close <= 0 or (hi and hi < close) or (lo and lo > close):
            continue
        if op is None or op <= 0 or (hi and op > hi) or (lo and op < lo):
            buy = "unknown"
        elif up and up > 0:
            buy = "open" if op < up else ("limit" if op == up else "unknown")
        else:
            # A later higher trade proves the opening print was below the daily ceiling.
            buy = "open" if hi and hi > op else "unknown"
        if down and down > 0:
            sell = "open" if close > down else ("limit" if close == down else "unknown")
        else:
            sell = "open" if lo and 0 < lo < close else "unknown"
        bars[code] = Quote(op, close, prev, buy, sell)
    return bars


def _position(code, market_days):
    gross = 1.
    path = []
    bought = None
    deferred = 0
    for i, bars in enumerate(market_days):
        q = bars.get(code)
        if q is None:
            return None
        if bought is None:
            if q.buy == "unknown":
                return None
            if q.buy == "open":
                gross = q.close / q.opening
                bought = i
            else:
                deferred += 1
        elif q.sell != "suspended":
            if not q.previous or q.previous <= 0:
                return None
            gross *= q.close / q.previous
        net = gross / (1 + BUY_COST) if bought is not None else 1.
        path.append(net)
    if bought is None:
        exit_state = "cash"
    elif bought == len(market_days) - 1:
        exit_state = "t_plus_one"
    else:
        exit_state = q.sell
    return path, gross, exit_state, deferred


def _pct(value):
    return round(value * 100, 6)


def _portfolio(codes, positions, horizon):
    total = len(codes)
    nav_sum = [0.] * horizon
    gross_sum = 0.
    exit_fees = 0.
    statuses = {k: 0 for k in ("open", "cash", "t_plus_one", "suspended", "limit", "unknown")}
    unavailable = []
    deferred = 0
    for code in codes:
        position = positions[code]
        if position is None:
            unavailable.append(code)
            continue
        path, gross, exit_state, delay = position
        statuses[exit_state] += 1
        deferred += delay > 0
        for i, nav in enumerate(path):
            nav_sum[i] += nav
        gross_sum += gross
        if exit_state == "open":
            exit_fees += path[-1] * SELL_COST
    result = {"target_count": total, "known_count": total - len(unavailable),
              "unavailable_codes": unavailable, "delayed_entries": deferred,
              "exit_states": statuses, "marked_return_pct": None, "gross_return_pct": None,
              "estimated_liquidation_return_pct": None, "max_drawdown_pct": None,
              "entry_cost_pct_of_initial_cash": None, "round_trip_cost_pct_of_initial_cash": None}
    result["known_contribution_pct_points"] = (
        _pct((nav_sum[-1] - result["known_count"]) / total) if total and result["known_count"] else None)
    if unavailable or not total:
        return result
    nav = [v / total for v in nav_sum]
    peak = 1.
    max_dd = 0.
    for value in nav:
        peak = max(peak, value)
        max_dd = max(max_dd, 1 - value / peak)
    entry_fee = (1 - statuses["cash"] / total) * BUY_COST / (1 + BUY_COST)
    result.update(marked_return_pct=_pct(nav[-1] - 1), gross_return_pct=_pct(gross_sum / total - 1),
                  entry_cost_pct_of_initial_cash=_pct(entry_fee), max_drawdown_pct=_pct(max_dd))
    if statuses["open"] + statuses["cash"] == total:
        result.update(estimated_liquidation_return_pct=_pct(nav[-1] - exit_fees / total - 1),
                      round_trip_cost_pct_of_initial_cash=_pct(entry_fee + exit_fees / total))
    return result


def evaluate_cohort(groups, market_days):
    horizon = len(market_days)
    if not horizon or "benchmark" not in groups:
        raise ValueError("至少需要一个交易日和当期候选池基准")
    positions = {code: _position(code, market_days) for code in set().union(*groups.values())}
    results = {name: _portfolio(codes, positions, horizon) for name, codes in groups.items()}
    for result in results.values():
        for field, excess in (("marked_return_pct", "excess_pct_points"),
                              ("estimated_liquidation_return_pct", "liquidation_excess_pct_points")):
            value, benchmark = result[field], results["benchmark"][field]
            result[excess] = round(value - benchmark, 6) if None not in (value, benchmark) else None
    return results
