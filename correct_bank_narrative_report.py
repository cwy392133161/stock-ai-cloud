"""Second bounded correction of published bank prose; no generation or re-audit.

Preserve source bytes except selected model paragraphs and one explicit banner.
The caller retains the full source page and audit before publishing the result.
"""
import hashlib
import re
from bs4 import BeautifulSoup
from bank_ratio_guard import BANK_RATIO_RULES, blocks_bank_ratio_inference, blocks_bank_fact_assertion

MARKER = "bank-narrative-correction"
LABELS = ("入选逻辑", "经营和估值", "反面证据", "跟踪条件")


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def correct_page(source):
    page = BeautifulSoup(source, "html.parser")
    generation = page.select('meta[name="research-generation-id"]')
    articles = page.select('article')
    if page.select_one('.' + MARKER) or len(generation) != 1 or len(articles) != 5 or not page.h1:
        raise ValueError('重复更正或缺少五篇报告、唯一生成ID及标题')
    removed = []
    replacements = []
    for article in articles:
        heading = article.find('b', recursive=False)
        insight = article.select_one('.ai-insight')
        if not heading or '银行' not in heading.get_text() or not article.select_one('.bank-evidence') or insight is None:
            raise ValueError('缺少明确银行主体、证据或模型正文边界')
        paragraphs = insight.find_all('p', recursive=False)
        for paragraph in paragraphs:
            label = paragraph.find('b', recursive=False)
            if not label or label.get_text() not in LABELS:
                continue
            original = paragraph.get_text(' ', strip=True)
            reasons = []
            if blocks_bank_ratio_inference(original):
                reasons.append('unverified-ratio-mechanism')
            if blocks_bank_fact_assertion(original):
                reasons.append('bank-direction-outside-literal-claim-channel')
            if not reasons:
                continue
            old = str(paragraph)
            # Fail closed if serialization cannot identify exactly one source block.
            if source.count(old) != 1:
                raise ValueError('正文原始字节不唯一或序列化发生变化')
            new = ('<p><b>' + label.get_text() + '</b><br/>' +
                   '本段含未经独立核验的银行指标方向或比率成因表述，已局部屏蔽。' +
                   '请核对保留的证据；未通过逐字主张审核不等于财报未披露相关数据，其余段落亦未完成全文复审。</p>')
            replacements.append((old, new))
            removed.append({'issuer': heading.get_text(), 'section': label.get_text(),
                            'reasons': reasons, 'original_text': original, 'original_html': old})
    if not removed:
        raise ValueError('未发现目标表述，不能伪称已更正')
    output = source
    for old, new in replacements:
        output = output.replace(old, new, 1)
    banner = ('<aside class="' + MARKER + '" role="alert"><strong>追加局部更正：银行事实与机制表述待复审</strong><p>' +
              BANK_RATIO_RULES + '</p><p>仅筛查并屏蔽特定银行指标方向及机制表述，未调用AI、未重审模型原答。' +
              '原生成ID、时间、证据和历史审核计数保留；不是全文复审或新生成。' +
              '归档摘录包含平安银行逾期占比变化，但正文改写句未通过独立逐字主张核验，不能把缺少核验说成财报没有数据。' +
              '</p><a href="./history/2026-11-13/bank-narrative-correction.json">本次更正审计</a></aside>')
    old_limit = '官方PDF未提供与经营财务主题相关的逐字事实主张；仅有位置匹配或无关事实不能支持研究判断'
    new_limit = '本期模型主张未通过经营财务主题的逐字事实核验；不表示财报未披露相关数据，位置匹配或无关事实不能支持研究判断'
    limitation_count = output.count(old_limit)
    output = output.replace(old_limit, new_limit)
    output = output.replace('</h1>', '</h1>' + banner, 1)
    return output, {'scope': 'bounded-bank-narrative-correction-not-full-reaudit',
                    'original_generation_id': generation[0]['content'],
                    'source_sha256': digest(source), 'corrected_sha256': digest(output),
                    'removed_paragraphs': removed, 'limitation_wording': {'original': old_limit, 'replacement': new_limit, 'count': limitation_count}}


def reconcile_status_page(original_source, current_source, prior_audit):
    """Replay an existing correction over a status-only main update.

    Fail closed on any change outside the two publication alerts. Never revise
    the earlier audit's source or hashes to pretend it used the newer report.
    """
    def without_alerts(source):
        for name in ('market-publication-alert', 'research-publication-alert'):
            pattern = r'<p\b[^>]*class="' + name + r'"[^>]*>.*?</p>'
            source, count = re.subn(pattern, '', source, flags=re.S)
            if count != 1:
                raise ValueError('状态警示边界缺失或不唯一')
        return source

    if without_alerts(original_source) != without_alerts(current_source):
        raise ValueError('正文或非状态内容已改变，不能只重放旧局部更正')
    previous_output, previous = correct_page(original_source)
    for key in previous:
        if previous[key] != prior_audit.get(key):
            raise ValueError('旧审计与原始更正不能逐项重放')
    output, audit = correct_page(current_source)
    if audit['removed_paragraphs'] != previous['removed_paragraphs']:
        raise ValueError('待屏蔽段落与旧审计不同')
    old_link = './history/2026-11-13/bank-narrative-correction.json'
    new_link = './history/2026-11-13/bank-narrative-status-reconciliation.json'
    if output.count('href="' + old_link + '"') != 1:
        raise ValueError('更正审计链接不唯一')
    output = output.replace('href="' + old_link + '"', 'href="' + new_link + '"', 1)
    audit['corrected_sha256'] = digest(output)
    audit['scope'] = 'status-only-reconciliation-of-bounded-correction-not-full-reaudit'
    audit['previous_correction'] = {
        'audit': 'bank-narrative-correction.json',
        'source_sha256': previous['source_sha256'],
        'corrected_sha256': digest(previous_output),
    }
    return output, audit
