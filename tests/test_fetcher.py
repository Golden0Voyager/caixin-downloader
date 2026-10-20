"""抓取层测试：`get_toc` 的解析 + `scrape_article` 的正文处理。

这两条链路以前完全没有测试 —— 它们每一步都要碰 Playwright，所以一直被当成
「没法单测」。实际上它们只用到 page 的十来个方法，用 `tests/fakes.py` 里的替身
就能在完全离线的环境里跑通，既不需要浏览器也不需要 Cookie。

真正要锁住的是**解析规则**：日期怎么归一、栏目怎么分组、API 与 DOM 怎么取长、
图片怎么归类。这些规则每一条都出过真实问题。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fakes import (
    ARTICLE_HTML,
    ARTICLE_HTML_EMPTY,
    HOME_HTML,
    HOME_HTML_WITHOUT_ISSUE,
    ISSUE_HTML,
    FakeContext,
    FakePage,
    FakeResponse,
    make_jsonp,
    make_jsonp_wrapped,
)

from caixin.config import MIN_ARTICLE_CHARS, console
from caixin.fetcher import get_toc, safe_goto, scrape_article

TEMP_ISSUE_HTML = """
<html><body>
  <div class="source">《财新周刊》2026年第39期　出版日期：2026-10-12</div>
</body></html>
"""

API_URL = "https://api.caixin.com/checkAuthByIdJsonp?type=0"


class FakeImageManager:
    """只实现 `download()` 与 `images` 两个成员 —— `scrape_article` 只用到这些。"""

    def __init__(self, entries=None):
        self.images = dict(entries or {})
        self.requested: list[str] = []

    async def download(self, url, retries=2):
        self.requested.append(url)
        return self.images.get(url, {}).get("filename")


def _image_entry(url, filename="images/a.jpg", is_marker=False):
    return {url: {"id": "img_x", "filename": filename, "mime": "image/jpeg",
                  "content": b"x", "is_marker": is_marker}}


class _BrokenResponse(FakeResponse):
    """读取响应体时抛异常 —— 模拟连接在传输中途被掐断。"""

    async def text(self):
        raise RuntimeError("connection reset by peer")


# ══ get_toc ══════════════════════════════════════════════════════════


class TestGetToc:
    @pytest.mark.asyncio
    async def test_extracts_issue_metadata_from_home(self) -> None:
        info = await get_toc(FakePage(html=HOME_HTML))
        assert info["issue_no"] == "39"
        assert "2026年第39期" in info["issue_title"]
        # 期页写的是「出版日期：2026-10-12」，历史列表写「2026年10月05日」——
        # 两种都要认，并且内部统一成 ISO（dc:date 的硬性要求）
        assert info["issue_date"] == "2026-10-12"
        assert info["cover_url"] == "https://img.caixin.com/cover.jpg"

    @pytest.mark.asyncio
    async def test_assigns_sections_in_page_order(self) -> None:
        info = await get_toc(FakePage(html=HOME_HTML))
        assert [(link["title"], link["section"]) for link in info["links"]] == [
            ("抢占办公室", "封面报道"),
            ("AI重塑金融", "金融"),
            ("专栏｜不断深化的滞胀格局", "金融"),
            ("新手机在涨价", "商业"),
        ]

    @pytest.mark.asyncio
    async def test_parses_past_issues(self) -> None:
        info = await get_toc(FakePage(html=HOME_HTML))
        past = {p["id"]: p for p in info["past_issues"]}
        assert past["cw1225"]["date"] == "2026-10-05"
        assert past["cw1225"]["no"] == "38"

    @pytest.mark.asyncio
    async def test_inserts_current_issue_when_absent_from_past_list(self) -> None:
        """当前期常常不在历史列表里 —— 要置顶，否则下拉菜单看不到本期。"""
        info = await get_toc(FakePage(html=HOME_HTML))
        assert info["past_issues"][0]["id"] == "cw1226"
        assert info["past_issues"][0]["no"] == "39"

    @pytest.mark.asyncio
    async def test_replaces_generic_issue_title_with_cover_topic(self) -> None:
        """「《财新周刊》2026年第39期」这种泛化刊名要换成封面主题，才认得是哪一期。"""
        info = await get_toc(FakePage(html=HOME_HTML))
        assert info["past_issues"][0]["title"] == "抢占办公室"
        assert info["topic"] == "抢占办公室"

    @pytest.mark.asyncio
    async def test_issue_page_has_no_cover_or_history(self) -> None:
        """真实期页只有 `.main`：没有 .mi、没有 .xsjCon。"""
        page = FakePage(html=ISSUE_HTML)
        info = await get_toc(page, "https://weekly.caixin.com/2026/cw1226/")
        assert page.visited == ["https://weekly.caixin.com/2026/cw1226/"]
        assert info["cover_url"] is None
        assert info["past_issues"] == []
        assert info["issue_date"] == "2026-10-12"
        assert [link["section"] for link in info["links"]] == ["封面报道", "金融"]

    @pytest.mark.asyncio
    async def test_unknown_issue_number_is_stolen_from_issue_page(self) -> None:
        """首页没写期号时，会新开一个临时页面去期页上「偷」准确信息。"""
        ctx = FakeContext(pages=[FakePage(html=TEMP_ISSUE_HTML)])
        page = FakePage(html=HOME_HTML_WITHOUT_ISSUE, context=ctx)
        info = await get_toc(page)
        assert ctx.created == 1, "应当开一个临时页面"
        assert info["issue_no"] == "39"
        assert info["issue_date"] == "2026-10-12"

    @pytest.mark.asyncio
    async def test_date_falls_back_to_today_when_page_has_none(self) -> None:
        from datetime import datetime

        info = await get_toc(FakePage(html="<html><body>空页面</body></html>"))
        assert info["issue_date"] == datetime.now().strftime("%Y-%m-%d")

    @pytest.mark.asyncio
    async def test_no_links_is_not_an_error(self) -> None:
        info = await get_toc(FakePage(html="<html><body>空页面</body></html>"))
        assert info["links"] == []
        assert info["topic"] == ""

    @pytest.mark.asyncio
    async def test_dedupes_links_appearing_in_two_containers(self) -> None:
        """同一篇文章同时出现在 `.lf` 和 `.main` 里时只能收一次。"""
        html = """
        <html><body>
          <div class="lf"><a href="https://weekly.caixin.com/2026-10-10/1.html">抢占办公室</a></div>
          <div class="main"><a href="https://weekly.caixin.com/2026-10-10/1.html">抢占办公室</a></div>
        </body></html>
        """
        info = await get_toc(FakePage(html=html))
        assert len(info["links"]) == 1


# ══ safe_goto ════════════════════════════════════════════════════════


class TestSafeGoto:
    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self, monkeypatch) -> None:
        monkeypatch.setattr("caixin.fetcher.asyncio.sleep", _no_sleep)
        page = FakePage()
        calls = {"n": 0}
        original = page.goto

        async def flaky(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("net::ERR_TIMED_OUT")
            return await original(url, **kwargs)

        page.goto = flaky
        await safe_goto(page, "https://example.com/")
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_raises_after_last_retry(self, monkeypatch) -> None:
        monkeypatch.setattr("caixin.fetcher.asyncio.sleep", _no_sleep)
        page = FakePage(goto_raises=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await safe_goto(page, "https://example.com/", retries=2)


async def _no_sleep(*args, **kwargs):
    return None


# ══ scrape_article ═══════════════════════════════════════════════════


def _run(page, url="https://weekly.caixin.com/2026-10-09/1.html", mgr=None, **kwargs):
    return scrape_article(page, url, mgr or FakeImageManager(), **kwargs)


class TestScrapeArticleBasics:
    @pytest.mark.asyncio
    async def test_extracts_title_author_and_body(self) -> None:
        art = await _run(FakePage(html=ARTICLE_HTML))
        assert art is not None
        assert "AI 时代的就业变局" in art["title"]
        assert art["empty"] is False
        assert "第一段正文" in art["html"]
        assert "author-bar" in art["html"] and "徐路易" in art["html"]

    @pytest.mark.asyncio
    async def test_strips_unpaired_template_braces_from_title(self) -> None:
        """财新的 `{{` 常常不成对，只删括号字符、保留中间文字。"""
        art = await _run(FakePage(html=ARTICLE_HTML))
        assert "{{" not in art["title"]
        assert art["title"].startswith("深度报道")

    @pytest.mark.asyncio
    async def test_falls_back_to_default_title(self) -> None:
        html = '<html><body><div id="Main_Content_Val"><p>' + "正文内容。" * 20 + "</p></div></body></html>"
        art = await _run(FakePage(html=html), default_title="目录里的标题")
        assert art["title"] == "目录里的标题"

    @pytest.mark.asyncio
    async def test_returns_none_without_content_container(self) -> None:
        art = await _run(FakePage(html="<html><body><div>没有正文容器</div></body></html>"))
        assert art is None

    @pytest.mark.asyncio
    async def test_exception_is_swallowed_and_returns_none(self, monkeypatch) -> None:
        """抓取异常只记一行日志，不能让整期任务崩掉。"""
        monkeypatch.setattr("caixin.fetcher.asyncio.sleep", _no_sleep)
        with patch.object(console, "log"):
            art = await _run(FakePage(goto_raises=RuntimeError("boom")))
        assert art is None

    @pytest.mark.asyncio
    async def test_removes_blacklisted_blocks(self) -> None:
        art = await _run(FakePage(html=ARTICLE_HTML))
        assert "related-articles" not in art["html"]
        assert "相关阅读" not in art["html"]

    @pytest.mark.asyncio
    async def test_cleans_root_attributes_and_inline_background(self) -> None:
        """根节点带着 62KB 内联 base64 背景，`find_all` 不包含自身，必须单独清。"""
        art = await _run(FakePage(html=ARTICLE_HTML))
        assert "Main_Content_Val" not in art["html"]
        assert "base64" not in art["html"]

    @pytest.mark.asyncio
    async def test_strips_hardcoded_indent(self) -> None:
        """段首两个全角空格 + CSS text-indent 会变成缩进 4 字。"""
        art = await _run(FakePage(html=ARTICLE_HTML))
        assert "　　" not in art["html"].split("content-body", 1)[1]

    @pytest.mark.asyncio
    async def test_marks_byline_paragraph(self) -> None:
        html = (
            '<html><body><div id="Main_Content_Val">'
            "<p>文｜财新周刊 徐路易</p><p>" + "正文内容，足够长的段落。" * 6 + "</p></div></body></html>"
        )
        art = await _run(FakePage(html=html))
        assert 'class="article-byline"' in art["html"]

    @pytest.mark.asyncio
    async def test_preserves_inline_anchor_href(self) -> None:
        """正文里的站内链接要留着 —— 属性清洗会重写整个 attrs，href 得显式搬过去。"""
        html = (
            '<html><body><div id="Main_Content_Val">'
            "<p>" + "正文内容，足够长的段落。" * 6 + "</p>"
            '<p>详见<a href="https://weekly.caixin.com/2026-10-10/1.html">这篇报道</a>。</p>'
            "</div></body></html>"
        )
        art = await _run(FakePage(html=html))
        assert 'href="https://weekly.caixin.com/2026-10-10/1.html"' in art["html"]
        assert "这篇报道" in art["html"]


class TestScrapeArticleApiMerge:
    @pytest.mark.asyncio
    async def test_uses_api_content_when_longer_than_dom(self) -> None:
        api_body = "<p>" + "API 里的完整正文。" * 40 + "</p>"
        page = FakePage(html=ARTICLE_HTML, responses=[(API_URL, make_jsonp(api_body))])
        art = await _run(page)
        assert "API 里的完整正文" in art["html"]

    @pytest.mark.asyncio
    async def test_cumulative_api_responses_are_not_duplicated(self) -> None:
        """「余下全文」后的第二次响应是「第 1 页 + 第 2 页」，直接拼会把第 1 页重复一遍。"""
        first = "<p>" + "第一页正文。" * 30 + "</p>"
        second = first + "<p>" + "第二页正文。" * 30 + "</p>"
        page = FakePage(
            html=ARTICLE_HTML,
            responses=[(API_URL, make_jsonp(first)), (API_URL, make_jsonp(second))],
        )
        art = await _run(page)
        assert art["html"].count("第一页正文") == 30, "第 1 页被拼了两遍"
        assert "第二页正文" in art["html"]

    @pytest.mark.asyncio
    async def test_jsonp_wrapper_is_unwrapped(self) -> None:
        api_body = "<p>" + "JSONP 包裹的正文。" * 40 + "</p>"
        page = FakePage(html=ARTICLE_HTML, responses=[(API_URL, make_jsonp_wrapped(api_body))])
        art = await _run(page)
        assert "JSONP 包裹的正文" in art["html"]

    @pytest.mark.asyncio
    async def test_non_zero_code_marks_auth_failed(self) -> None:
        """code != 0 表示 Cookie 失效，此时应当告警而不是静默给个摘要。"""
        page = FakePage(
            html="<html><body><div id='Main_Content_Val'><p>只有摘要。</p></div></body></html>",
            responses=[(API_URL, make_jsonp("", code=403))],
        )
        with patch.object(console, "log") as log:
            art = await _run(page)
        assert art is not None
        assert any("Cookie" in str(call) for call in log.call_args_list)

    @pytest.mark.asyncio
    async def test_malformed_api_payload_is_ignored(self) -> None:
        page = FakePage(html=ARTICLE_HTML, responses=[(API_URL, "not json at all")])
        art = await _run(page)
        assert "第一段正文" in art["html"]

    @pytest.mark.asyncio
    async def test_uses_api_content_when_dom_container_missing(self) -> None:
        """正文容器被改成新类名时（DOM 认不出来），只要 API 有货就还能救回来。"""
        api_body = "<p>" + "只在 API 里存在的正文。" * 20 + "</p>"
        page = FakePage(
            html="<html><body><h1>标题</h1><div class='brand-new-container'><p>壳</p></div></body></html>",
            responses=[(API_URL, make_jsonp(api_body))],
        )
        art = await _run(page)
        assert art is not None and "只在 API 里存在的正文" in art["html"]

    @pytest.mark.asyncio
    async def test_broken_response_does_not_abort_interception(self) -> None:
        """读响应体时抛异常（连接被掐断）只该丢掉这一条响应，不该让整篇失败。"""
        broken = _BrokenResponse(API_URL)
        page = FakePage(html=ARTICLE_HTML, responses=[(API_URL, broken)])
        art = await _run(page)
        assert art is not None
        assert "第一段正文" in art["html"], "坏响应之后应回落到 DOM"


class TestScrapeArticleFulltext:
    @pytest.mark.asyncio
    async def test_single_page_article_skips_expansion(self) -> None:
        page = FakePage(html=ARTICLE_HTML, fulltext_links=0)
        await _run(page)
        assert page.clicked == 0
        assert len(page.visited) == 1

    @pytest.mark.asyncio
    async def test_click_that_grows_body_is_accepted(self) -> None:
        page = FakePage(html=ARTICLE_HTML, fulltext_links=1,
                        body_text="短", expanded_body_text="长得多得多的正文内容")
        await _run(page)
        assert page.clicked == 1
        assert len(page.visited) == 1, "展开成功就不该重新加载"

    @pytest.mark.asyncio
    async def test_failed_expansion_reloads_once_then_warns(self) -> None:
        """点击静默失败时只抓到第一页 —— 必须重载重试，两次都失败才告警。"""
        page = FakePage(html=ARTICLE_HTML, fulltext_links=1)  # 点了不长
        with patch.object(console, "log") as log:
            await _run(page)
        assert page.clicked == 2, "应当重载后再试一次"
        assert len(page.visited) == 2
        assert any("两次均未展开" in str(call) for call in log.call_args_list)

    @pytest.mark.asyncio
    async def test_locator_error_is_treated_as_single_page(self) -> None:
        page = FakePage(html=ARTICLE_HTML, locator_count_raises=True)
        await _run(page)
        assert page.clicked == 0

    @pytest.mark.asyncio
    async def test_detached_body_before_click_is_treated_as_single_page(self) -> None:
        page = FakePage(html=ARTICLE_HTML, fulltext_links=1, raise_inner_text_calls={1})
        await _run(page)
        assert page.clicked == 0

    @pytest.mark.asyncio
    async def test_detached_body_after_click_counts_as_failure(self) -> None:
        page = FakePage(html=ARTICLE_HTML, fulltext_links=1, raise_inner_text_calls={2})
        with patch.object(console, "log") as log:
            await _run(page)
        assert page.clicked == 2
        assert any("两次均未展开" in str(call) for call in log.call_args_list)


class TestScrapeArticleImages:
    URL = "https://img.caixin.com/a.jpg"

    @pytest.mark.asyncio
    async def test_builds_figure_with_caption_and_alt(self) -> None:
        mgr = FakeImageManager(_image_entry(self.URL))
        art = await _run(FakePage(html=ARTICLE_HTML), mgr=mgr)
        assert mgr.requested == [self.URL], "协议相对 URL 必须先规范化"
        assert 'class="article-figure"' in art["html"]
        assert 'alt="图：财新"' in art["html"]
        assert 'class="article-caption">图：财新<' in art["html"]
        assert "cximg" not in art["html"], "原始容器应被 figure 取代"

    @pytest.mark.asyncio
    async def test_marker_image_gets_empty_alt(self) -> None:
        """装饰图（「财」字标志）是纯设计元素，空 alt 让屏幕阅读器跳过。"""
        mgr = FakeImageManager(_image_entry(self.URL, is_marker=True))
        art = await _run(FakePage(html=ARTICLE_HTML), mgr=mgr)
        assert "article-figure--marker" in art["html"]
        assert 'alt=""' in art["html"]

    @pytest.mark.asyncio
    async def test_failed_image_is_removed_with_its_container(self) -> None:
        art = await _run(FakePage(html=ARTICLE_HTML), mgr=FakeImageManager())
        assert "cximg" not in art["html"]
        assert "article-figure" not in art["html"]

    @pytest.mark.asyncio
    async def test_empty_figure_left_by_failed_image_is_removed(self) -> None:
        """图片下载失败后留下的空 `<figure>` 会撑出空白，必须一并删掉。"""
        html = (
            '<html><body><div id="Main_Content_Val">'
            "<p>" + "正文内容，足够长的段落。" * 6 + "</p>"
            '<figure><img src="https://img.caixin.com/e.jpg"/></figure>'
            "</div></body></html>"
        )
        art = await _run(FakePage(html=html), mgr=FakeImageManager())
        assert "<figure" not in art["html"]

    @pytest.mark.asyncio
    async def test_caption_from_dl_dd_structure(self) -> None:
        html = (
            '<html><body><div id="Main_Content_Val">'
            "<p>" + "正文内容，足够长的段落。" * 6 + "</p>"
            '<dl><dt><img src="https://img.caixin.com/b.jpg"/></dt><dd>图：视觉中国</dd></dl>'
            "</div></body></html>"
        )
        mgr = FakeImageManager(_image_entry("https://img.caixin.com/b.jpg", "images/b.jpg"))
        art = await _run(FakePage(html=html), mgr=mgr)
        assert "图：视觉中国" in art["html"]
        assert "article-caption" in art["html"]

    @pytest.mark.asyncio
    async def test_caption_from_sibling_block(self) -> None:
        html = (
            '<html><body><div id="Main_Content_Val">'
            "<p>" + "正文内容，足够长的段落。" * 6 + "</p>"
            '<p><img src="https://img.caixin.com/c.jpg"/></p>'
            '<p class="imageText">图：财新记者</p>'
            "</div></body></html>"
        )
        mgr = FakeImageManager(_image_entry("https://img.caixin.com/c.jpg", "images/c.jpg"))
        art = await _run(FakePage(html=html), mgr=mgr)
        assert "图：财新记者" in art["html"]

    @pytest.mark.asyncio
    async def test_image_without_caption_gets_generic_alt(self) -> None:
        html = (
            '<html><body><div id="Main_Content_Val">'
            "<p>" + "正文内容，足够长的段落。" * 6 + "</p>"
            '<img src="https://img.caixin.com/d.jpg"/>'
            "</div></body></html>"
        )
        mgr = FakeImageManager(_image_entry("https://img.caixin.com/d.jpg", "images/d.jpg"))
        art = await _run(FakePage(html=html), mgr=mgr)
        assert 'alt="配图"' in art["html"]


class TestScrapeArticleCompleteness:
    @pytest.mark.asyncio
    async def test_truncates_after_end_marker(self) -> None:
        html = (
            '<html><body><div id="Main_Content_Val">'
            "<p>" + "正文内容，足够长的段落。" * 6 + "</p>"
            "<p>相关阅读</p><p>另一篇文章的标题</p>"
            "</div></body></html>"
        )
        art = await _run(FakePage(html=html))
        assert "另一篇文章的标题" not in art["html"]
        assert "正文内容" in art["html"]

    @pytest.mark.asyncio
    async def test_short_text_without_image_is_flagged_empty(self) -> None:
        art = await _run(FakePage(html=ARTICLE_HTML_EMPTY))
        assert art["empty"] is True

    @pytest.mark.asyncio
    async def test_short_text_with_image_is_kept_as_gallery(self) -> None:
        """「显影」「天眼」这类图集本来就没多少字，不能当空章丢掉。"""
        html = (
            '<html><body><div id="Main_Content_Val"><p>图集说明。</p>'
            '<img src="https://img.caixin.com/e.jpg"/></div></body></html>'
        )
        mgr = FakeImageManager(_image_entry("https://img.caixin.com/e.jpg", "images/e.jpg"))
        art = await _run(FakePage(html=html), mgr=mgr)
        assert art["empty"] is False

    @pytest.mark.asyncio
    async def test_warns_when_body_still_contains_paywall_hint(self) -> None:
        html = (
            '<html><body><div id="Main_Content_Val">'
            "<p>" + "正文内容，足够长的段落。" * 6 + "</p><p>余下全文</p>"
            "</div></body></html>"
        )
        with patch.object(console, "log") as log:
            await _run(FakePage(html=html))
        assert any("可能不完整" in str(call) for call in log.call_args_list)

    @pytest.mark.asyncio
    async def test_min_article_chars_threshold_is_used(self) -> None:
        """阈值是个常量，别在代码里又写死一个 80。"""
        body = "字" * (MIN_ARTICLE_CHARS - 1)
        html = f'<html><body><div id="Main_Content_Val"><p>{body}</p></div></body></html>'
        art = await _run(FakePage(html=html))
        assert art["empty"] is True

        body = "字" * (MIN_ARTICLE_CHARS + 1)
        html = f'<html><body><div id="Main_Content_Val"><p>{body}</p></div></body></html>'
        art = await _run(FakePage(html=html))
        assert art["empty"] is False


class TestScrapeArticleSection:
    """栏目要透传，标题要剥掉与栏目重复的前缀。

    这是 2026-10-17 实跑暴露的断链：期页的 `extract_article_links` 抽对了栏目，
    但 `scrape_article` 既不接收也不返回 `section`，于是 `group_by_section` 永远
    只看到一个空栏目，目录页退化成 27 条平铺 —— 单元测试用自带 `section` 的
    夹具，所以没照出来。
    """

    def _page(self, title: str) -> FakePage:
        body = "正文内容，足够长的段落。" * 8
        html = (
            '<html><body><h1 class="wiv-title">' + title + "</h1>"
            f'<div id="Main_Content_Val"><p>{body}</p></div></body></html>'
        )
        return FakePage(html=html)

    @pytest.mark.asyncio
    async def test_section_is_returned_untouched(self) -> None:
        art = await _run(self._page("抢占办公室"), section="封面报道")
        assert art["section"] == "封面报道"

    @pytest.mark.asyncio
    async def test_section_prefix_is_stripped_from_title(self) -> None:
        art = await _run(self._page("封面报道｜抢占办公室"), section="封面报道")
        assert art["title"] == "抢占办公室"

    @pytest.mark.asyncio
    async def test_genre_marker_is_kept(self) -> None:
        """「专栏｜」是体裁标记不是栏目前缀，不能跟着栏目名一起删。"""
        art = await _run(self._page("专栏｜不断深化的滞胀格局"), section="金融")
        assert art["title"] == "专栏｜不断深化的滞胀格局"

    @pytest.mark.asyncio
    async def test_without_section_title_is_left_alone(self) -> None:
        """首页/手动输入期号时没有栏目信息，标题原样保留，不能误删前缀。"""
        art = await _run(self._page("封面报道｜抢占办公室"))
        assert art["title"] == "封面报道｜抢占办公室"
        assert art["section"] == ""


class TestFakeResponseContract:
    """替身自身的行为也要对得上真实对象，否则测试会给出假阳性。"""

    @pytest.mark.asyncio
    async def test_response_text_and_body_agree(self) -> None:
        resp = FakeResponse(url="u", body="中文")
        assert await resp.text() == "中文"
        assert await resp.body() == "中文".encode()
