"""排版相关纯函数的回归测试。

覆盖三处曾经真实出问题的排版处理：
1. 段首硬编码全角缩进（U+3000）与 CSS text-indent 叠加 → 首行缩进 4 字符；
2. 正文容器残留的内联 base64 背景图（62KB × 23 章 ≈ 1.4MB）；
3. 标题里的 {{ }} 模板占位符残片（会漏进 nav.xhtml 与阅读器目录）。
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from caixin import text as text_utils  # noqa: E402


def _soup(html: str):
    return BeautifulSoup(html, "html.parser").div


class TestStripHardIndent:
    def test_strips_ideographic_space_at_paragraph_start(self) -> None:
        root = _soup("<div><p>　　这是正文第一段。</p><p>　　第二段。</p></div>")
        fixed = text_utils.strip_hard_indent(root)
        assert fixed == 2
        assert root.find_all("p")[0].get_text() == "这是正文第一段。"
        assert root.find_all("p")[1].get_text() == "第二段。"

    def test_leaves_paragraphs_without_indent_untouched(self) -> None:
        root = _soup("<div><p>本来就没有缩进。</p></div>")
        assert text_utils.strip_hard_indent(root) == 0
        assert root.p.get_text() == "本来就没有缩进。"

    def test_does_not_touch_paragraph_starting_with_tag(self) -> None:
        """以 <strong> 开头的段落保持原样，避免误伤加粗小标题。"""
        root = _soup("<div><p><strong>小标题</strong>　　后面的正文</p></div>")
        assert text_utils.strip_hard_indent(root) == 0
        assert root.p.strong.get_text() == "小标题"

    def test_handles_li_and_figcaption(self) -> None:
        root = _soup("<div><ul><li>　　列表项</li></ul><figcaption>　　图注</figcaption></div>")
        assert text_utils.strip_hard_indent(root) == 2
        assert root.li.get_text() == "列表项"
        assert root.figcaption.get_text() == "图注"

    def test_keeps_inner_full_width_spaces(self) -> None:
        """只剥行首，段内的全角空格要保留。"""
        root = _soup("<div><p>　　财新　周刊</p></div>")
        text_utils.strip_hard_indent(root)
        assert root.p.get_text() == "财新　周刊"

    def test_tolerates_empty_paragraphs(self) -> None:
        """空 `<p></p>` 没有首个子节点，不能因此抛 IndexError。"""
        root = _soup("<div><p></p><p>　　有内容</p></div>")
        assert text_utils.strip_hard_indent(root) == 1


class TestSanitizeInlineStyle:
    def test_drops_base64_background(self) -> None:
        style = 'background: url("data:image/png;base64,iVBORw0KGgoAAAANSUhEUg")'
        assert text_utils.sanitize_inline_style(style) is None

    def test_drops_overlong_style(self) -> None:
        assert text_utils.sanitize_inline_style("color: red;" + "x" * 400) is None

    def test_keeps_short_semantic_style(self) -> None:
        assert text_utils.sanitize_inline_style("text-align:right;") == "text-align:right;"

    def test_returns_none_for_missing_style(self) -> None:
        assert text_utils.sanitize_inline_style(None) is None
        assert text_utils.sanitize_inline_style("") is None


class TestCleanTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("{{周刊导播｜抢占办公室}}", "周刊导播｜抢占办公室"),
            ("{{周刊导播｜抢占办公室", "周刊导播｜抢占办公室"),  # 不成对的残片
            ("周刊导播}}", "周刊导播"),
            ("  正常标题  ", "正常标题"),
        ],
    )
    def test_strips_template_braces(self, raw: str, expected: str) -> None:
        assert text_utils.clean_title(raw) == expected

    def test_falls_back_when_empty(self) -> None:
        assert text_utils.clean_title("", fallback="（无标题）") == "（无标题）"
        assert text_utils.clean_title("{{}}", fallback="（无标题）") == "（无标题）"
        assert text_utils.clean_title(None) == "未知标题"


class TestSectionName:
    """期页的栏目标题是「中文名 + 英文名」，目录页只留中文。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("金融 Finance", "金融"),
            ("环境与科技 Environment&Tech", "环境与科技"),
            ("周刊导播", "周刊导播"),          # 只有中文
            ("封面报道 Cover Story", "封面报道"),
            ("  民生   Society  ", "民生"),    # 多余空白要压掉
            ("", ""),
        ],
    )
    def test_keeps_chinese_part_only(self, raw: str, expected: str) -> None:
        assert text_utils.section_name(raw) == expected

    def test_falls_back_to_raw_for_english_only(self) -> None:
        assert text_utils.section_name("Opinion") == "Opinion"


