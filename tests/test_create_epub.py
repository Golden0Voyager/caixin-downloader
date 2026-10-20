"""create_epub 的结构回归测试：版心包装、封面、目录、导航、元数据、排版尺度。"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from caixin.config import STYLE_FILE, console  # noqa: E402
from caixin.epub_builder import create_epub  # noqa: E402


@pytest.fixture()
def sample_info() -> dict:
    return {"issue_title": "《财新周刊》2026年第25期"}


@pytest.fixture()
def sample_articles() -> list[dict]:
    return [
        {
            "title": "深度报道：AI 时代的就业变局",
            "html": "<div class='content-body'><p>文章正文内容...</p></div>",
        },
        {
            "title": "经济观察：下半年宏观政策展望",
            "html": "<div class='content-body'><p>经济分析内容...</p><p>第二段...</p></div>",
        },
    ]


@pytest.fixture()
def sample_image_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.images = {
        "https://example.com/img1.jpg": {
            "id": "img_abc123",
            "filename": "images/img_abc123.jpg",
            "mime": "image/jpeg",
            "content": b"fake-image-bytes",
        },
    }
    return mgr


class TestCreateEpub:
    def test_creates_valid_epub_file(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name

        try:
            with patch.object(console, "print"):
                create_epub(
                    sample_articles, sample_info, sample_image_manager, out_path
                )

            assert os.path.exists(out_path)
            assert os.path.getsize(out_path) > 0

            # Verify it's a valid ZIP-based EPUB
            with open(out_path, "rb") as f:
                sig = f.read(4)
            # ZIP magic bytes: PK\x03\x04
            assert sig == b"PK\x03\x04", "Not a valid ZIP (EPUB) file"

        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_output_filename_is_respected(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        custom_path = "/tmp/test_caixin_epub.epub"
        try:
            with patch.object(console, "print"):
                create_epub(
                    sample_articles, sample_info, sample_image_manager, custom_path
                )

            assert os.path.exists(custom_path)
        finally:
            if os.path.exists(custom_path):
                os.unlink(custom_path)

    def test_handles_cover_image(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        info_with_cover = {
            "issue_title": "《财新周刊》2026年第26期",
            "cover_data": b"fake-cover-image-bytes",
        }
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name

        try:
            with patch.object(console, "print"):
                create_epub(
                    sample_articles, info_with_cover, sample_image_manager, out_path
                )

            assert os.path.exists(out_path)
            assert os.path.getsize(out_path) > 0
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_empty_articles_creates_minimal_epub(
        self, sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name

        try:
            with patch.object(console, "print"):
                create_epub([], sample_info, sample_image_manager, out_path)

            assert os.path.exists(out_path)
            assert os.path.getsize(out_path) > 0
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_every_document_links_the_stylesheet(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        """回归：每个内容文档都必须引用样式表。

        ebooklib 的 EpubHtml.get_content() 会用 book 模板重建文档 —— 只取 content
        的 <body> 子节点，<head> 整个丢弃。因此把 <link> 写在 content 里是无效的，
        必须走 add_link()。曾因此导致全书 CSS 从未生效（图片不受约束、文末标志
        按原始尺寸渲染）。
        """
        info = {"issue_title": "《财新周刊》2026年第39期", "cover_data": b"fake-cover"}
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name

        try:
            with patch.object(console, "print"):
                create_epub(sample_articles, info, sample_image_manager, out_path)

            with zipfile.ZipFile(out_path) as z:
                names = z.namelist()
                assert "EPUB/style/default.css" in names, "样式表未被打包"

                docs = [n for n in names if re.search(r"EPUB/(chapter_\d+|contents|cover_page)\.xhtml$", n)]
                assert docs, "未找到任何内容文档"
                for n in docs:
                    head = z.read(n).decode("utf-8").split("</head>")[0]
                    assert 'href="style/default.css"' in head, f"{n} 的 <head> 未引用样式表"
                    assert 'rel="stylesheet"' in head, f"{n} 的 link 缺少 rel=stylesheet"
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)


class TestTypographyStructure:
    """回归：版心包装层、封面满幅、目录编号、导航非线性、OPF 元数据、CJK 排版规则。

    这些改动都对应过真实的排版缺陷，不能再退化回去。
    """

    @staticmethod
    def _build(articles, info, image_manager) -> str:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name
        with patch.object(console, "print"):
            create_epub(articles, info, image_manager, out_path)
        return out_path

    @staticmethod
    def _read(path: str, member: str) -> str:
        with zipfile.ZipFile(path) as z:
            return z.read(member).decode("utf-8")

    def test_article_body_wrapped_in_page_not_body_padding(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """左右内边距必须挂在 .page 上 —— 挂在 body 上会给封面也留白边。"""
        out = self._build(sample_articles, sample_info, sample_image_manager)
        try:
            chapter = self._read(out, "EPUB/chapter_0.xhtml")
            assert 'class="page"' in chapter
            css = self._read(out, "EPUB/style/default.css")
            page_rule = re.search(r"\n\s*\.page \{(.*?)\}", css, re.S).group(1)
            # 先给百分比兜底，再用 max() 覆盖 —— 旧引擎不认 max() 时仍有 6%
            assert "padding: 0 6%" in page_rule
            assert "padding: 0 max(6%, 1.5em)" in page_rule
            body_rule = re.search(r"\n\s*body \{(.*?)\}", css, re.S).group(1)
            assert "padding: 0;" in body_rule
            assert "0 6%" not in body_rule and "max(" not in body_rule, "版心内边距不该挂在 body 上"
        finally:
            os.unlink(out)

    def test_cover_page_is_full_bleed(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        info = {"issue_title": "《财新周刊》2026年第39期", "cover_data": b"fake-cover"}
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            cover = self._read(out, "EPUB/cover_page.xhtml")
            assert 'class="cover-page"' in cover
            assert 'class="cover-img"' in cover
            # 不应再有内联样式
            assert "style=" not in cover
            css = self._read(out, "EPUB/style/default.css")
            assert ".cover-img" in css and "max-height: 100vh" in css
        finally:
            os.unlink(out)

    def test_toc_items_are_numbered_and_labelled(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        out = self._build(sample_articles, sample_info, sample_image_manager)
        try:
            toc = self._read(out, "EPUB/contents.xhtml")
            assert 'class="toc-index">01</span>' in toc
            assert 'class="toc-index">02</span>' in toc
            assert 'class="toc-meta"' in toc
            assert "共 2 篇" in toc
        finally:
            os.unlink(out)

    def test_chapter_titles_have_no_template_braces(
        self, sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """标题里的 {{ }} 残片曾漏进 nav.xhtml 与阅读器目录。"""
        articles = [
            {"title": "{{周刊导播｜抢占办公室", "html": "<div class='content-body'><p>正文</p></div>"},
            {"title": "正常标题}}", "html": "<div class='content-body'><p>正文</p></div>"},
        ]
        out = self._build(articles, sample_info, sample_image_manager)
        try:
            nav = self._read(out, "EPUB/nav.xhtml")
            toc = self._read(out, "EPUB/contents.xhtml")
            for doc in (nav, toc):
                assert "{{" not in doc and "}}" not in doc
            assert "周刊导播｜抢占办公室" in nav
        finally:
            os.unlink(out)

    def test_nav_is_non_linear(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """nav 留在 spine 里以保证兼容，但必须 linear="no"，否则翻页会多读一页目录。"""
        out = self._build(sample_articles, sample_info, sample_image_manager)
        try:
            opf = self._read(out, "EPUB/content.opf")
            assert 'idref="nav" linear="no"' in opf
        finally:
            os.unlink(out)

    def test_opf_metadata_is_populated(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        info = {
            "issue_title": "《财新周刊》2026年第39期",
            "topic": "抢占办公室",
            "past_issues": [{"date": "2026年10月12日"}],
        }
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            opf = self._read(out, "EPUB/content.opf")
            assert "<dc:publisher" in opf and "财新周刊" in opf
            # dc:date 必须是 ISO 8601（原先写成「2026年10月12日」是不合规的）
            assert "<dc:date>2026-10-12</dc:date>" in opf
            assert "<dc:description" in opf
            assert "<dc:subject" in opf
        finally:
            os.unlink(out)

    def test_css_contains_cjk_and_table_rules(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        out = self._build(sample_articles, sample_info, sample_image_manager)
        try:
            css = self._read(out, "EPUB/style/default.css")
            # 中文两端对齐 / 断行 / 孤行控制 / 标题不断页
            assert "text-justify: inter-ideograph" in css
            assert "overflow-wrap: break-word" in css
            assert "orphans: 2" in css and "widows: 2" in css
            assert "break-after: avoid" in css
            # 导语与引语（此前 preserved_classes 保留了类名，却没有对应样式）
            assert ".content-body .lead" in css
            assert ".content-body .quote" in css
            # 表格
            assert ".content-body table" in css
            assert ".content-body th" in css
        finally:
            os.unlink(out)

    def test_packaged_stylesheet_matches_source(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """打进去的 CSS 必须就是 style/default.css 当前内容，不能是旧副本。"""
        out = self._build(sample_articles, sample_info, sample_image_manager)
        try:
            packaged = self._read(out, "EPUB/style/default.css")
        finally:
            os.unlink(out)
        assert packaged == STYLE_FILE.read_text(encoding="utf-8")


# ══ em 尺度回归 ══════════════════════════════════════════════════════
# 字号与间距全部用 em（跟随阅读器字号），但折算回 20px 基准后必须仍是 3 的倍数。
# 这层保护很关键：em 会随字号复利，改动任何一处字号都可能让某个间距悄悄跑出网格。

READER_DEFAULT_PX = 16.0   # 阅读器默认字号；body 的 font-size em 相对它
BASE_PX = 20.0             # body 实际字号 = 1.25em × 16px
HAIRLINE_PX = 1            # 发丝线是唯一允许保留的 px

# 这几条规则的「上下文字号」不是 20px：body 的父级是阅读器默认值；
# th/td/caption 的父元素是 font-size 0.75em(=15px) 的 table。
CONTEXT_PX = {
    "body": READER_DEFAULT_PX,
    ".content-body table": 20.0,                    # font-size 的 em 相对 .content-body
    ".content-body th, .content-body td": 15.0,     # 继承 table
    ".content-body caption": 15.0,                  # 继承 table
}

# 注意 text-indent 不在此列：2em 是「两个字符宽」的语义值，不是设计网格上的间距，
# 基准换成 20px 后 2em = 40px（非 3 的倍数）依然是对的。它由
# test_paragraph_indent_is_two_characters 单独锁定。
_SPACING_PROPS = (
    "margin", "padding", "height", "min-width", "border-radius",
    "max-width", "max-height",
    "border", "border-left", "border-right", "border-top", "border-bottom",
)


def _strip_comments(css: str) -> str:
    """去掉注释 —— 文件头的尺度说明里写满了「1.8em = 36px」这类示例，不能参与校验。"""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _css_rules(css: str) -> list[tuple[str, str]]:
    """把样式表拆成 (选择器, 声明块)。只够解析本项目这份手写 CSS 用。"""
    css = _strip_comments(css)
    return [
        (" ".join(m.group(1).split()), m.group(2))
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css)
    ]


def _em_values(decls: str, prop: str) -> list[float]:
    """收集某属性所有 em 值。

    注意要遍历**全部**同名声明 —— 回退写法（如 .page 的
    `padding: 0 6%; padding: 0 max(6%, 1.5em)`）有两处 padding，
    只看第一处会漏掉真正生效的那条。
    """
    values: list[float] = []
    for m in re.finditer(rf"(?:^|;)\s*{prop}:\s*([^;]+)", decls):
        values += [float(x) for x in re.findall(r"([\d.]+)em", m.group(1))]
    return values


class TestDateAndCover:
    """回归：出版日期必须落到 dc:date（ISO 8601），且不能出现两个封面文档。"""

    @staticmethod
    def _build(articles, info, image_manager) -> str:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name
        with patch.object(console, "print"):
            create_epub(articles, info, image_manager, out_path)
        return out_path

    @staticmethod
    def _read(path: str, member: str) -> str:
        with zipfile.ZipFile(path) as z:
            return z.read(member).decode("utf-8")

    def test_dc_date_is_iso8601(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        """EPUB 3 要求 dc:date 是 ISO 8601；写「2026年10月12日」epubcheck 会报 ERROR。"""
        info = {"issue_title": "《财新周刊》2026年第39期", "issue_date": "2026-10-12"}
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            opf = self._read(out, "EPUB/content.opf")
            assert "<dc:date>2026-10-12</dc:date>" in opf
        finally:
            os.unlink(out)

    def test_chinese_date_from_past_issues_is_normalized(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        """历史期刊列表里的日期是「2026年10月12日」，写进 dc:date 前要转 ISO。"""
        info = {
            "issue_title": "《财新周刊》2026年第39期",
            "past_issues": [{"date": "2026年10月12日"}],
        }
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            opf = self._read(out, "EPUB/content.opf")
            assert "<dc:date>2026-10-12</dc:date>" in opf
        finally:
            os.unlink(out)

    def test_issue_date_wins_over_past_issues(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        info = {
            "issue_title": "《财新周刊》2026年第39期",
            "issue_date": "2026-10-12",
            "past_issues": [{"date": "2026年01月01日"}],
        }
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            opf = self._read(out, "EPUB/content.opf")
            assert "<dc:date>2026-10-12</dc:date>" in opf
        finally:
            os.unlink(out)

    def test_skips_past_issues_without_date(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        """历史列表里第一条常常缺日期（置顶的本期条目），要往后找而不是直接放弃。"""
        info = {
            "issue_title": "《财新周刊》2026年第39期",
            "past_issues": [{"id": "cw1226"}, {"date": "2026年10月12日"}],
        }
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            opf = self._read(out, "EPUB/content.opf")
            assert "<dc:date>2026-10-12</dc:date>" in opf
        finally:
            os.unlink(out)

    def test_toc_meta_shows_chinese_date(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        """目录页展示用中文日期，与 dc:date 的 ISO 格式各管一头。"""
        info = {"issue_title": "《财新周刊》2026年第39期", "issue_date": "2026-10-12"}
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            toc = self._read(out, "EPUB/contents.xhtml")
            assert "2026年10月12日" in toc
            assert "2026-10-12" not in toc
        finally:
            os.unlink(out)

    def test_no_duplicate_cover_document(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        """set_cover() 默认 create_page=True 会额外生成一个无样式的 cover.xhtml，
        与自建的 cover_page.xhtml 在 manifest 里并存，有些阅读器会挑中那个。"""
        info = {"issue_title": "《财新周刊》2026年第39期", "cover_data": b"fake-cover"}
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
            assert "EPUB/cover_page.xhtml" in names
            assert "EPUB/cover.xhtml" not in names, "不应再生成第二个封面文档"
            opf = self._read(out, "EPUB/content.opf")
            assert '<item href="cover.xhtml"' not in opf
            assert 'properties="cover-image"' in opf
        finally:
            os.unlink(out)


class TestEmScale:
    """字号与间距用 em，但折算回 20px 基准后仍落在 3 的倍数网格上。"""

    @staticmethod
    def _css(sample_articles, sample_info, sample_image_manager) -> str:
        """直接读样式表源文件。

        CSS 已经从 create_epub 的局部字符串挪到 style/default.css，所以校验
        排版尺度不必先打包一个 EPUB —— 用例快一个数量级。打包是否真的带上了
        这份文件，由 test_packaged_stylesheet_matches_source 单独保证。
        """
        del sample_articles, sample_info, sample_image_manager
        return STYLE_FILE.read_text(encoding="utf-8")

    @staticmethod
    def _own_px(rules: list[tuple[str, str]]) -> dict[str, float]:
        """每条规则里「元素自己的 font-size」折算成 px（无声明则继承上下文字号）。"""
        own = {}
        for sel, decls in rules:
            context = CONTEXT_PX.get(sel, BASE_PX)
            declared = _em_values(decls, "font-size")
            own[sel] = declared[0] * context if declared else context
        return own

    def test_no_absolute_px_except_hairlines(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """既然选了 em 就走到底：除发丝线外，样式表里不该再出现 px。"""
        css = _strip_comments(self._css(sample_articles, sample_info, sample_image_manager))
        offenders = [
            m.group(0)
            for m in re.finditer(r"[\d.]+px", css)
            if float(m.group(0)[:-2]) != HAIRLINE_PX
        ]
        assert not offenders, f"这些值应该改用 em：{offenders}"

    def test_font_sizes_land_on_the_three_grid(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        css = self._css(sample_articles, sample_info, sample_image_manager)
        problems = []
        for sel, decls in _css_rules(css):
            for em in _em_values(decls, "font-size"):
                px = em * CONTEXT_PX.get(sel, BASE_PX)
                # 正文基准自身（20px）不在 3 的倍数上 —— 网格约束的是「相对基准的各级字号」
                if round(px) == round(BASE_PX):
                    continue
                if round(px) % 3 != 0:
                    problems.append(f"{sel} {{ font-size: {em}em }} → {px:.2f}px")
        assert not problems, "字号没落在 3 的倍数上：\n" + "\n".join(problems)

    def test_spacing_lands_on_the_three_grid(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """间距的 em 相对元素自身字号，所以要连字号一起算。"""
        css = self._css(sample_articles, sample_info, sample_image_manager)
        rules = _css_rules(css)
        own = self._own_px(rules)
        problems = []
        for sel, decls in rules:
            base = own.get(sel, BASE_PX)
            for prop in _SPACING_PROPS:
                for em in _em_values(decls, prop):
                    px = em * base
                    if round(px) % 3 != 0:
                        problems.append(f"{sel} {{ {prop}: {em}em }} → {px:.2f}px")
        assert not problems, "间距没落在 3 的倍数上：\n" + "\n".join(problems)

    def test_paragraph_indent_is_two_characters(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """首行缩进必须恒为 2em —— 它是「两个字符宽」，不随基准字号进网格。

        基准换成 20px 后 2em = 40px（不是 3 的倍数），这是对的：缩进要跟着字数走，
        不能为了凑网格改成 1.95em（那就不是两个字了）。
        """
        css = self._css(sample_articles, sample_info, sample_image_manager)
        rule = next(d for s, d in _css_rules(css) if s == ".content-body p")
        assert "text-indent: 2em" in rule, f"段落缩进不是 2 个字符：{rule}"

    def test_type_ladder_descends_with_fibonacci_like_steps(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """标题逐级递减：相邻标题比值落在 1.1–2.1，且 h3 仍须大于正文。"""
        css = self._css(sample_articles, sample_info, sample_image_manager)
        ladder: dict[str, float] = {}
        for sel, decls in _css_rules(css):
            for name in (".article-title", ".content-body h2", ".content-body h3"):
                if name in sel:
                    declared = _em_values(decls, "font-size")
                    if declared:
                        ladder[name] = declared[0] * CONTEXT_PX.get(sel, BASE_PX)
        assert len(ladder) == 3, f"没解析到完整的标题阶梯：{ladder}"
        headings = [
            ladder[".article-title"],
            ladder[".content-body h2"],
            ladder[".content-body h3"],
        ]
        for big, small in zip(headings, headings[1:], strict=False):
            assert big > small, f"层级断了：{headings}"
            ratio = big / small
            assert 1.1 <= ratio <= 2.1, f"{big}px → {small}px 的比值 {ratio:.3f} 超出层级区间"
        # h3 与正文的差距可以很小（h3 靠加粗 + 色条区分层级），但方向必须对
        assert headings[-1] > BASE_PX, f"h3 {headings[-1]}px 必须大于正文 {BASE_PX}px"
        assert headings[0] / BASE_PX <= 2.5, f"文章标题 {headings[0]}px 相对正文过大"


# ══ 目录分组 / 章末翻页 / landmarks ═══════════════════════════════════
# 这一组都在 create_epub 里做，因为只有它知道章节序号与总数。此前章末页脚由
# scrape_article 生成，于是「上一篇 / 下一篇」根本无从写起。


class TestNavigationAndToc:
    @staticmethod
    def _build(articles, info, image_manager) -> str:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name
        with patch.object(console, "print"):
            create_epub(articles, info, image_manager, out_path)
        return out_path

    @staticmethod
    def _read(path: str, member: str) -> str:
        with zipfile.ZipFile(path) as z:
            return z.read(member).decode("utf-8")

    @staticmethod
    def _grouped():
        return [
            {"title": "抢占办公室", "section": "封面报道", "html": "<div class='content-body'><p>一</p></div>"},
            {"title": "AI重塑金融", "section": "金融", "html": "<div class='content-body'><p>二</p></div>"},
            {"title": "AMC通道业务再收紧", "section": "金融", "html": "<div class='content-body'><p>三</p></div>"},
            {"title": "必和必拓加注铜未来", "section": "商业", "html": "<div class='content-body'><p>四</p></div>"},
        ]

    def test_toc_is_grouped_by_section_in_page_order(
        self, sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        out = self._build(self._grouped(), sample_info, sample_image_manager)
        try:
            toc = self._read(out, "EPUB/contents.xhtml")
        finally:
            os.unlink(out)
        groups = re.findall(r'toc-group-title">([^<]+)<', toc)
        assert groups == ["封面报道", "金融", "商业"], f"栏目顺序应与版面一致：{groups}"
        # 每个分组内的条目数：封面报道 1、金融 2、商业 1
        blocks = re.findall(r'class="toc-group">(.*?)(?=class="toc-group">|$)', toc, re.S)
        assert [len(re.findall(r'class="toc-item"', b)) for b in blocks] == [1, 2, 1]

    def test_toc_keeps_chapter_numbers_after_grouping(
        self, sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """分组只改展示，编号仍是全局连续序号 —— 否则目录与章节对不上。"""
        out = self._build(self._grouped(), sample_info, sample_image_manager)
        try:
            toc = self._read(out, "EPUB/contents.xhtml")
        finally:
            os.unlink(out)
        # ebooklib 会重新缩进序列化，标签之间会插入换行，所以匹配要容忍空白
        pairs = re.findall(
            r'href="chapter_(\d+)\.xhtml">\s*<div class="toc-item-title">\s*'
            r'<span class="toc-index">(\d+)</span>',
            toc,
        )
        assert pairs == [("0", "01"), ("1", "02"), ("2", "03"), ("3", "04")]

    def test_toc_falls_back_to_flat_list_without_sections(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """首页/手动输入期号拿不到栏目 —— 不能出现空的分组标题，也不能报错。"""
        out = self._build(sample_articles, sample_info, sample_image_manager)
        try:
            toc = self._read(out, "EPUB/contents.xhtml")
        finally:
            os.unlink(out)
        assert 'class="toc-group-title"' not in toc
        assert len(re.findall(r'class="toc-item"', toc)) == len(sample_articles)

    def test_footer_links_prev_and_next(
        self, sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        articles = self._grouped()
        out = self._build(articles, sample_info, sample_image_manager)
        try:
            first = self._read(out, "EPUB/chapter_0.xhtml")
            middle = self._read(out, "EPUB/chapter_1.xhtml")
            last = self._read(out, "EPUB/chapter_3.xhtml")
        finally:
            os.unlink(out)
        # 首篇没有「上一篇」、末篇没有「下一篇」，避免死链
        assert "pager-prev" not in first
        assert 'href="chapter_1.xhtml">下一篇' in first
        assert 'href="chapter_0.xhtml">← 上一篇' in middle
        assert 'href="chapter_2.xhtml">下一篇' in middle
        assert "pager-next" not in last
        assert 'href="chapter_2.xhtml">← 上一篇' in last
        # 每篇都有回目录入口
        for doc in (first, middle, last):
            assert 'class="back-to-toc"' in doc

    def test_nav_has_landmarks(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """EPUB 3 的「导览」菜单靠 landmarks 定位封面/目录/正文起点。"""
        info = dict(sample_info, cover_data=b"fake-cover")
        out = self._build(sample_articles, info, sample_image_manager)
        try:
            nav = self._read(out, "EPUB/nav.xhtml")
            opf = self._read(out, "EPUB/content.opf")
        finally:
            os.unlink(out)
        assert 'epub:type="landmarks"' in nav
        assert re.search(r'epub:type="cover"[^>]*href="cover_page\.xhtml"', nav)
        assert re.search(r'epub:type="toc"[^>]*href="contents\.xhtml"', nav)
        assert re.search(r'epub:type="bodymatter"[^>]*href="chapter_0\.xhtml"', nav)
        # 别再写 EPUB 2 那套已废弃的 <guide>
        assert "<guide" not in opf

    def test_page_has_line_length_cap(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        """宽屏（iPad 横屏）下不设行长上限，一行能到 70 字，读起来会串行。"""
        css = STYLE_FILE.read_text(encoding="utf-8")
        rule = next(d for s, d in _css_rules(css) if s == ".page")
        widths = _em_values(rule, "max-width")
        assert widths, "版心缺少 max-width 上限"
        assert 20 <= widths[0] * BASE_PX <= 800, f"行长上限不合理：{widths[0] * BASE_PX}px"
        assert "margin: 0 auto" in rule, "设了上限就必须居中，否则正文挤在左边"
