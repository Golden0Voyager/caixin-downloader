"""Playwright 与网络层的测试替身。

`fetcher` 和 `cli` 几乎每一行都在跟 `page` / `context` 打交道，但它们真正依赖的
只有十来个方法。把这些方法做出来，就能在完全离线的环境里跑通整条链路 ——
不需要浏览器，也不需要财新的 Cookie。

刻意做得「笨」：所有等待都是空操作，所有返回值都由测试直接给定。测试要断言的是
「代码怎么使用这些返回值」，而不是 Playwright 本身的行为。
"""

from __future__ import annotations

import json


class FakeResponse:
    """同时扮演 HTTP 响应与 Playwright 的 response 事件对象。"""

    def __init__(self, url="", body="", status=200, headers=None):
        self.url = url
        self._body = body
        self.status = status
        self.headers = headers or {}

    async def text(self):
        return self._body if isinstance(self._body, str) else self._body.decode("utf-8")

    async def body(self):
        return self._body if isinstance(self._body, bytes) else self._body.encode("utf-8")


class FakeRequest:
    """`context.request` —— 只实现 `get()`，支持按 URL 编排返回值。

    值可以是：
    - `FakeResponse`：每次都返回它；
    - `list`：按顺序消费（最后一个会一直复用），用来模拟「第一次 502、第二次 200」；
    - `Exception` 实例：抛出它，用来模拟连接失败。
    """

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.get(url)
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        if value is None:
            return FakeResponse(url=url, body=b"", status=404)
        if isinstance(value, bytes):
            return FakeResponse(url=url, body=value)
        return value


class FakeLocator:
    """`page.locator(...)` 的替身，只覆盖「余下全文」用到的三个成员。"""

    def __init__(self, page):
        self.page = page

    async def count(self):
        if self.page.locator_count_raises:
            raise RuntimeError("locator not ready")
        return self.page.fulltext_links

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        if self.page.click_raises:
            raise RuntimeError("click intercepted by overlay")
        self.page.clicked += 1
        # 真实页面点「余下全文」后正文会变长；长度不变就代表点击静默失败
        if self.page.expanded_body_text is not None:
            self.page.body_text = self.page.expanded_body_text


class FakePage:
    """`page` 的替身。

    参数对应测试要触发的分支：
    - `responses`：`goto` 期间触发的 response 事件（正文 API 拦截靠它）；
    - `fulltext_links`：「余下全文」链接数量，0 表示单页文章；
    - `body_text` / `expanded_body_text`：`inner_text("body")` 的前后长度比对；
    - `raise_inner_text_calls`：在第几次调用 `inner_text` 时抛异常（从 1 计）。
    """

    def __init__(
        self,
        html="",
        responses=None,
        fulltext_links=0,
        body_text="正文",
        expanded_body_text=None,
        click_raises=False,
        locator_count_raises=False,
        raise_inner_text_calls=None,
        goto_raises=None,
        context=None,
        page_click_raises=None,
    ):
        self.html = html
        self.responses = list(responses or [])
        self.fulltext_links = fulltext_links
        self.body_text = body_text
        self.expanded_body_text = expanded_body_text
        self.click_raises = click_raises
        self.locator_count_raises = locator_count_raises
        self.raise_inner_text_calls = set(raise_inner_text_calls or ())
        self.goto_raises = goto_raises
        self.context = context
        # 与 `click_raises` 区分：那个是「余下全文」链接（locator.click），
        # 这个是 `page.click(selector)`（首页「加载更多」）。
        self.page_click_raises = page_click_raises

        self.visited: list[str] = []
        self.clicked = 0  # 「余下全文」链接的点击次数（FakeLocator.click）
        self.selector_clicks: list[str] = []  # page.click(selector) 的选择器序列
        self.routes: list[tuple] = []
        self.closed = False
        self._handlers: dict[str, list] = {}
        self._inner_text_calls = 0

    # ── 事件 ──
    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit(self, url, body):
        """手动投递一个 response 事件，返回待 await 的 Task（没有监听者则为 None）。

        `body` 也可以是现成的 `FakeResponse` —— 需要构造「读响应体时抛异常」这类
        场景时，光有字节串不够用。
        """
        response = body if isinstance(body, FakeResponse) else FakeResponse(url=url, body=body)
        tasks = []
        for handler in list(self._handlers.get("response", [])):
            task = handler(response)
            if task is not None:
                tasks.append(task)
        return tasks

    # ── 导航 ──
    async def goto(self, url, **kwargs):
        self.visited.append(url)
        if self.goto_raises:
            raise self.goto_raises
        for resp_url, body in self.responses:
            for task in self.emit(resp_url, body):
                await task
        return None

    async def content(self):
        return self.html

    async def wait_for_load_state(self, state=None, timeout=None):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def inner_text(self, selector):
        self._inner_text_calls += 1
        if self._inner_text_calls in self.raise_inner_text_calls:
            raise RuntimeError("element is not attached to the DOM")
        return self.body_text

    def locator(self, selector, has_text=None):
        return FakeLocator(self)

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def click(self, selector, **kwargs):
        self.selector_clicks.append(selector)
        if self.page_click_raises:
            raise self.page_click_raises

    async def close(self):
        self.closed = True