class TestStripSectionPrefix:
    """目录页已经用分组标题表达了栏目，条目里再重复一遍就啰嗦了。"""

    @pytest.mark.parametrize(
        ("title", "section", "expected"),
        [
            ("封面报道｜抢占办公室", "封面报道", "抢占办公室"),
            ("财新观察｜与诺奖相比，诺奖级成果是第一位的", "财新观察", "与诺奖相比，诺奖级成果是第一位的"),
            ("财新周刊｜AI重塑金融", "金融", "AI重塑金融"),      # 泛用前缀
            ("最新封面报道｜售电巨亏谁埋单？", "封面报道", "售电巨亏谁埋单？"),  # 期页爱加「最新」
            ("最新财新周刊｜户外运动乱象待破局", "时事", "户外运动乱象待破局"),
            ("最新周刊导播｜售电巨亏谁埋单、暂别AI伴侣", "周刊导播", "售电巨亏谁埋单、暂别AI伴侣"),
            ("专栏｜不断深化的滞胀格局", "金融", "专栏｜不断深化的滞胀格局"),  # 体裁标记，保留
            ("回溯｜一周回溯", "开卷", "回溯｜一周回溯"),
            ("没有任何前缀的标题", "金融", "没有任何前缀的标题"),
        ],
    )
    def test_strips_only_section_and_publisher_prefix(
        self, title: str, section: str, expected: str
    ) -> None:
        assert text_utils.strip_section_prefix(title, section) == expected

    def test_keeps_title_when_stripping_would_empty_it(self) -> None:
        """「金融｜」这种只剩前缀的异常标题不能被削成空串。"""
        assert text_utils.strip_section_prefix("金融｜", "金融") == "金融｜"

    def test_tolerates_missing_section(self) -> None:
        assert text_utils.strip_section_prefix("财新周刊｜AI重塑金融", "") == "AI重塑金融"


class TestStripWatermarks:
    """水印清理同样只能认叶子块 —— 否则包着正文的容器会被整块删掉。"""

    def test_removes_leaf_watermark_paragraph(self) -> None:
        root = _soup(
            "<div><p>正文第一段，正常内容。</p><p>本文为财新传媒版权所有，翻录必究。</p></div>"
        )
        assert text_utils.strip_watermarks(root) >= 1
        assert "本文为财新" not in root.get_text()
        assert "正文第一段" in root.get_text()

    def test_keeps_wrapper_that_merely_mentions_watermark(self) -> None:
        """回归：容器一旦被当成水印删掉，整篇正文就没了。

        `get_text()` 会拼上所有子孙文本，所以「本文为财新」出现在某一段里时，
        外层 <div> 的文本也包含它、且总长 < 180 —— 旧实现会把整个容器删掉。
        """
        html = (
            "<div class='wrap'>"
            "<p>本文为财新传媒版权所有，翻录必究。</p>"
            "<p>但这一段是真正的正文，不能被删掉。</p>"
            "</div>"
        )
        root = _soup(f"<div>{html}</div>")
        text_utils.strip_watermarks(root)
        assert "真正的正文" in root.get_text(strip=True)

    def test_removes_bare_long_number(self) -> None:
        """纯长数字串是防盗码。"""
        root = _soup("<div><p>正文。</p><p>1234567890123</p></div>")
        text_utils.strip_watermarks(root)
        assert "1234567890123" not in root.get_text()

    def test_removes_empty_tags(self) -> None:
        root = _soup("<div><p>正文。</p><p></p><div><span></span></div></div>")
        text_utils.strip_watermarks(root)
        assert len(root.find_all("p")) == 1

    def test_keeps_images_and_rules(self) -> None:
        """自闭合标签本身没有文本，不能被当成空标签删掉。"""
        root = _soup('<div><img src="a.png"/><hr/><p>正文。</p></div>')
        text_utils.strip_watermarks(root)
        assert root.find("img") is not None
        assert root.find("hr") is not None
