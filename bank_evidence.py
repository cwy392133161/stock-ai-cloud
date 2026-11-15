"""Bank ratios from dated CNInfo statements. Keep the source words and PDF page."""
import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

from period_reports import period_title_score


METRICS = ("净息差", "不良贷款率", "拨备覆盖率", "核心一级资本充足率")
LABEL = "|".join(METRICS)
NUMBER = r"\d{1,4}(?:\.\d{1,4})?"
NARRATIVE = re.compile(rf"(?:^|[，,；;。])(?:本集团|本公司|本行)?(?P<name>{LABEL})(?:为|为约)?(?P<value>{NUMBER})[%％]")
CHANGE = re.compile(
    rf"^[，,]?(?:与|较|比)(?P<base>上年末|年初|去年同期|上年同期)"
    rf"(?:(?P<flat>持平)|(?P<direction>上升|提高|提升|下降|降低|减少)"
    rf"(?P<number>{NUMBER})(?P<unit>个百分点|个基点|基点))")
TABLE_ROW = re.compile(rf"^\s*(?:\d+[、.])?\s*(?P<name>{LABEL}|净利息收益率)\s*"
                       rf"(?P<unit>[（(]\s*(?:年化\s*[，,]\s*)?[%％]\s*[）)])?\s+"
                       rf"(?P<current>{NUMBER})(?P<percent>[%％]?)\s+"
                       rf"(?P<prior>{NUMBER})[%％]?(?:\s|$)")


def is_bank(stock):
    """Candidate for reading a bank filing. A name alone does not confirm the framework."""
    industry = str(stock.get("行业") or "").strip()
    if re.fullmatch(r"银行|銀行|银行业|銀行業|Bank(?:s|ing)?", industry, re.I):
        return True
    if industry.lower() not in ("", "货币金融服务", "未知", "未知行业", "未分类", "未核实",
                                "—", "-", "nan", "none", "null", "n/a", "unknown"):
        return False
    return _bank_name(stock)


def _bank_name(stock):
    name = re.sub(r"^(?:XD|DR|XR)", "", str(stock.get("名称") or "").strip())
    return bool(re.fullmatch(r".+(?:银行|銀行|农商行)", name) or name == "张家港行")


def with_bank_classification(stock, data):
    """Only a published industry table matching this snapshot can corroborate a bank name."""
    if not _bank_name(stock):
        return stock
    ranking = data.get("ranking_analysis") or {}
    day = str(data.get("updated") or "")[:10]
    source = ranking.get("source") or {}
    industry = (ranking.get("stocks") or {}).get(stock.get("代码"), {})
    if (ranking.get("trade_date") != day or not source.get("published") or source["published"] >= day
            or urlsplit(str(source.get("source_url") or "")).hostname != "sp.capco.org.cn"
            or industry.get("name") != stock.get("名称") or industry.get("industry") != "货币金融服务"):
        return stock
    return {**stock, "行业": "货币金融服务",
            "银行分类依据": f'中上协 {source["published"]} 已发布的货币金融服务分类与发行人银行名称一致'}


def is_current_bank_report(notice, period):
    parts = urlsplit(str(notice.get("链接") or ""))
    return (parts.scheme == "https" and parts.netloc == "static.cninfo.com.cn"
            and parts.path.lower().endswith(".pdf")
            and notice.get("报告期匹配", period) == period
            and period_title_score(notice.get("标题", ""), period, "A股") >= 100)


def _compact(text):
    return re.sub(r"\s+", "", text)


