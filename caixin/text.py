"""纯文本 / HTML 后处理：标题清洗、缩进剥离、内联样式消毒、文末截断、日期归一。

这里的函数都不依赖 Playwright，可以单独测试。
"""

import re

from bs4 import NavigableString

from .config import (
    _LEADING_NOISE,
    _NESTED_BLOCK_TAGS,
    DROP_STYLE_MARKERS,
    FILENAME_BAD_CHARS,
    HARD_INDENT_CHARS,
    LONG_NUMBER_RE,
    MAX_END_MARKER_LEN,
    MAX_INLINE_STYLE_LEN,
    MAX_TAIL_BLOCK_LEN,
    MAX_WATERMARK_LEN,
    TRUNC_BLOCK_TAGS,
    TRUNC_KEYWORDS,
    WATERMARK_KEYWORDS,
)

# ── 日期 ────────────────────────────────────────────────────────────
# 财新的日期有两种写法：期页是「出版日期：2026-10-12」，历史期刊列表是
# 「2026年10月12日」。内部统一存 ISO 8601（dc:date 的硬性要求），
# 展示时再转回中文格式。

_DATE_RE = re.compile(r"(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})")


def parse_iso_date(text, fallback=""):
    """从任意文本里抠出日期并归一成 YYYY-MM-DD；抠不到返回 fallback。"""
    m = _DATE_RE.search(text or "")
    if not m:
        return fallback
    year, month, day = (int(g) for g in m.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return fallback
    return f"{year:04d}-{month:02d}-{day:02d}"


def format_cn_date(iso, fallback=""):
    """把 YYYY-MM-DD 转成「2026年10月12日」用于展示；不是 ISO 就原样返回。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (iso or "").strip())
    if not m:
        return iso or fallback
    return f"{m.group(1)}年{m.group(2)}月{m.group(3)}日"


# ── 正文边界截断 ─────────────────────────────────────────────────────


def _is_end_marker_text(text):
    """判断一段文本像不像文末标志。

    要求**以关键词开头**（允许前面有书名号/冒号这类装饰字符），而不是仅仅
    「包含」关键词 —— 否则「本文探讨相关阅读产品的设计」这种正文句子也会被
    当成标志，整段正文就没了。
    """
    cleaned = text.lstrip(_LEADING_NOISE)
    return any(cleaned.startswith(kw) for kw in TRUNC_KEYWORDS)


def _leaf_blocks(root):
    """只取叶子块：自身不含其他块级元素的标签。

    `find_all` 按文档序返回（祖先先于后代），而 `get_text()` 会把所有子孙文本
    拼进来。所以包裹整篇正文的那个 `<div>` 只要正文里提到过关键词就会整体命中，
    并被整块删掉 —— 实测结果是空正文。财新每篇正文都包在一层 `<div>` 里，
    这条路径一直真实存在，所以必须先按「叶子」过滤掉所有容器。
    """
    return [t for t in root.find_all(TRUNC_BLOCK_TAGS) if not t.find(_NESTED_BLOCK_TAGS)]


def find_end_marker(root):
    """从后往前找文末标志块，找不到返回 None。

    四重限制，全部是为了「宁可少截，也不能误删」：

    1. **只认叶子块** —— 见 `_leaf_blocks`，这是防「整篇被删」的关键。
    2. **必须以关键词开头且足够短** —— 见 `_is_end_marker_text`；仅「包含」
       关键词是不够的，「本文探讨相关阅读产品的设计」这种正文句子会误命中。
    3. **其后不能有长段落** —— 相关推荐是一串短标题；若标志之后还跟着成段
       正文，说明它只是正文中段的「延伸阅读」小标题，不能截。
    4. 多个命中时取**最后一个**。
    """
    blocks = _leaf_blocks(root)
    for idx in range(len(blocks) - 1, -1, -1):
        text = blocks[idx].get_text(strip=True)
        if not text or len(text) > MAX_END_MARKER_LEN:
            continue
        if not _is_end_marker_text(text):
            continue
        tail = blocks[idx + 1:]
        if any(len(b.get_text(strip=True)) > MAX_TAIL_BLOCK_LEN for b in tail):
            continue
        return blocks[idx]
    return None


def _drop_from(root, node):
    """删除 node 及其后所有内容 —— 沿祖先链逐层切，嵌套结构也能切干净。

    向上走时只删祖先的**后续兄弟**，祖先本身必须留着：它里面还有标志之前的正文。
    （早先的写法会连祖先一起 decompose，于是整段正文跟着没了。）
    """
    parent = node.parent
    for sibling in list(node.find_next_siblings()):
        sibling.decompose()
    node.decompose()

    while parent is not None and parent is not root:
        grandparent = parent.parent
        for sibling in list(parent.find_next_siblings()):
            sibling.decompose()
        parent = grandparent


def truncate_after_end_marker(root):
    """截掉文末标志及其后的所有内容，返回是否发生截断。

    注：这里不处理「文末 logo 链接」。财新的「财」字标志是刻意保留的排版元素
    （见 CSS 的 .article-img--marker），按 logo 截断会把它一起删掉；而它之后的
    相关推荐块已由关键词规则与 blacklist 覆盖。
    """
    marker = find_end_marker(root)
    if marker is None:
        return False
    _drop_from(root, marker)
    return True


_CJK_HEAD_RE = re.compile(r"^[\u4e00-\u9fff]+")


def section_name(raw):
    """把期页的栏目标题压成中文主名：「金融 Finance」→「金融」。

    财新的分栏小标题都是「中文名 + 英文名」（`环境与科技 Environment&Tech`），
    也有只有中文的（`周刊导播`）。目录页只展示中文名，免得中英混排把目录行
    拉长、也让分组标题更像一个标签。整条都是西文时原样返回。
    """
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    match = _CJK_HEAD_RE.match(text)
    return (match.group(0) if match else text).strip()


def strip_section_prefix(title, section):
    """去掉标题里与栏目重复的前缀：「封面报道｜抢占办公室」→「抢占办公室」。

    期页的链接文字带着栏目前缀，两种形态：泛用的 `财新周刊｜`，或栏目名本身。
    目录页已经用分组标题表达了栏目，再重复一遍既啰嗦又把目录行拉长。

    `专栏｜`「随笔｜」「前瞻｜」这类是**体裁标记**而不是栏目（同一栏目下会混着
    好几种），保留 —— 读者靠它快速分辨正文与专栏。

    前缀还会带「最新」二字（`最新封面报道｜售电巨亏谁埋单？`），是期页的编辑习惯，
    不是标题的一部分。第 40 期 27 条里有 12 条是这种，漏掉会让目录显得很脏。
    """
    for base in ("财新周刊", section or ""):
        if not base:
            continue
        for prefix in (base, "最新" + base):
            if title.startswith(prefix + "｜"):
                stripped = title[len(prefix) + 1:].strip()
                if stripped:
                    return stripped
    return title


def clean_title(raw, fallback="未知标题"):
    """清掉财新标题里的 {{ }} 模板标记与多余空白。

    财新的目录/标题文本里混有 {{ }} 标记，而且常常不成对（只有前半个 `{{`）。
    这里只删括号字符本身、保留中间的文字 —— 与自建目录页原有的处理保持一致。
    """
    t = (raw or "").replace("{{", "").replace("}}", "")
    return re.sub(r"\s+", " ", t).strip() or fallback


def safe_filename(name, fallback="未命名", limit=80):
    """把任意标题变成可用作文件名的字符串。

    标题直接来自网页：含 `/` 会写到别的目录或直接失败，`:` `|` `?` `*`
    在 Windows 上非法。这里统一替换成下划线并截断。
    """
    cleaned = FILENAME_BAD_CHARS.sub("_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned.strip("_"):
        return fallback
    return cleaned[:limit] or fallback


def sanitize_inline_style(style):
    """丢弃内联背景图 / 超长内联样式，其余保留（如「回声」栏目右对齐的署名）。

    财新把一张 62KB 的 base64 背景纹理挂在正文容器上，整期重复 23 次 ≈ 1.4MB。
    """
    if not style:
        return None
    if any(m in style for m in DROP_STYLE_MARKERS) or len(style) > MAX_INLINE_STYLE_LEN:
        return None
    return style


def strip_watermarks(root):
    """删掉水印、版权声明、纯数字防盗码，以及空标签，返回删除的标签数。

    水印判定同样**只认叶子块**：`get_text()` 会把所有子孙文本拼进来，一个包着
    两段正文的 `<div>` 只要总长不到 180 字、又恰好提到「本文为财新」，就会被
    整块删掉 —— 与文末截断踩的是同一个坑，只是这里多了长度兜底。
    """
    removed = 0
    for tag in root.find_all(True):
        text = tag.get_text(strip=True)
        is_leaf = not tag.find(_NESTED_BLOCK_TAGS)
        if is_leaf and text and (
            (any(k in text for k in WATERMARK_KEYWORDS) and len(text) < MAX_WATERMARK_LEN)
            or LONG_NUMBER_RE.match(text)
        ):
            tag.decompose()
            removed += 1
            continue
        # 空的或只有空白字符的标签（非自闭合标签）
        if not text and not tag.find_all() and tag.name not in ("img", "br", "hr"):
            tag.decompose()
            removed += 1
    return removed


def strip_hard_indent(root):
    """剥掉正文段首的硬编码全角缩进，返回处理的段落数。

    只看块级元素的第一个子节点，且仅在它是纯文本时才动手 —— 以 <strong> 之类
    标签开头的段落保持原样，避免误伤。
    """
    fixed = 0
    for p in root.find_all(["p", "li", "figcaption"]):
        if not p.contents:
            continue
        first = p.contents[0]
        if not isinstance(first, NavigableString):
            continue
        cleaned = first.lstrip(HARD_INDENT_CHARS)
        if cleaned != first:
            first.replace_with(cleaned)
            fixed += 1
    return fixed
