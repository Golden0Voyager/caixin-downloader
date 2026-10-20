"""抓取层的小工具测试：文件名转义 + 资源拦截路由。

两条都出过真实问题：
1. 标题直接拼进文件路径，含 `/` 时会写到别的目录或直接失败；
2. 图片拦截拆成两条 Playwright 路由，而后注册的 `**/*` 永远先命中，
   先注册的那条成了死代码（实测只触发后者）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from caixin.cookies import load_cookies  # noqa: E402
from caixin.fetcher import _route_request  # noqa: E402
from caixin.text import safe_filename  # noqa: E402


class TestLoadCookies:
    """main.py 与 test_caixin.py 共用同一份解析逻辑，这里锁住转换规则。"""

    @staticmethod
    def _write(records) -> str:
        import json
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f)
        return path

    def test_keeps_only_playwright_fields(self) -> None:
        import os

        path = self._write([
            {
                "name": "ADVC", "value": "x", "domain": ".caixin.com", "path": "/",
                "expirationDate": 1778418961, "hostOnly": False, "httpOnly": False,
            }
        ])
        try:
            cookies = load_cookies(path)
        finally:
            os.unlink(path)
        assert cookies == [
            {"name": "ADVC", "value": "x", "domain": ".caixin.com", "path": "/"}
        ], "expirationDate 等字段应被丢掉，让 Playwright 按 session cookie 处理"

    def test_keeps_valid_samesite_only(self) -> None:
        import os

        path = self._write([
            {"name": "a", "value": "1", "domain": "x", "sameSite": "Lax"},
            {"name": "b", "value": "2", "domain": "x", "sameSite": "unspecified"},
            {"name": "c", "value": "3", "domain": "x"},
        ])
        try:
            cookies = load_cookies(path)
        finally:
            os.unlink(path)
        assert cookies[0]["sameSite"] == "Lax"
        assert "sameSite" not in cookies[1]
        assert "sameSite" not in cookies[2]

    def test_defaults_path_to_root(self) -> None:
        import os

        path = self._write([{"name": "a", "value": "1", "domain": "x"}])
        try:
            cookies = load_cookies(path)
        finally:
            os.unlink(path)
        assert cookies[0]["path"] == "/"


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("抢占办公室", "抢占办公室"),
            ("A/B 测试", "A_B 测试"),
            ("比例 1:2", "比例 1_2"),
            ("why?|what*", "why__what_"),
            ('引号"与<尖括号>', "引号_与_尖括号_"),
        ],
    )
    def test_replaces_illegal_characters(self, raw: str, expected: str) -> None:
        assert safe_filename(raw) == expected

    def test_keeps_chinese_title_intact(self) -> None:
        title = "封面报道｜抢占办公室、新手机涨价、重估华为汽车"
        assert safe_filename(title) == title

    def test_strips_surrounding_dots_and_spaces(self) -> None:
        assert safe_filename("  ..标题..  ") == "标题"

    def test_truncates_long_names(self) -> None:
        assert len(safe_filename("长" * 200)) == 80

    @pytest.mark.parametrize("raw", ["", None, "   ", "///"])
    def test_falls_back_when_nothing_usable(self, raw) -> None:
        assert safe_filename(raw, fallback="未命名") == "未命名"


class _FakeRoute:
    """最小化的 Playwright Route 替身，只记录被要求做了什么。"""

    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.action: str | None = None

    async def abort(self) -> None:
        self.action = "abort"

    async def continue_(self) -> None:
        self.action = "continue"


async def _decide(url: str) -> str:
    route = _FakeRoute(url)
    await _route_request(route)
    return route.action


class TestRouteRequest:
    @pytest.mark.parametrize(
        "url",
        [
            "https://analytics.example.com/a.js",
            "https://adserver.caixin.com/banner",
            "https://fonts.gstatic.com/x.woff2",
            "https://hm.baidu.com/hm.js",
        ],
    )
    def test_aborts_tracking_and_fonts(self, url: str) -> None:
        assert asyncio.run(_decide(url)) == "abort"

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example.com/photo.jpg",
            "https://other.site/cover.PNG?x=1",
        ],
    )
    def test_aborts_offsite_images(self, url: str) -> None:
        assert asyncio.run(_decide(url)) == "abort"

    @pytest.mark.parametrize(
        "url",
        [
            "https://img.caixin.com/2026-10-10/x.jpg",
            "https://datanews.caixin.com/a/editorIcon.png",
            "https://weekly.caixin.com/2026/cw1226/",
        ],
    )
    def test_allows_caixin_assets_and_pages(self, url: str) -> None:
        assert asyncio.run(_decide(url)) == "continue"


# ══ 期页栏目抽取 ══════════════════════════════════════════════════════
# 财新期页把文章放在 `<div class="magIntrotit">金融 Finance</div>` 之后的一组
# `<dl>` 里：栏目名只出现一次，作用于其后所有链接。所以要顺序遍历、边走边记，
# 一旦改成「先找所有标题、再找所有链接」就会丢掉这层对应关系。

ISSUE_FRAGMENT = """
<div class="main">
  <div class="report">
    <div class="magazine-container">
      <div class="reportTit">封面报道 Cover Story</div>
      <dl><dt><a href="https://weekly.caixin.com/2026-10-09/1.html">抢占办公室</a></dt>
          <dd>字节跳动、腾讯、阿里巴巴争夺全流程日常办公应用场景</dd></dl>
    </div>
  </div>
  <div class="magContent2">
    <div class="magIntrotit">金融 Finance</div>
    <dl><dt><a href="https://weekly.caixin.com/2026-10-10/2.html">财新周刊｜AI重塑金融</a></dt></dl>
    <dl><dt><a href="https://weekly.caixin.com/2026-10-10/3.html">专栏｜不断深化的滞胀格局</a></dt></dl>
    <div class="magIntrotit">商业 Business</div>
    <dl><dt><a href="https://weekly.caixin.com/2026-10-10/4.html">财新周刊｜必和必拓加注铜未来</a></dt></dl>
  </div>
  <div class="sidebar">
    <a href="https://weekly.caixin.com/2026/cw1226/">第1226期</a>
    <a href="https://other.example.com/x.html">站外链接</a>
  </div>