def _narrative_facts(text, period):
    # Accept only the filing's own period. Never anchor a subsidiary's or a
    # different year's figures to the quarter merely because its PDF matched.
    end = rf"(?:截至)?(?:本?报告期末|{period.year}年{period.month}月(?:末|{period.day}日))"
    ytd = {3: "一季度", 6: "上半年", 9: "前三季度", 12: "全年"}[period.month]
    cumulative = rf"{period.year}年(?:{ytd}|1[-至—]{period.month}月)"
    # A new dated paragraph can follow a PDF page header with no punctuation.
    text = re.sub(rf"(?m)^\s*(?=(?:{end}|{cumulative})\s*[，,])", "。",
                  "\n".join(_compact(line) for line in text.splitlines()))
    for sentence in re.split(r"[。！？]", _compact(text)):
        anchor = re.search(rf"(?:^|[，,；;])(?P<date>{end}|{cumulative})[，,]", sentence)
        if not anchor:
            continue
        body = sentence[anchor.end():]
        if re.search(r"子公司|分行|分部|零售贷款|公司贷款|个人贷款|目标|预计|力争|假设", body):
            continue
        subject = re.match(r"本集团|本公司|本行", body)
        for match in NARRATIVE.finditer(body):
            name = match["name"]
            is_ytd = re.fullmatch(cumulative, anchor["date"]) is not None
            if (name == "净息差") != is_ytd:
                continue
            scope = subject[0] if subject else "该段未标明并表口径"
            delta = CHANGE.match(body[match.end():])
            change = delta[0].lstrip("，,") if delta else ""
            yield name, match["value"], scope, change, is_ytd


def _table_facts(text, period):
    # Retain the exact header and unit with the row. On a new section,
    # comparative table or page, discard context instead of borrowing it.
    header, percent, scope = "", False, None
    split_years = ""
    end = rf"(?:本?报告期末|{period.year}年{period.month}月{period.day}日)"
    prior = rf"(?:上年度?末|{period.year-1}年12月31日)"
    for line in text.splitlines():
        slim = _compact(line)
        if not slim:
            continue
        # Paired date columns can contain separate group, parent or regulator
        # values. Only the exact supported ordering has an unambiguous index.
        if re.fullmatch(r"集团口径本行口径(?:集团口径本行口径)+", slim):
            scope = "集团口径"
            continue
        if re.search(r"集团口径|合并口径|本行口径|母公司口径|监管标准|监管要求", slim):
            header, percent, scope, split_years = "", False, None, ""
            continue
        if re.fullmatch(r"(?:20\d\d年){2,6}", slim):
            split_years = slim
            header, scope = "", None
            continue
        if slim in ("项目", "指标"):
            continue
        if (split_years and re.fullmatch(r"(?:\d{1,2}月\d{1,2}日){2,6}", slim)
                and split_years.startswith(f"{period.year}年{period.year-1}年")
                and slim.startswith(f"{period.month}月{period.day}日12月31日")):
            header, scope = line, None
            split_years = ""
            continue
        split_years = ""
        if re.search(rf"^(?:项目|主要指标|指标)?{end}{prior}", slim):
            header, scope = line, None
            continue
        if (re.match(r"^[（(][一二三四五六七八九十]+[）)]|^(?:项目|主要指标|单位[：:])", slim)
                or re.search(r"(?:20\d\d年.*20\d\d年|报告期末.*(?:上年|期初))", slim)):
            header, percent, scope = "", bool(re.fullmatch(r"单位[：:][%％]", slim)), None
            continue
        row = TABLE_ROW.match(line)
        if not (header and row
                and (percent or row["unit"] or row["percent"])):
            continue
        if row["name"] in ("净息差", "净利息收益率"):
            if "年化" in (row["unit"] or ""):
                yield "净息差", row["current"], "该表未标明并表口径", "", "年化"
            continue
        values = re.findall(rf"(?<!\S)({NUMBER})[%％]?(?=\s|$)", line[row.start("current"):])
        if scope:
            if len(values) < 4:
                continue
            previous = values[2]
        else:
            previous = row["prior"]
        difference = Decimal(row["current"]) - Decimal(previous)
        change = ("较上年末持平" if not difference else "较上年末" +
                  ("上升" if difference > 0 else "下降") + f"{abs(difference):f}个百分点")
        yield row["name"], row["current"], scope or "该表未标明并表口径", change, False


def _candidates(text, period):
    yield from _narrative_facts(text, period)
    yield from _table_facts(text, period)


