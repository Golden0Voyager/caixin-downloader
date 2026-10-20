"""编排层测试：`process_one_issue` 的单期流水线 + `main()` 的交互分支。

`cli.py` 一直是覆盖率黑洞（0%），原因是它把 `questionary` 和 `playwright`
都写成了函数内延迟导入，看起来只能人工点一遍。其实这两个导入点恰好是**接缝**：
只要往 `sys.modules` 里塞一个替身，整条交互链路就能在离线环境里跑完。

这里锁的是编排逻辑而不是 UI：
- 期页拿不到链接时该早退，而不是打包一本空书；
- 空章要过滤，但过滤后不能悄悄产出 0 字节的 EPUB；
- 「上一页 / 下一页 / 加载更多 / 手动输入」四条导航不能互相抢答；
- 翻页时当前页的勾选要同步进 `selected_tasks`，取消勾选的要从里面移除。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest
from fakes import (
    ARTICLE_HTML,
    ISSUE_HTML,
    FakeAsyncPlaywright,
    FakeBrowser,
    FakeContext,
    FakePage,
    FakeRequest,
    FakeResponse,
)

import caixin.cli as cli
from caixin.config import MAX_ARTICLES_PER_ISSUE

# ══ 夹具与小工具 ══════════════════════════════════════════════════════


def _past(index):
    """构造一条历史期刊记录（字段与 `get_toc` 的返回值一致）。"""
    no = 26 + index
    return {
        "id": f"cw{1200 + index}",
        "date": f"2026-{1 + index % 12:02d}-01",
        "no": str(no),
        "title": f"第{no}期封面报道",
        "url": f"https://weekly.caixin.com/2026/cw{1200 + index}/",
    }


def _issue_info(links=None, **overrides):
    info = {
        "issue_title": "《财新周刊》 2026年第39期",
        "issue_no": "39",
        "issue_date": "2026-10-12",
        "cover_url": None,
        "links": list(links or []),
        "past_issues": [_past(0)],
        "topic": "封面主题",
    }
    info.update(overrides)
    return info


def _link(index):
    return {
        "title": f"文章{index}",
        "url": f"https://weekly.caixin.com/2026-10-10/{index}.html",
        "section": "金融",
    }


def _const(value):
    """异步函数：恒返回 value。"""
    async def _fn(*args, **kwargs):
        return value
    return _fn


def _sequence(*values):
    """异步函数：按顺序返回 values，最后一个会一直复用。"""
    queue = list(values)

    async def _fn(*args, **kwargs):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return _fn


def _article(index, empty=False):
    return {
        "title": f"文章{index}",
        "html": f'<div class="article-header"><h1 class="article-title">文章{index}</h1></div>',
        "url": f"https://weekly.caixin.com/2026-10-10/{index}.html",
        "empty": empty,
    }


@pytest.fixture
def issue_context():
    """一条期的运行环境：期页 + 若干自动生成的抓取页。"""
    return FakeContext(pages=[FakePage(html=ISSUE_HTML)], default_html=ARTICLE_HTML)


@pytest.fixture
def workdir(monkeypatch):
    """临时工作目录（`process_one_issue` 会往 `download/` 里写文件）。

    不用 pytest 的 `tmp_path`：本机沙箱的文件系统钩子会把 pytest 创建
    numbered-dir 时的 EEXIST 改写成 PermissionError，导致夹具直接报错。
    自己 `mkdtemp` 一次就绕开了，顺便保证测试之间互不影响。
    """
    original = os.getcwd()
    path = Path(tempfile.mkdtemp(prefix="caixin-cli-"))
    monkeypatch.chdir(path)
    yield path
    os.chdir(original)
    shutil.rmtree(path, ignore_errors=True)


# ── questionary 替身 ───────────────────────────────────────────────────


class _Answer:
    """`questionary.xxx(...).ask_async()` 的返回值包装。"""

    def __init__(self, value=None, exc=None):
        self.value = value
        self.exc = exc

    async def ask_async(self):
        if self.exc is not None:
            raise self.exc
        return self.value


class _Choice:
    def __init__(self, title="", value=None, checked=False):
        self.title = title
        self.value = value
        self.checked = checked


class _Separator:
    def __init__(self, line=""):
        self.line = line
        self.title = line   # cli 会把所有选项的 .title 收集起来，分隔线也要有


class _Style:
    def __init__(self, rules=()):
        self.rules = list(rules)


class FakeQuestionary(types.ModuleType):
    """`questionary` 模块的替身：按队列依次回答，并记录问了什么。"""

    def __init__(self, confirms=None, checkboxes=None, texts=None):
        super().__init__("questionary")
        self._confirms = list(confirms or [])
        self._checkboxes = list(checkboxes or [])
        self._texts = list(texts or [])
        self.confirm_calls: list[tuple] = []
        self.checkbox_calls: list[tuple[str, list[str]]] = []
        self.text_calls: list[str] = []
        self.Choice = _Choice
        self.Separator = _Separator
        self.Style = _Style

    def _pop(self, queue, name):
        if not queue:
            raise AssertionError(f"questionary.{name}() 被多调用了一次")
        return queue.pop(0)

    def confirm(self, message, default=None):
        self.confirm_calls.append((message, default))
        return self._pop(self._confirms, "confirm")

    def checkbox(self, message, choices, style=None):
        self.checkbox_calls.append((message, [c.title for c in choices]))
        return self._pop(self._checkboxes, "checkbox")

    def text(self, message):
        self.text_calls.append(message)
        return self._pop(self._texts, "text")


class FastAsyncio:
    """`asyncio` 的代理：只把 `sleep` 变空操作，其余原样转发。

    「加载更多」分支里有一句 `await asyncio.sleep(2)`。直接 patch 全局的
    `asyncio.sleep` 会波及事件循环自身，所以这里包一层只改这一个属性。
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def sleep(self, seconds):
        return None


