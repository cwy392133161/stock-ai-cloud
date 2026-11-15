"""Deterministic bank ratio identities and conservative prose screening.

This is a bounded lexical exclusion of known errors, not semantic validation.
Keep the authoritative values and literal source quotes outside this screen.
"""
import re
import unicodedata

BANK_RATIO_SOURCE = "https://jrs.mof.gov.cn/zhengcefabu/cwjx/202006/t20200602_3524593.htm"

BANK_RATIO_RULES = (
    "拨备覆盖率=贷款损失准备余额÷不良贷款余额×100%；"
    "不良贷款率=不良贷款余额÷贷款总额×100%，两者分母不同。"
    "仅在其他条件不变且口径可比时，准备余额增加会提高拨备覆盖率，"
    "不良贷款余额增加会降低拨备覆盖率；反向变化的方向相反。"
    "贷款总额变化不能直接解释拨备覆盖率变化。"
    "这些是公式关系，不是任何一家银行的实际成因；"
    "须核验同口径准备与不良贷款余额、核销及计提等变动，不能仅凭比率推断原因。"
)

_TRADITIONAL = str.maketrans({
    "撥": "拨", "備": "备", "蓋": "盖", "貸": "贷", "餘": "余", "額": "额",
    "準": "准", "損": "损", "總": "总", "縮": "缩", "擴": "扩", "張": "张",
    "減": "减", "導": "导", "來": "来", "於": "于", "區": "区", "釋": "释",
    "貢": "贡", "獻": "献", "驅": "驱", "動": "动", "緩": "缓", "變": "变",
})
_COVERAGE = re.compile(r"拨备覆盖率|准备覆盖率|拨贷覆盖率")
_LOAN_DENOMINATOR = re.compile(
    r"(?<!不良)贷款(?:总额|总量|余额|分母|扩张|扩展|增长|增加|下降|减少|收缩)|贷款规模"
)
_COMPONENT = re.compile(r"准备|拨备余额|不良(?:贷款)?(?:余额|规模)|不良贷款")
_RELATION = re.compile(r"来自|源于|导致|由于|因为|因而|成因|归因|区分|解释|贡献|驱动|带来|使得|反映|取决于")
_DIRECTION = re.compile(r"上升|下降|增加|减少|增长|收缩|扩张|提高|降低|放缓|加快|改善|恶化")


def blocks_bank_ratio_inference(text):
    """Reject known denominator errors or unsupported component causation.

    A cautious suffix or question is not a license for an invalid mechanism.
    Correct mathematical identities are supplied by BANK_RATIO_RULES instead
    of trying to certify model-written algebra or issuer-specific causation.
    Pure numeric facts and neutral requests for component data are untouched.
    """
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).translate(_TRADITIONAL)
    for sentence in re.split(r"[。！？!?；;\n]", normalized):
        if not _COVERAGE.search(sentence):
            continue
        if _LOAN_DENOMINATOR.search(sentence):
            return True
        if _COMPONENT.search(sentence) and _RELATION.search(sentence):
            return True
        # Includes a conditional hypothesis about the wrong direction, even
        # without a causal verb (e.g. coverage down / NPL balance down).
        if len(_DIRECTION.findall(sentence)) >= 2 and _COMPONENT.search(sentence):
            return True
    return False