class FakeContext:
    """`context` 的替身：发新页面 + 下载资源。"""

    def __init__(self, pages=None, request=None, default_html=""):
        self._pages = list(pages or [])
        self.request = request or FakeRequest()
        self.default_html = default_html
        self.created = 0
        self.created_pages: list[FakePage] = []   # 按创建顺序留档，便于断言关页
        self.closed = False

    async def new_page(self):
        self.created += 1
        page = self._pages.pop(0) if self._pages else FakePage(html=self.default_html)
        page.context = self
        self.created_pages.append(page)
        return page

    async def add_cookies(self, cookies):
        return None

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.closed = False

    async def new_context(self, **kwargs):
        return self.context

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    async def launch(self, **kwargs):
        return self.browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)


class FakeAsyncPlaywright:
    """`async with async_playwright() as p:` 的替身。"""

    def __init__(self, browser):
        self.browser = browser

    async def __aenter__(self):
        return FakePlaywright(self.browser)

    async def __aexit__(self, *exc_info):
        return False


# ══ 页面 HTML 夹具 ══════════════════════════════════════════════════
# 结构与真实期页一致（类名、嵌套都照抄），但内容是合成的 —— 既避免把财新的
# 版权内容放进仓库，也能顺手塞进真实页面上出现过的各种坑（未配对的 {{ }}、
# 两种日期写法、栏目标题中英混排）。

HOME_HTML = """
<html><body>
  <div class="mi">
    <a href="https://weekly.caixin.com/2026/cw1226/"><img src="https://img.caixin.com/cover.jpg"/></a>
  </div>
  <div class="main">
    <div class="report">
      <div class="magazine-container">
        <div class="title">《财新周刊》总第1226期</div>
        <div class="source">本文来源于《财新周刊》 2026年第39期 出版日期：2026-10-12</div>
        <div class="reportTit">封面报道 Cover Story</div>
        <dl>
          <dt><a href="https://weekly.caixin.com/2026-10-09/1.html">抢占办公室</a></dt>
          <dd>字节跳动、腾讯、阿里巴巴争夺全流程日常办公应用场景</dd>
        </dl>
      </div>
    </div>
    <div class="magContent2">
      <div class="magIntrotit">金融 Finance</div>
      <dl><dt><a href="https://weekly.caixin.com/2026-10-10/2.html">财新周刊｜AI重塑金融</a></dt></dl>
      <dl><dt><a href="https://weekly.caixin.com/2026-10-10/3.html">专栏｜不断深化的滞胀格局</a></dt></dl>
      <div class="magIntrotit">商业 Business</div>
      <dl><dt><a href="https://weekly.caixin.com/2026-10-10/4.html">{{新手机在涨价</a></dt></dl>
    </div>
  </div>
  <div class="xsjCon">
    <ul>
      <li><a href="https://weekly.caixin.com/2026/cw1225/">上一期</a> 2026年10月05日 年度期号:38 《财新周刊》2026年第38期</li>
    </ul>
  </div>
</body></html>
"""