def _install_runtime(monkeypatch, questionary, browser, workdir, cookies=True):
    """把 `main()` 里那两个延迟导入换成替身，并把 Cookie 文件指向临时目录。"""
    monkeypatch.setitem(sys.modules, "questionary", questionary)

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = lambda: FakeAsyncPlaywright(browser)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)

    cookie_path = workdir / "cookies.json"
    if cookies:
        cookie_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli, "COOKIES_FILE", str(cookie_path))
    monkeypatch.setattr(cli, "load_cookies", lambda path: [{"name": "caixin", "value": "x"}])
    monkeypatch.setattr(cli, "asyncio", FastAsyncio(asyncio))


# ══ process_one_issue ════════════════════════════════════════════════


class TestProcessOneIssue:
    async def test_happy_path_writes_epub(self, workdir, monkeypatch, issue_context):
        monkeypatch.chdir(workdir)
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1), _link(2)])))
        monkeypatch.setattr(cli, "scrape_article", _sequence(_article(1), _article(2)))

        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, info, im, fn: calls.append((arts, info, im, fn)))

        selected = {"date": "2026-10-12", "no": "39", "title": "封面主题"}
        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    selected, {"cover_url": None}, issue_context)

        assert len(calls) == 1
        articles, _info, manager, filename = calls[0]
        assert [a["title"] for a in articles] == ["文章1", "文章2"]
        assert filename == "download/2026-10-12 (第39期) - 封面主题.epub"
        assert (workdir / "download").is_dir(), "download 目录应由 process_one_issue 自己建"
        assert manager.__class__.__name__ == "ImageManager"

    async def test_builds_selected_past_from_issue_info(self, workdir, monkeypatch, issue_context):
        """手动输入期号时 `selected_past` 是 None，要用期页上的信息补出来。"""
        monkeypatch.chdir(workdir)
        info = _issue_info([_link(1)], issue_date="2026-10-12", issue_no="39", topic="封面主题")
        monkeypatch.setattr(cli, "get_toc", _const(info))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, i, im, fn: calls.append(fn))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    None, {"cover_url": None}, issue_context)

        assert calls == ["download/2026-10-12 (第39期) - 封面主题.epub"]

    async def test_falls_back_to_issue_title_when_topic_missing(self, workdir, monkeypatch, issue_context):
        monkeypatch.chdir(workdir)
        info = _issue_info([_link(1)], topic="", issue_date="")
        monkeypatch.setattr(cli, "get_toc", _const(info))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, i, im, fn: calls.append(fn))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    None, {"cover_url": None}, issue_context)

        assert "《财新周刊》 2026年第39期" in calls[0]
        assert calls[0].startswith("download/2"), "缺 issue_date 时用当天日期兜底"

    async def test_probes_previous_years_for_manual_issue_id(self, workdir, monkeypatch):
        """期页地址猜错时（年份不对），应逐年往前试直到拿到链接。"""
        context = FakeContext(pages=[FakePage(html=ISSUE_HTML)], default_html=ARTICLE_HTML)
        found = _issue_info([_link(1)])
        seen = []

        async def _get_toc(page, url=None):
            seen.append(url)
            return found if len(seen) >= 3 else _issue_info([])

        monkeypatch.setattr(cli, "get_toc", _get_toc)
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, i, im, fn: calls.append(fn))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    None, {"cover_url": None}, context)

        assert len(seen) == 3, "第 1 次拿不到链接，应再试两个年份才命中"
        assert seen[1] != seen[2], "每次探测都要换年份"
        assert calls, "探测到链接后应当照常打包"

    async def test_probe_gives_up_when_get_toc_raises(self, monkeypatch, issue_context):
        """探测期的某一年报错时要继续试下一年，而不是整体失败。"""
        calls = {"n": 0}

        async def _get_toc(page, url=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _issue_info([])
            if calls["n"] == 2:
                raise RuntimeError("timeout")
            return _issue_info([])

        monkeypatch.setattr(cli, "get_toc", _get_toc)
        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    None, {"cover_url": None}, issue_context)
        assert calls["n"] > 2, "第一次探测抛异常后应继续试更早的年份"

    async def test_no_links_returns_without_building_epub(self, monkeypatch, issue_context):
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([])))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda *a, **k: calls.append(a))

        # selected_past 非空 -> 跳过年份探测，直接走「找不到链接」分支
        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, issue_context)

        assert calls == []

    async def test_warns_when_links_exceed_cap(self, workdir, monkeypatch, issue_context):
        monkeypatch.chdir(workdir)
        links = [_link(i) for i in range(MAX_ARTICLES_PER_ISSUE + 3)]
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info(links)))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(0)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, i, im, fn: calls.append(arts))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, issue_context)

        assert len(calls[0]) == MAX_ARTICLES_PER_ISSUE, "超出的部分应被截断"

    async def test_drops_empty_chapters(self, workdir, monkeypatch, issue_context):
        """空章不入书；但只要有内容，就照常打包。"""
        monkeypatch.chdir(workdir)
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1), _link(2)])))
        monkeypatch.setattr(cli, "scrape_article", _sequence(_article(1), _article(2, empty=True)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, i, im, fn: calls.append(arts))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, issue_context)

        assert [a["title"] for a in calls[0]] == ["文章1"]

    async def test_all_chapters_empty_skips_epub(self, monkeypatch, issue_context):
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1)])))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1, empty=True)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda *a, **k: calls.append(a))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, issue_context)

        assert calls == [], "全空时不该生成 0 字节的 EPUB"

    async def test_downloads_cover_into_info(self, workdir, monkeypatch):
        monkeypatch.chdir(workdir)
        cover = "https://img.caixin.com/cover.jpg"
        context = FakeContext(
            pages=[FakePage(html=ISSUE_HTML)],
            request=_fake_request({cover: FakeResponse(url=cover, body=b"jpeg-bytes")}),
            default_html=ARTICLE_HTML,
        )
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1)], cover_url=cover)))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, info, im, fn: calls.append(info))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, context)

        assert calls[0]["cover_data"] == b"jpeg-bytes"

    async def test_cover_falls_back_to_home_info(self, workdir, monkeypatch):
        monkeypatch.chdir(workdir)
        cover = "https://img.caixin.com/home-cover.jpg"
        context = FakeContext(
            pages=[FakePage(html=ISSUE_HTML)],
            request=_fake_request({cover: FakeResponse(url=cover, body=b"home")}),
            default_html=ARTICLE_HTML,
        )
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1)], cover_url=None)))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, info, im, fn: calls.append(info))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": cover}, context)

        assert calls[0]["cover_data"] == b"home"

    async def test_cover_failure_is_not_fatal(self, workdir, monkeypatch):
        monkeypatch.chdir(workdir)
        cover = "https://img.caixin.com/cover.jpg"
        context = FakeContext(
            pages=[FakePage(html=ISSUE_HTML)],
            request=_fake_request({cover: RuntimeError("connection reset")}),
            default_html=ARTICLE_HTML,
        )
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1)], cover_url=cover)))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, info, im, fn: calls.append(info))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, context)

        assert "cover_data" not in calls[0], "封面失败不该阻塞打包"

    async def test_non_200_cover_is_ignored(self, workdir, monkeypatch):
        monkeypatch.chdir(workdir)
        cover = "https://img.caixin.com/cover.jpg"
        context = FakeContext(
            pages=[FakePage(html=ISSUE_HTML)],
            request=_fake_request({cover: FakeResponse(url=cover, body=b"", status=404)}),
            default_html=ARTICLE_HTML,
        )
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1)], cover_url=cover)))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1)))
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, info, im, fn: calls.append(info))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, context)

        assert "cover_data" not in calls[0]

    async def test_section_reaches_epub_builder(self, workdir, monkeypatch, issue_context):
        """期页的栏目要一路带到 create_epub。

        断在这里时目录分组会静默退化：group_by_section 拿到全空的 section，
        只产出一个无标题分组，看起来跟平铺列表一模一样，不报错。
        """
        monkeypatch.chdir(workdir)
        seen_sections: list[str] = []

        async def _scrape(page, url, manager, default_title="", section=""):
            seen_sections.append(section)
            return {**_article(1), "section": section}

        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1)])))
        monkeypatch.setattr(cli, "scrape_article", _scrape)
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, i, im, fn: calls.append(arts))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, issue_context)

        assert seen_sections == ["金融"], "栏目名要传给 scrape_article（用于剥标题前缀）"
        assert calls[0][0]["section"] == "金融", "栏目名要出现在入库的文章字典里（用于目录分组）"

    async def test_article_failure_is_isolated(self, workdir, monkeypatch, issue_context):
        """单篇抓取抛异常只丢这一篇，其余照常入库。"""
        monkeypatch.chdir(workdir)

        async def _scrape(page, url, manager, default_title="", **kwargs):
            if "1.html" in url:
                raise RuntimeError("page crashed")
            return _article(2)

        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1), _link(2)])))
        monkeypatch.setattr(cli, "scrape_article", _scrape)
        calls = []
        monkeypatch.setattr(cli, "create_epub", lambda arts, i, im, fn: calls.append(arts))

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, issue_context)

        assert [a["title"] for a in calls[0]] == ["文章2"]

    async def test_fatal_error_closes_page(self, monkeypatch, issue_context):
        async def _boom(*args, **kwargs):
            raise RuntimeError("fatal")

        monkeypatch.setattr(cli, "get_toc", _boom)

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, issue_context)

        assert issue_context.created_pages[0].closed is True, "异常路径也必须关页，否则浏览器会泄漏"

    async def test_worker_pages_are_closed(self, workdir, monkeypatch):
        """并发抓取时每篇文章新开的页面都要关掉。"""
        monkeypatch.chdir(workdir)
        context = FakeContext(pages=[FakePage(html=ISSUE_HTML)], default_html=ARTICLE_HTML)
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info([_link(1), _link(2)])))
        monkeypatch.setattr(cli, "scrape_article", _const(_article(1)))
        monkeypatch.setattr(cli, "create_epub", lambda *a, **k: None)

        await cli.process_one_issue("cw1226", "https://weekly.caixin.com/2026/cw1226/",
                                    {"date": "2026-10-12", "no": "39", "title": "t"},
                                    {"cover_url": None}, context)

        # 1 个期页 + 2 个 worker 页；worker 页用完即关
        assert context.created == 3
        workers = context.created_pages[1:]
        assert workers and all(p.closed for p in workers)