def _continued_capital(passage):
    """Single-column capital table continued at consecutive line-item numbers."""
    prior = passage.get("上页表头")
    if not isinstance(prior, dict) or prior.get("页") != passage["页"] - 1:
        return
    header = prior.get("摘录", "").splitlines()
    current = (passage.get("摘录") or "").splitlines()
    if len(header) != 3 or len(current) < 2:
        return
    if not (re.fullmatch(r"(?:[（(][一二三四五六七八九十]+[）)])?资本数据", _compact(header[0]))
            and re.fullmatch(r"项目(?:本)?报告期末", _compact(header[1]))):
        return
    previous_item = re.match(r"\s*(\d+)\.(\d+)\s*[\u4e00-\u9fff]", header[2])
    next_item = re.match(r"\s*(\d+)\.(\d+)\s*[\u4e00-\u9fff]", current[0])
    if not (previous_item and next_item and previous_item[1] == next_item[1]
            and int(next_item[2]) == int(previous_item[2]) + 1):
        return
    value = re.fullmatch(rf"\d+[、.]核心一级资本充足率[（(][%％][）)]({NUMBER})", _compact(current[-1]))
    if value:
        yield "核心一级资本充足率", value[1], "该表未标明并表口径", "", False


def select_bank_passages(pages, period):
    """Store short original passages, not uncheckable pre-computed numbers."""
    end = date.fromisoformat(period)
    result = []
    for page, text in enumerate(pages, 1):
        # Sentences may wrap mid-word across lines in the same PDF page.
        for sentence in re.split(r"(?<=[。！？])", text):
            if len(sentence) > 1200:
                # Tables often precede the first paragraph; keep the sentence
                # start at its period anchor, rather than the table above it.
                sentence = sentence[-1200:]
            if list(_narrative_facts(sentence, end)):
                result.append({"页": page, "摘录": sentence.strip()})
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if page > 1:
            previous = [x.strip() for x in pages[page-2].splitlines() if x.strip()]
            titles = [x for x in previous if "资本数据" in x]
            tail = previous[previous.index(titles[-1])+1:] if titles else []
            headings = [x for x in tail if "项目" in x and "报告期末" in x]
            prior_items = [x for x in tail if re.match(r"^\d+\.\d+\s*[\u4e00-\u9fff]", x)]
            this_items = [i for i, x in enumerate(lines) if re.match(r"^\d+\.\d+\s*[\u4e00-\u9fff]", x)]
            if titles and len(headings) == 1 and prior_items and this_items and this_items[0] < 3:
                i = this_items[0]
                for j in range(i + 1, min(i + 14, len(lines))):
                    if re.search(r"^[（(]|项目|报告期末|上年末|单位[：:]", lines[j]):
                        break
                    if "核心一级资本充足率" in lines[j]:
                        continuation = {"页": page, "摘录": "\n".join(lines[i:j+1]),
                                        "上页表头": {"页": page - 1, "摘录": "\n".join(
                                            [titles[-1], headings[-1], prior_items[-1]])}}
                        if list(_continued_capital(continuation)):
                            result.append(continuation)
                        break
        for i, line in enumerate(lines):
            if not TABLE_ROW.match(line):
                continue
            context = lines[max(0, i-30):i]
            # Keep only headers, section boundaries and units, maintaining
            # their original order. Other financial values are unrelated.
            headers = [x for x in context if re.search(
                r"项目|主要指标|报告期末|20\d{2}\s*年|^\d{1,2}\s*月\s*\d{1,2}\s*日|单位[：:]|"
                r"集团口径|合并口径|本行口径|母公司口径|监管标准|监管要求|^[（(][一二三四五六七八九十]+[）)]", x)]
            fragment = "\n".join(headers[-6:] + [line])
            if list(_table_facts(fragment, end)):
                result.append({"页": page, "摘录": fragment})
    return result


