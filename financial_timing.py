"""Row-local provider publication/revision evidence, never a filing guarantee."""
from datetime import date, datetime
import re


DATE_FIELDS = ("NOTICE_DATE", "UPDATE_DATE")
CASHFLOW_SOURCE = "东方财富现金流量表（AKShare stock_cash_flow_sheet_by_report_em）"


def provider_date(value):
    """Parse actual calendar dates; integers YYYYMMDD are not Unix timestamps."""
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"[0-9]{8}", text):
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
            return date.fromisoformat(text).isoformat()
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?", text):
            return datetime.fromisoformat(text).date().isoformat()
    except (ValueError, OverflowError):
        pass
    return None


def timing_evidence(fields, period, as_of):
    """Validate every supplied provider date and retain its original field name.

    An absent/null date is not evidence. A populated but invalid date, a date
    before period end, or any revision after the snapshot invalidates the row.
    The latest valid date is the conservative availability bound, not a claim
    that the provider offers immutable point-in-time historical versions.
    """
    end, cutoff = provider_date(period), provider_date(as_of)
    if not end or not cutoff or end > cutoff or not hasattr(fields, "get"):
        return None
    dates = {}
    # Unknown date aliases have unverified semantics. Do not silently ignore
    # them (or guess that an UPDATE_DATE suffix is harmless metadata).
    for field in fields.keys():
        if (str(field).startswith(("NOTICE_DATE", "UPDATE_DATE"))
                and field not in DATE_FIELDS
                and str(fields.get(field)).strip().lower() not in ("", "none", "nan", "nat", "<na>", "null")):
            return None
    for field in DATE_FIELDS:
        raw = fields.get(field)
        if raw is None or str(raw).strip().lower() in ("", "none", "nan", "nat", "<na>", "null"):
            continue
        parsed = provider_date(raw)
        if not parsed or not end <= parsed <= cutoff:
            return None
        dates[field] = parsed
    if not dates:
        return None
    return {"日期字段": dates, "披露时点依据": max(dates.values())}


def validated_cashflow(evidence, period, currency, as_of):
    """Only accept the cash-flow row's own matching period/currency/date proof."""
    if (not isinstance(evidence, dict)
            or evidence.get("来源") != CASHFLOW_SOURCE
            or evidence.get("报告期") != period
            or evidence.get("币种") != currency):
        return None
    timing = timing_evidence(evidence.get("日期字段"), period, as_of)
    if not timing or evidence.get("披露时点依据") != timing["披露时点依据"]:
        return None
    return {**evidence, **timing}