# 没有任何「2026年第N期」文本的首页：issue_no 会停在 "XX"，
# 从而触发「新建临时页面去期页偷准确信息」那条分支。
HOME_HTML_WITHOUT_ISSUE = """
<html><body>
  <div class="mi"><a href="https://weekly.caixin.com/2026/cw1226/"><img src="https://img.caixin.com/cover.jpg"/></a></div>
  <div class="main">
    <div class="magContent2">
      <div class="magIntrotit">金融 Finance</div>
      <dl><dt><a href="https://weekly.caixin.com/2026-10-10/2.html">AI重塑金融</a></dt></dl>
    </div>
  </div>
</body></html>
"""

# 期页：只有 .main，没有 .mi、没有 .xsjCon —— 真实期页就是这样
ISSUE_HTML = """
<html><body>
  <div class="main">
    <div class="report">
      <div class="magazine-container">
        <div class="source">本文来源于《财新周刊》 2026年第39期 出版日期：2026-10-12</div>
        <div class="reportTit">封面报道 Cover Story</div>
        <dl><dt><a href="https://weekly.caixin.com/2026-10-09/1.html">抢占办公室</a></dt></dl>
      </div>
    </div>
    <div class="magContent2">
      <div class="magIntrotit">金融 Finance</div>
      <dl><dt><a href="https://weekly.caixin.com/2026-10-10/2.html">财新周刊｜AI重塑金融</a></dt></dl>
    </div>
  </div>
</body></html>
"""

# 文章页：标题带未配对的 {{ }}、正文有硬缩进、带内联 base64 背景、
# 图片走 cximg 容器、文末有相关推荐区块。
# 正文刻意写得比 MIN_ARTICLE_CHARS 长 —— 否则会被判成空章。
ARTICLE_HTML = """
<html><body>
  <h1 class="wiv-title">{{深度报道：AI 时代的就业变局</h1>
  <div class="author">文｜财新周刊 徐路易</div>
  <div id="Main_Content_Val" class="text" style="background: url(data:image/png;base64,AAAA)">
    <p>　　第一段正文。字节跳动、腾讯、阿里巴巴都在争夺全流程日常办公应用场景，
      而这场竞争的胜负，最终取决于谁能把模型能力真正嵌进企业既有的工作流里。</p>
    <p>　　第二段正文。采访中多位企业 IT 负责人提到，采购决策已经从「试试看」
      转向「算得清账」，这对供应商的交付能力提出了完全不同的要求。</p>
    <cximg><img data-src="//img.caixin.com/a.jpg"/><div class="article_img_talk">图：财新</div></cximg>
    <div class="related-articles"><a href="https://weekly.caixin.com/x.html">相关阅读</a></div>
  </div>
</body></html>
"""

# 极简文章页：只有一段短正文，用来触发空章判定
ARTICLE_HTML_EMPTY = """
<html><body>
  <h1 class="wiv-title">图集：某地风景</h1>
  <div id="Main_Content_Val"><p>图集说明。</p></div>
</body></html>
"""


def make_jsonp(content_html, code=0):
    """拼一个财新 checkAuthByIdJsonp 的响应体。

    真实响应形如 `callback({"code":0,"data":"resetContentInfo({...})"})`；
    这里同时支持裸 JSON（`json.loads` 直接成功）和 JSONP 两种解析路径。
    """
    inner = json.dumps({"content": content_html}, ensure_ascii=False)
    payload = {"code": code, "data": f"resetContentInfo({inner})"}
    return json.dumps(payload, ensure_ascii=False)


def make_jsonp_wrapped(content_html, code=0):
    """带 JSONP 回调外壳的版本（裸 `json.loads` 会失败，走正则那条路径）。"""
    return f"caixinCallback({make_jsonp(content_html, code)})"