</div>
"""


class TestExtractArticleLinks:
    @staticmethod
    def _links():
        from bs4 import BeautifulSoup

        from caixin.fetcher import extract_article_links

        soup = BeautifulSoup(ISSUE_FRAGMENT, "html.parser")
        return extract_article_links(soup.select_one(".main"))

    def test_assigns_each_article_to_its_section(self) -> None:
        assert [(link["title"], link["section"]) for link in self._links()] == [
            ("抢占办公室", "封面报道"),
            ("AI重塑金融", "金融"),
            ("专栏｜不断深化的滞胀格局", "金融"),
            ("必和必拓加注铜未来", "商业"),
        ]

    def test_keeps_document_order(self) -> None:
        """章节顺序 = 版面阅读顺序，栏目分组只影响目录展示，不能重排文章。"""
        assert [link["url"].rsplit("/", 1)[-1] for link in self._links()] == [
            "1.html", "2.html", "3.html", "4.html",
        ]

    def test_skips_non_article_and_offsite_links(self) -> None:
        urls = [link["url"] for link in self._links()]
        assert not any("cw1226" in u for u in urls), "期号导航链接不是文章"
        assert not any("other.example.com" in u for u in urls), "站外链接不收"

    def test_section_is_empty_without_headings(self) -> None:
        """首页没有栏目标题 —— 目录页要能退回平铺列表，而不是报错。"""
        from bs4 import BeautifulSoup

        from caixin.fetcher import extract_article_links

        soup = BeautifulSoup(
            '<div><a href="https://weekly.caixin.com/2026-10-10/9.html">某篇文章</a></div>',
            "html.parser",
        )
        links = extract_article_links(soup.div)
        assert links == [
            {"title": "某篇文章", "url": "https://weekly.caixin.com/2026-10-10/9.html", "section": ""}
        ]

    def test_drops_duplicate_urls(self) -> None:
        """同一篇文章在页面上常出现两次（标题 + 图片链接），只能收一次。

        回归点：不去重的话，目录里会出现两个同名章节，正文被抓两遍。
        """
        from bs4 import BeautifulSoup

        from caixin.fetcher import extract_article_links

        soup = BeautifulSoup(
            '<div><a href="https://weekly.caixin.com/2026-10-10/9.html">某篇文章</a>'
            '<a href="https://weekly.caixin.com/2026-10-10/9.html">某篇文章</a></div>',
            "html.parser",
        )
        assert len(extract_article_links(soup.div)) == 1

    def test_drops_empty_and_issue_number_titles(self) -> None:
        """标题只有一两个字、或写着「第N期」的，都是导航而不是文章。"""
        from bs4 import BeautifulSoup

        from caixin.fetcher import extract_article_links

        soup = BeautifulSoup(
            '<div>'
            '<a href="https://weekly.caixin.com/2026-10-10/7.html">图</a>'
            '<a href="https://weekly.caixin.com/2026-10-10/8.html">本期期号</a>'
            '<a href="https://weekly.caixin.com/2026-10-10/9.html">真正的文章</a>'
            '</div>',
            "html.parser",
        )
        assert [link["url"].rsplit("/", 1)[-1] for link in extract_article_links(soup.div)] == ["9.html"]