def bank_metrics(stock, finance, evidence, as_of):
    if not is_bank(stock):
        return None
    result = {"指标": [], "未定位": list(METRICS)}
    try:
        period = date.fromisoformat(finance["报告期"])
        cutoff = date.fromisoformat(str(as_of)[:10])
    except (KeyError, TypeError, ValueError):
        return result
    if evidence.get("来源") != "巨潮资讯" or period.month not in (3, 6, 9, 12):
        return result
    chosen = {}
    for notice in evidence.get("公告", []):
        try:
            day = date.fromisoformat(notice["日期"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (period <= day <= cutoff and is_current_bank_report(notice, period.isoformat())):
            continue
        for passage in (notice.get("银行指标摘录", []) + notice.get("正文摘录", [])):
            page, text = passage.get("页"), passage.get("摘录")
            if type(page) is not int or page < 1 or not isinstance(text, str):
                continue
            candidates = (_continued_capital(passage) if "上页表头" in passage else _candidates(text, period))
            for name, value, scope, change, is_ytd in candidates:
                if Decimal(value) > (100 if name != "拨备覆盖率" else 10000):
                    continue
                rank = (scope in ("本集团", "集团口径"), not scope.startswith("该"), bool(change))
                if name in chosen and chosen[name][0] >= rank:
                    continue
                chosen[name] = rank, {
                    "指标": name, "数值": value, "单位": "%", "口径": scope,
                    "期间": (f"{period.isoformat()}列（年化，表内未列起始日）"
                           if is_ytd == "年化" else
                           (f"{period.year}-01-01 至 " if is_ytd else "") + period.isoformat()),
                    "变化": change, "披露日": day.isoformat(), "编号": notice.get("编号", ""),
                    "页": page, "链接": notice["链接"],
                }
                if change:
                    chosen[name][1]["对照期"] = (
                        f"{period.year-1}-01-01 至 {period.replace(year=period.year-1)}"
                        if "同期" in change and is_ytd else str(period.replace(year=period.year-1))
                        if "同期" in change else f"{period.year-1}-12-31")
                if "上页表头" in passage:
                    chosen[name][1]["表头页"] = passage["上页表头"]["页"]
    result["指标"] = [chosen[name][1] for name in METRICS if name in chosen]
    result["未定位"] = [name for name in METRICS if name not in chosen]
    return result


def bank_focus(stock, bank):
    """Choose a follow-up from the issuer's observed levels and movements."""
    rows = {r["指标"]: r for r in bank["指标"]}
    if not rows:
        return {"研究主题": "银行披露覆盖", "核心矛盾": "尚无可定位到报告期及官方财报页码的银行指标。",
                "下一期关键问题": "核查同口径净息差、不良贷款率、拨备覆盖率及核心一级资本充足率。"}

    def falling(name):
        return bool(re.search("下降|降低|减少", rows.get(name, {}).get("变化", "")))

    coverage = rows.get("拨备覆盖率", {})
    npl = rows.get("不良贷款率", {})
    if rows.get("净息差", {}).get("变化"):
        theme, pair = "净息差与风险抵补", ("净息差", "拨备覆盖率")
        follow = "下期同口径净息差如何变化？如拨备覆盖率同步下行，核对减值准备与不良贷款余额各自的变化。"
    elif falling("核心一级资本充足率") and coverage:
        theme, pair = "资本缓冲与贷款拨备", ("核心一级资本充足率", "拨备覆盖率")
        follow = "资本充足率下期是否继续下降？分别核对核心一级资本净额、风险加权资产与贷款减值准备。"
    elif falling("拨备覆盖率") and npl:
        theme, pair = "不良率与拨备覆盖的变化", ("不良贷款率", "拨备覆盖率")
        follow = "若不良率继续下降，拨备覆盖率是否也在下降？分别核对不良贷款余额、贷款总额和减值准备。"
    elif "持平" in npl.get("变化", "") and coverage:
        theme, pair = "不良率持平时的拨备变化", ("不良贷款率", "拨备覆盖率")
        follow = "下期不良贷款率是否仍持平？分别核验不良贷款余额、贷款总额、贷款损失准备；不良贷款率与拨备覆盖率使用不同分母，不能相互替代。"
    else:
        theme, pair = "资产质量与资本缓冲", ("不良贷款率", "核心一级资本充足率", "拨备覆盖率")
        follow = "核对下期同口径贷款分类迁徙与减值准备，观察资本和拨备能否同时支撑风险抵补。"
    observed = [rows[k] for k in pair if k in rows] or list(rows.values())
    baseline = "，".join(f'{r["指标"]} {r["数值"]}%（{r["变化"] or r["口径"]}）' for r in observed)
    return {"研究主题": theme, "核心矛盾": f'{stock.get("名称", "")}披露的核对起点为{baseline}。',
            "下一期关键问题": follow}
