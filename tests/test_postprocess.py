"""抓取后处理的回归测试：文末截断 + 日期归一化。

这两块都出过真实问题：
1. 文末截断按「关键词 in 整段文本」定位，而 `find_all` 是文档序（祖先先于后代），
   包裹整篇正文的 `<div>` 会整体命中并被整块删掉 —— 实测得到空正文。
2. 出版日期只认「2026年10月12日」，而期页写的是「出版日期：2026-10-12」，
   于是日期退回打包当天，dc:date 还会因为不是 ISO 8601 而校验失败。
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from caixin.text import (  # noqa: E402
    format_cn_date,
    parse_iso_date,
    truncate_after_end_marker,
)


def _root(html: str):
    return BeautifulSoup(html, "html.parser").div


# ══ 文末截断 ══════════════════════════════════════════════════════════


class TestTruncateAfterEndMarker:
    def test_truncates_tail_after_marker(self) -> None:
        root = _root(
            "<div>"
            "<p>正文第一段。</p><p>正文第二段。</p>"
            "<p>相关阅读</p><p>推荐一</p><p>推荐二</p>"
            "</div>"
        )
        assert truncate_after_end_marker(root) is True
        text = root.get_text(strip=True)
        assert "正文第二段" in text
        assert "推荐一" not in text and "相关阅读" not in text

    def test_never_deletes_whole_article_when_body_mentions_keyword(self) -> None:
        """回归：正文提到关键词、外层有包裹 div 时，绝不能把整篇删掉。

        旧实现里 `<div class="wrap">` 的 get_text() 拼上了所有子孙文本，
        于是它自己命中「相关阅读」并被整块删除，剩余内容为空字符串。
        财新每篇正文都是 `content-body > div > p...` 这个形状，所以这条路径
        一直真实存在。
        """
        root = _root(
            "<div>"
            '<div class="wrap">'
            "<p>本文探讨相关阅读产品的设计。</p>"
            "<p>正文第二段，正常内容。</p>"
            "<p>正文第三段，正常内容。</p>"
            "</div>"
            "</div>"
        )
        assert truncate_after_end_marker(root) is False
        text = root.get_text(strip=True)
        assert "正文第二段" in text
        assert "正文第三段" in text

    def test_marker_must_start_with_keyword(self) -> None:
        """「本文探讨相关阅读产品」不是标志；「相关阅读：xxx」才是。"""
        root = _root(
            "<div><p>第一段。</p><p>本文探讨相关阅读产品的设计。</p>"
            "<p>相关阅读：AI 与就业</p><p>尾巴</p></div>"
        )
        assert truncate_after_end_marker(root) is True
        text = root.get_text(strip=True)
        assert "本文探讨相关阅读产品的设计" in text
        assert "尾巴" not in text

    def test_marker_nested_in_container_truncates_at_every_level(self) -> None:
        """标志嵌在容器里时，祖先层级上的后续兄弟也要一起删掉。"""
        root = _root(
            "<div>"
            "<p>正文。</p>"
            '<div class="wrap"><p>正文二。</p><p>延伸阅读</p><p>尾巴一</p></div>'
            "<p>尾巴二</p>"
            "</div>"
        )
        assert truncate_after_end_marker(root) is True
        text = root.get_text(strip=True)
        assert "正文二" in text
        assert "尾巴一" not in text and "尾巴二" not in text

    def test_mid_article_section_heading_is_not_a_marker(self) -> None:
        """正文中段的同名小标题后面跟着成段正文，不能触发截断。"""
        para = (
            "这是一段完整的正文内容，长度接近真实段落。财新的正文段落通常在八十到三百字之间，"
            "而相关推荐列表里只会是短标题，所以「标志之后还有长段落」就说明这只是正文中的小标题。"
        )
        body = "".join(f"<p>{para}</p>" for _ in range(4))
        root = _root(f"<div><p>延伸阅读</p>{body}</div>")
        assert truncate_after_end_marker(root) is False
        assert root.get_text(strip=True).count("完整的正文内容") == 4

    def test_long_paragraph_is_never_a_marker(self) -> None:
        root = _root("<div>" + "<p>正文。</p>" * 6 + f"<p>相关阅读{'很长的正文' * 20}</p></div>")
        assert truncate_after_end_marker(root) is False

    def test_no_marker_keeps_content_untouched(self) -> None:
        root = _root("<div><p>第一段。</p><p>第二段。</p></div>")
        assert truncate_after_end_marker(root) is False
        assert len(root.find_all("p")) == 2

    def test_takes_the_last_marker(self) -> None:
        root = _root(
            "<div><p>正文一。</p><p>相关阅读</p><p>中间段。</p>"
            "<p>推荐阅读</p><p>真正的尾巴</p></div>"
        )
        assert truncate_after_end_marker(root) is True
        text = root.get_text(strip=True)
        assert "中间段" in text
        assert "真正的尾巴" not in text


# ══ 日期 ══════════════════════════════════════════════════════════════


class TestParseIsoDate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("本文来源于《财新周刊》 2026年第39期 出版日期：2026-10-12", "2026-10-12"),
            ("2026年10月12日", "2026-10-12"),
            ("2026/1/5", "2026-01-05"),
            ("出版日期：2026-1-5", "2026-01-05"),
        ],
    )
    def test_parses_both_chinese_and_dash_formats(self, raw: str, expected: str) -> None:
        assert parse_iso_date(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "没有任何日期", "2026年13月40日"])
    def test_falls_back_when_absent_or_impossible(self, raw) -> None:
        assert parse_iso_date(raw, "2020-01-01") == "2020-01-01"

    def test_returns_fallback_by_default(self) -> None:
        assert parse_iso_date("") == ""


class TestFormatCnDate:
    def test_converts_iso_to_chinese(self) -> None:
        assert format_cn_date("2026-10-12") == "2026年10月12日"

    def test_passes_through_non_iso(self) -> None:
        assert format_cn_date("2026年10月12日") == "2026年10月12日"
        assert format_cn_date("") == ""
        assert format_cn_date(None) == ""

    def test_round_trip(self) -> None:
        assert format_cn_date(parse_iso_date("2026年10月12日")) == "2026年10月12日"