def _fake_request(mapping):
    return FakeRequest(mapping)


# ══ main() ═══════════════════════════════════════════════════════════


class TestMainGuardClauses:
    async def test_disclaimer_rejected_returns_immediately(self, monkeypatch, workdir):
        q = FakeQuestionary(confirms=[_Answer(False)])
        browser = FakeBrowser(FakeContext())
        _install_runtime(monkeypatch, q, browser, workdir)

        await cli.main()

        assert q.checkbox_calls == [], "未同意声明时不该进入选期界面"
        assert browser.closed is False, "连浏览器都不该启动"

    async def test_keyboard_interrupt_at_disclaimer(self, monkeypatch, workdir):
        q = FakeQuestionary(confirms=[_Answer(exc=KeyboardInterrupt())])
        browser = FakeBrowser(FakeContext())
        _install_runtime(monkeypatch, q, browser, workdir)

        await cli.main()

        assert q.checkbox_calls == []

    async def test_missing_cookies_file_returns(self, monkeypatch, workdir):
        q = FakeQuestionary(confirms=[_Answer(True)])
        browser = FakeBrowser(FakeContext())
        _install_runtime(monkeypatch, q, browser, workdir, cookies=False)

        await cli.main()

        assert q.checkbox_calls == []
        assert browser.closed is False

    async def test_cookie_parse_error_returns(self, monkeypatch, workdir):
        q = FakeQuestionary(confirms=[_Answer(True)])
        browser = FakeBrowser(FakeContext())
        _install_runtime(monkeypatch, q, browser, workdir)

        def _boom(path):
            raise ValueError("不是合法的 JSON")

        monkeypatch.setattr(cli, "load_cookies", _boom)
        await cli.main()

        assert q.checkbox_calls == []


class TestMainNavigation:
    def _setup(self, monkeypatch, workdir, q, browser, pasts, **kwargs):
        _install_runtime(monkeypatch, q, browser, workdir)
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info(past_issues=pasts)))
        downloads = []

        async def _process(issue_id, issue_url, selected, home_info, context):
            downloads.append((issue_id, issue_url, selected))

        monkeypatch.setattr(cli, "process_one_issue", _process)
        return downloads

    async def test_exit_option_quits_without_download(self, monkeypatch, workdir):
        q = FakeQuestionary(confirms=[_Answer(True)], checkboxes=[_Answer(["EXIT"])])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, [_past(0)])

        await cli.main()

        assert downloads == []
        assert browser.closed is True

    async def test_none_answer_is_treated_as_exit(self, monkeypatch, workdir):
        """Ctrl-C 时 questionary 返回 None，不能当成「空选择」继续循环。"""
        q = FakeQuestionary(confirms=[_Answer(True)], checkboxes=[_Answer(None)])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, [_past(0)])

        await cli.main()

        assert downloads == []

    async def test_keyboard_interrupt_in_checkbox_breaks(self, monkeypatch, workdir):
        q = FakeQuestionary(confirms=[_Answer(True)],
                            checkboxes=[_Answer(exc=KeyboardInterrupt())])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, [_past(0)])

        await cli.main()

        assert downloads == []

    async def test_selects_and_downloads(self, monkeypatch, workdir):
        pasts = [_past(0), _past(1)]
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer([pasts[1]])])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert [d[0] for d in downloads] == [pasts[1]["id"]]

    async def test_tasks_are_sorted_by_issue_id(self, monkeypatch, workdir):
        pasts = [_past(2), _past(0), _past(1)]
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer([pasts[0], pasts[1], pasts[2]])])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        ids = [d[0] for d in downloads]
        assert ids == sorted(ids)

    async def test_confirm_declined_cancels(self, monkeypatch, workdir):
        pasts = [_past(0)]
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(False)],
                            checkboxes=[_Answer([pasts[0]])])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert downloads == []
        assert q.confirm_calls[-1][1] is True, "确认框默认应为「是」"

    async def test_empty_selection_reprompts(self, monkeypatch, workdir):
        """回车但没勾选：提示后重新回到选期界面，而不是直接退出。"""
        pasts = [_past(0)]
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer([]), _Answer([pasts[0]])])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert len(q.checkbox_calls) == 2
        assert [d[0] for d in downloads] == [pasts[0]["id"]]

    async def test_next_page_navigation(self, monkeypatch, workdir):
        pasts = [_past(i) for i in range(20)]     # > page_size(12)，第一页有「下一页」
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer(["NEXT"]), _Answer([pasts[13]])])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert len(q.checkbox_calls) == 2
        assert "第 2 页" in q.checkbox_calls[1][0]
        assert [d[0] for d in downloads] == [pasts[13]["id"]]

    async def test_prev_page_navigation(self, monkeypatch, workdir):
        pasts = [_past(i) for i in range(20)]
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer(["NEXT"]), _Answer(["PREV"]), _Answer([pasts[0]])])
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert "第 1 页" in q.checkbox_calls[2][0]
        assert [d[0] for d in downloads] == [pasts[0]["id"]]

    async def test_prev_does_not_go_negative(self, monkeypatch, workdir):
        """第一页不该出现「上一页」；若因状态错乱收到 PREV，offset 不能变负。"""
        pasts = [_past(i) for i in range(20)]
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer(["PREV"]), _Answer([pasts[0]])])
        browser = FakeBrowser(FakeContext())
        self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert "第 1 页" in q.checkbox_calls[1][0]

    async def test_load_more_calls_page_and_refetches(self, monkeypatch, workdir):
        pasts = [_past(i) for i in range(5)]      # < page_size，出现「加载更多」
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer(["MORE"]), _Answer([pasts[0]])])
        page = FakePage()
        browser = FakeBrowser(FakeContext(pages=[page]))
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert page.selector_clicks == [".xsjMore, a.more"]
        assert [d[0] for d in downloads] == [pasts[0]["id"]]

    async def test_load_more_failure_shows_end_of_list(self, monkeypatch, workdir):
        pasts = [_past(i) for i in range(5)]
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer(["MORE"]), _Answer(["EXIT"])])
        page = FakePage(page_click_raises=RuntimeError("no such element"))
        browser = FakeBrowser(FakeContext(pages=[page]))
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert downloads == []

    async def test_manual_issue_id_is_queued(self, monkeypatch, workdir):
        pasts = [_past(0)]
        q = FakeQuestionary(
            confirms=[_Answer(True), _Answer(True)],
            checkboxes=[_Answer(["INPUT"]), _Answer([pasts[0]])],
            texts=[_Answer("cw1180")],
        )
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert q.text_calls and "期号" in q.text_calls[0]
        ids = [d[0] for d in downloads]
        assert "cw1180" in ids, "手动输入的期号应与勾选的期一起下载"
        assert len(ids) == 2

    async def test_invalid_manual_id_is_ignored(self, monkeypatch, workdir):
        pasts = [_past(0)]
        q = FakeQuestionary(
            confirms=[_Answer(True), _Answer(True)],
            checkboxes=[_Answer(["INPUT"]), _Answer([]), _Answer([pasts[0]])],
            texts=[_Answer("1180")],   # 少了 cw 前缀
        )
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        assert [d[0] for d in downloads] == [pasts[0]["id"]]

    async def test_navigation_syncs_checked_items_across_pages(self, monkeypatch, workdir):
        """勾了期又勾「下一页」时，导航优先，且已勾的期要带过去。

        questionary 的 checkbox 会把**所有**勾选项一起返回，所以
        `[某期, "NEXT"]` 是真实会出现的组合 —— 这时不能立刻开始下载。
        """
        pasts = [_past(i) for i in range(20)]
        q = FakeQuestionary(
            confirms=[_Answer(True), _Answer(True)],
            checkboxes=[
                _Answer([pasts[0], "NEXT"]),      # 第 1 页勾第一期，同时翻页
                _Answer([pasts[13]]),             # 第 2 页再勾一期后回车
            ],
        )
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        ids = [d[0] for d in downloads]
        assert pasts[0]["id"] in ids, "翻页前勾选的期不能丢"
        assert pasts[13]["id"] in ids
        assert len(ids) == 2

    async def test_unchecking_on_previous_page_removes_task(self, monkeypatch, workdir):
        """翻回上一页时取消勾选，该期要从任务清单里移除。"""
        pasts = [_past(i) for i in range(20)]
        q = FakeQuestionary(
            confirms=[_Answer(True), _Answer(True)],
            checkboxes=[
                _Answer([pasts[0], "NEXT"]),   # 第 1 页勾第一期 → 第 2 页
                _Answer(["PREV"]),             # 回第 1 页，什么都没勾
                _Answer([pasts[1]]),           # 只勾第二期
            ],
        )
        browser = FakeBrowser(FakeContext())
        downloads = self._setup(monkeypatch, workdir, q, browser, pasts)

        await cli.main()

        ids = [d[0] for d in downloads]
        assert ids == [pasts[1]["id"]], "取消勾选的第一期不应出现在下载清单里"

    async def test_runtime_exception_closes_browser(self, monkeypatch, workdir):
        q = FakeQuestionary(confirms=[_Answer(True)])
        browser = FakeBrowser(FakeContext())
        _install_runtime(monkeypatch, q, browser, workdir)

        async def _boom(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(cli, "get_toc", _boom)

        await cli.main()

        assert browser.closed is True, "异常路径也必须关浏览器"


class TestProcessOneIssueCalledFromMain:
    async def test_download_uses_home_info_for_cover(self, monkeypatch, workdir):
        """`main()` 把 home_info 一路传进 `process_one_issue`，封面兜底靠它。"""
        pasts = [_past(0)]
        q = FakeQuestionary(confirms=[_Answer(True), _Answer(True)],
                            checkboxes=[_Answer([pasts[0]])])
        browser = FakeBrowser(FakeContext())
        _install_runtime(monkeypatch, q, browser, workdir)
        monkeypatch.setattr(cli, "get_toc", _const(_issue_info(past_issues=pasts)))
        seen = {}

        async def _process(issue_id, issue_url, selected, home_info, context):
            seen["home_info"] = home_info
            seen["selected"] = selected
            seen["issue_url"] = issue_url

        monkeypatch.setattr(cli, "process_one_issue", _process)

        await cli.main()

        assert seen["home_info"]["past_issues"] == pasts
        assert seen["selected"]["title"] == pasts[0]["title"]
        assert seen["issue_url"] == pasts[0]["url"]


# ══ 入口脚本 ═════════════════════════════════════════════════════════


class TestEntryPoint:
    def test_main_py_delegates_to_cli(self, monkeypatch) -> None:
        """`python main.py` 只是把 `caixin.cli.main` 交给 asyncio 跑一遍。

        用 `runpy` 以 `__main__` 身份重新执行入口文件，顺带确认那行
        `if __name__ == "__main__"` 没被写坏（写坏了就是「跑起来什么都不做」）。
        """
        import runpy

        ran = []

        async def _fake_main():
            ran.append(True)

        monkeypatch.setattr(cli, "main", _fake_main)
        entry = Path(__file__).resolve().parent.parent / "main.py"
        runpy.run_path(str(entry), run_name="__main__")

        assert ran == [True]
