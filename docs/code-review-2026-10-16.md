# 财新周刊 EPUB 下载器 · 代码评审

> **修复进度（2026-10-17）**：P0-1/P0-2、P1-1 ~ P1-9、P2-1 ~ P2-9、**P3-1 ~ P3-7** 已全部实施。
> 代码已从单文件 `main.py` 拆成 `caixin/` 包，CSS 外置到 `style/default.css`。
> 本文保留评审当时的判断与证据，行号已随重构失效，请以模块名/函数名为准。
> 测试 47 → **236 passed**；覆盖率 26% → **97.74%**（口径修正后；`fail_under` 已提到 90）。

- 日期：2026-10-16（覆盖数据更新于 2026-10-17）
- 范围：`main.py`(1183 行) · `test_caixin.py` · `tests/`(3 个文件, 729 行) · `pyproject.toml` · `.github/workflows/ci.yml`
- 方法：通读 + 真实期页 (`weekly.caixin.com/2026/cw1226/`) 结构验证 + 成品 EPUB 解包检查 + Playwright 行为实测

---

## 0. 现状总览

| 维度 | 现状 | 评价 |
|---|---|---|
| 代码组织 | 单文件 1183 行，`scrape_article` 325 行、`create_epub` 208 行、`main` 185 行 | 可维护性瓶颈 |
| 测试 | 47 passed；`main.py` 覆盖率 **26%**（报告里的 52% 是虚高，见 P2-2） | 核心链路无测试 |
| 静态检查 | ruff ✅ / mypy ✅ / bandit 1×HIGH (B324) | 基本干净 |
| CI | 三道门禁全部带 `\|\| true`，其中一道命令根本不存在 | **形同虚设** |
| EPUB 合规 | mimetype(STORED)/OPF/spine 结构正确；`dc:date` 可能非 ISO、存在悬空 `cover.xhtml` | 小瑕疵 |

**核心判断**：排版层（前几轮迭代的重点）已经相当扎实；现在真正的风险集中在**抓取链路的静默失败**——它们不报错，只是悄悄少给内容或给错内容。下面 P0/P1 里 7 条有 5 条属于这一类。

### 0.1 覆盖率的实际推进（2026-10-17 补记）

| 阶段 | 测试数 | 覆盖率 | 关键动作 |
|---|---|---|---|
| 评审当时 | 47 | 26% | 只有排版层与 EPUB 组装有测试 |
| P3 收尾后 | 130 | 39% | 补了栏目分组、空章过滤、landmarks 等 |
| 本轮 | **236** | **97.74%** | 用 `tests/fakes.py` 的离线替身打通抓取与编排 |

40% 那会儿的分布很畸形：`fetcher.py` 12%、`cli.py` 0%，两者合计占未覆盖语句的 97%。
根因不是「难测」，而是**没人拆接缝**——`fetcher` 每一步都碰 `page`，`cli.main()`
把 `questionary` / `playwright.async_api` 写成函数内延迟导入，看起来只能人工点。

`tests/fakes.py` 的做法：只实现真正用到的十几个方法（`goto` / `content` / `on` /
`inner_text` / `locator` / `route` / `click` / `new_page` / `request.get`），
返回值全部由测试给定，等待全部是空操作。于是：

- `get_toc` 变成纯解析测试，喂 HTML 断言栏目、期号、日期、去重；
- `scrape_article` 能造出「API 比 DOM 长」「点『余下全文』静默失败」「图片 404」
  这些真实事故场景，而不用碰网络；
- `cli.main()` 的交互分支（NEXT / PREV / MORE / INPUT / EXIT）用队列化的
  `FakeQuestionary` 逐个走一遍，`async_playwright` 换成假的 `async with` 对象。

> 注：本机沙箱里 pytest 的 `tmp_path` 会被文件系统钩子拦成 `PermissionError`
> （创建 numbered-dir 时的 `EEXIST` 被改写），所以 `tests/test_cli.py` 自建了
> `workdir` 夹具（`mkdtemp` + `chdir` + 清理），没有依赖 `tmp_path`。

剩余 2.3% 基本都是**循环退出分支**（`for` 跑完自然结束）和一处构造上不可达的
防御性分支（`fetcher.py` 里已被 `# pragma: no cover` 标注并写明理由）。

---

## P0 · 会造成内容丢失或凭据泄露

### P0-1 `_truncate_after_end_marker` 可能删掉整篇正文

**位置** `main.py:375-398`

```python
for tag in root.find_all(["p", "div", "section", "h2", "h3", "h4", "figcaption"]):
    text = tag.get_text(strip=True)
    if any(kw in text for kw in trunc_kws):
        for sibling in list(tag.find_next_siblings()):
            sibling.decompose()
        tag.decompose()
        return True
```

**问题**：`find_all` 是文档序（祖先先于后代），而 `get_text()` 会拼上所有子孙文本。所以只要正文被一个 `<div>` 包裹，**这个包裹 div 本身就会命中关键词**，然后被整块删掉。

**这不是理论风险**：成品 EPUB 里 27/27 章的正文结构都是 `content-body > div > p…`（单层包裹），抓取时的 `#Main_Content_Val` 同样是这个形状。

实测复现：

```
输入: <div class="wrap"><p>本文探讨相关阅读产品的设计。</p><p>正文第二段</p><p>正文第三段</p></div>
命中: div ['wrap'] '本文探讨相关阅读产品的设计。正文第二段，正常内容。正文第三段'
剩余: ''
```

**后果**：只要某篇正文里合法出现「相关阅读 / 延伸阅读 / 更多报道详见」中的任一词，整篇正文清零，而且不会报错——只会触发 `len(content_text) < 80` 的「可能是图集/视频类栏目」告警，误导排查方向。

反过来说，当前这个功能**要么不生效、要么全毁**，从来没有正确地只切掉尾巴。

**改法**：只认「叶子块」（不含其他块级子元素、文本很短），从后往前取最后一个匹配，删除时沿祖先链逐层切。

```python
_TRUNC_KEYWORDS = ("相关视频", "视频说明", "更多报道详见", "相关阅读",
                   "延伸阅读", "热门文章", "推荐阅读", "对此文亦有贡献")
_BLOCK_TAGS = ["p", "div", "section", "h2", "h3", "h4", "figcaption"]
_NESTED_TAGS = ["p", "div", "section", "h1", "h2", "h3", "h4", "figure", "table", "ul", "ol"]

def _find_end_marker(root):
    """从后往前找「结尾标志」：只认叶子块，避免包裹整篇的容器整体命中。"""
    hits = []
    for tag in root.find_all(_BLOCK_TAGS):
        if tag.find(_NESTED_TAGS):        # 是容器不是叶子
            continue
        text = tag.get_text(strip=True)
        if not text or len(text) > 40:    # 正文段落不会只有 40 字还长这样
            continue
        if any(kw in text for kw in _TRUNC_KEYWORDS):
            hits.append(tag)
    return hits[-1] if hits else None

def _truncate_after_end_marker(root):
    marker = _find_end_marker(root)
    if marker is None:
        return False
    node = marker
    while node is not None and node is not root:
        for sib in list(node.find_next_siblings()):
            sib.decompose()
        parent = node.parent
        node.decompose()
        node = parent
    return True
```

> 回归测试要覆盖两种输入：① 尾巴在包裹 div 内部；② 正文段落里出现关键词但**没有**结尾标志（应完全不截断）。

---

### P0-2 cookie 备份文件没被 .gitignore 覆盖

**位置** `.gitignore:10` + 项目根目录 `cookies.json.bak.20261015105701`

`.gitignore` 只写了 `cookies.json`，而备份文件 `cookies.json.bak.20261015105701` 里是**真实会话凭据**（含 `.caixin.com` 域的 token）。一次 `git add -A` 就会进仓库。

已确认历史提交里没有 cookie（`git log --all` 为空），所以不需要改写历史。

**改法**：

```gitignore
cookies.json*
```

并把现有备份移出工作区（`mv cookies.json.bak.* ~/.config/caixin/`）。

---

## P1 · 真实缺陷

### P1-1 出版日期被丢弃，EPUB 里显示的是「打包当天」

**位置** `main.py:574, 592-593, 654, 699-706`（`get_toc`）+ `main.py:716-722`（`create_epub`）

三处独立问题叠在一起：

1. **`get_toc` 算出了 `issue_date`，却没放进返回值**：

```python
issue_date = datetime.now().strftime("%Y年%m月%d日")   # 574
...
dm = re.search(r"(\d{4}年\d{2}月\d{2}日)", raw)        # 592   ← 只认「年月日」
if dm: issue_date = dm.group(1)
...
return {
    "issue_title": ..., "issue_no": ..., "cover_url": ...,
    "links": ..., "past_issues": ..., "topic": ...,        # ← issue_date 不在里面
}
```

2. **期页的日期格式是 `YYYY-MM-DD`，上面那个正则匹配不到**。用真实期页实测：

```
页面文本: '本文来源于《财新周刊》 2026年第39期 出版日期：2026-10-12'
旧正则 (年月日): None
```

3. `create_epub` 只能从 `past_issues[0]["date"]` 里猜日期（716-720），而**期页上根本没有 `.xsjCon li` 历史列表**（实测 0 条），于是 `issue_date = ""`，最后落到 `datetime.now()`。

**后果**：从期页下载时，`dc:date` 与目录页的 `.toc-meta` 都变成打包日期。当前成品里的「2026年10月12日」是离线重排脚本从旧 EPUB 里带过来的，走正常流程跑一遍就会丢。

**改法**：`get_toc` 里统一用宽松正则，并把结果返回；`create_epub` 优先用 `info["issue_date"]`。

```python
# get_toc
dm = re.search(r"(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})", raw)
if dm:
    issue_date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"   # 统一存 ISO
...
return {..., "issue_date": issue_date, ...}
```

```python
# create_epub
iso_date = info.get("issue_date") or next(
    (p["date"] for p in info.get("past_issues") or [] if p.get("date")), "")
iso_date = _to_iso(iso_date) or datetime.now().strftime("%Y-%m-%d")
book.add_metadata("DC", "date", iso_date)      # dc:date 必须是 ISO 8601
```

显示用的中文日期另外格式化，别和 `dc:date` 共用一个变量。

---

### P1-2 `dc:date` 写成中文，不符合 EPUB 3

**位置** `main.py:722`

```python
book.add_metadata('DC', 'date', issue_date or datetime.now().strftime('%Y-%m-%d'))
```

`issue_date` 来自 `past_issues[i]["date"]`，格式是 `2026年10月12日`。EPUB 3 要求 `dc:date` 是 ISO 8601，epubcheck 会直接报 ERROR。

当前成品实测：

```
dc:date = '2026年10月12日' | ISO8601 合规: False
```

和 P1-1 一起修：内部统一存 ISO，展示时再转中文。

---

### P1-3 重复的封面文档

**位置** `main.py:855-856`

```python
if info.get("cover_data"):
    book.set_cover("images/cover.jpg", info["cover_data"])
```

ebooklib 的 `set_cover(file_name, content, create_page=True)` 会额外生成一个 `cover.xhtml`（纯 `<img>`、无样式）。我们又自己建了 `cover_page.xhtml`，于是 manifest 里两个封面页并存：

```
<item href="cover.xhtml" id="cover" media-type="application/xhtml+xml"/>
<item href="cover_page.xhtml" id="cover_page" media-type="application/xhtml+xml"/>
<itemref idref="cover_page"/>
```

`cover.xhtml` 不进 spine，是个悬空文档；但有些阅读器会按文件名 `cover.xhtml` 找封面，结果拿到那张没样式、没 `max-height` 约束的版本。

**改法**：

```python
book.set_cover("images/cover.jpg", info["cover_data"], create_page=False)
```

---

### P1-4 图片拦截路由是死代码（已实测）

**位置** `main.py:969-970`

```python
await tp.route("**/*.{png,jpg,jpeg,gif,webp}",
               lambda r: r.continue_() if "caixin.com" in r.request.url else r.abort())
await tp.route("**/*",
               lambda r: r.abort() if any(x in r.request.url for x in [...]) else r.continue_())
```

Playwright 的路由按**注册倒序**匹配：后注册的 `**/*` 先命中并对一切 `continue_()`，所以第一条「非 caixin 图片一律 abort」永远不会执行。

实测（先注册图片路由，后注册 `**/*`，访问 example.com）：

```
命中顺序: ['B:all', 'B:all']
说明: 若只有 B 命中，说明后注册的 **/* 覆盖了先注册的图片路由
```

**后果**：页面照样下载全部图片，之后 `ImageManager` 再下一次。一期 27 篇、79 张源图，等于带宽翻倍。

**改法**：合并成一个 handler。

```python
BLOCK_HOSTS = ("analytics", "adserver", "hm.baidu.com")
BLOCK_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2")

async def _route(r):
    url = r.request.url
    if any(h in url for h in BLOCK_HOSTS):
        return await r.abort()
    if url.lower().split("?")[0].endswith(BLOCK_EXTS) and "caixin.com" not in url:
        return await r.abort()
    await r.continue_()

await tp.route("**/*", _route)
```

---

### P1-5 图片并发重复下载

**位置** `main.py:186-227` + `main.py:464`

`self.images[url]` 只在下载**完成之后**才写入；`Semaphore(2)` 下两篇文章同时请求同一 URL 时会各下一遍。

本期「财」字标志被 23 章引用，58 个文件 vs 79 张源图说明缓存大体生效——但那是靠「同一篇内的 `gather` 顺序」和「并发只有 2」侥幸，一旦提高并发就会退化。

**改法**：加 in-flight 表，同 URL 复用同一个 Future。

```python
def __init__(self, context):
    ...
    self._inflight: dict[str, asyncio.Future] = {}

async def download(self, url, retries=2):
    url = self.normalize_url(url)
    if not url: return None
    if url in self.images: return self.images[url]["filename"]
    if url in self.skipped: return None
    if url in self._inflight:            # 别人正在下，等结果
        return await self._inflight[url]

    fut = asyncio.get_running_loop().create_future()
    self._inflight[url] = fut
    try:
        result = await self._download_once(url, retries)
        fut.set_result(result)
        return result
    finally:
        self._inflight.pop(url, None)
```

---

### P1-6 非 200 响应不重试

**位置** `main.py:224`

```python
if response.status == 200:
    ...
    return filename
break          # ← 502 / 超时 也走这里，retries=2 形同虚设
```

`break` 跳出重试循环直接返回 `None`。只有抛异常才会重试，而 HTTP 502、503 是正常返回。

**改法**：`break` → `continue`，循环结束后再 `return None`。

---

### P1-7 文件名未做非法字符转义

**位置** `main.py:985`

```python
filename = f"download/{selected_past['date']} (第{selected_past['no']}期) - {selected_past['title']}.epub"
```

`title` 直接来自网页。含 `/` 会写到别的目录或直接失败；`:` `|` `?` `*` 在 Windows 上非法。

**改法**：

```python
def safe_filename(name, fallback="未命名", limit=80):
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit] or fallback
```

---

### P1-8 首段缩进前后不一致

**位置** `main.py:804`（`.content-body p:first-of-type { text-indent: 0 }`）

`:first-of-type` 只看标签不看类。带 `article-byline` 的章节里，byline 自己占掉了 `:first-of-type`，正文第一段变成 `nth-of-type(2)`，于是**被缩进 2 字**；没有 byline 的章节，第一段**不缩进**。

成品实测：

| 章节 | p[0] | p[1]（正文第一段） |
|---|---|---|
| chapter_0 | `class=['article-byline']` 文｜财新周刊 关聪… | `class=None` 中国三大互联网科技巨头… → **有缩进** |
| chapter_1 | `class=None` 又到了一年一度诺贝尔奖… → **无缩进** | — |

同一本书里两种首段处理，翻页时很显眼。

**改法**：

```css
.content-body p:first-of-type { text-indent: 0; }
.content-body p.article-byline + p { text-indent: 0; }   /* byline 之后的正文首段 */
```

---

### P1-9 `hashlib.md5` 触发 bandit HIGH/B324

**位置** `main.py:215`

```python
img_hash = hashlib.md5(url.encode()).hexdigest()
```

只用于图片去重命名，没有安全含义；但它让 bandit 常年挂着一条 HIGH，真问题容易被淹掉。

**改法**：`hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()`

---

## P2 · 工程与可维护性

### P2-1 CI 三道门禁全是装饰性的

**位置** `.github/workflows/ci.yml`

```yaml
- run: uv run mypy main.py src/ || true        # 实测已 0 issue，|| true 可以去掉
- run: uv tool run bandit -r . || true         # 扫到了 .venv：54 万行 / 3328 条 Low
- run: uv audit || true                        # uv 0.9.17 没有 audit 子命令，必失败
```

实测：

```
$ uv audit
error: unrecognized subcommand 'audit'
```

`bandit -r .` 把 `.venv` 一起扫了（`Total lines of code: 543850`，`Low: 3328`），真正的 1 条 HIGH 埋在噪音里。

**改法**：

```yaml
- name: ruff
  run: uv run ruff check .                      # 含 tests/
- name: mypy
  run: uv run mypy main.py
- name: bandit
  run: uv tool run bandit -r main.py -q
- name: dependency audit
  run: uvx pip-audit
```

### P2-2 覆盖率统计口径错，门槛形同虚设

> **已做（2026-10-17）**：最终落地的是 `source = ["."]` + `omit = ["tests/*", "test_caixin.py"]`。
> 注意 `source = ["main.py"]` **不能用** —— coverage 会把它当模块名，报
> 「Module main.py was never imported」；`include = ["main.py"]` 同样不生效。
> 门槛先设 25（对应当时 39%），本轮覆盖率到 97.74% 后提到 **90**。

`[tool.coverage.run] source = ["."]` 把测试文件本身算进分母（tests 三个文件 99-100%），把总数拉到 52%；只看 `main.py` 是 **26%**。而 `fail_under = 15` 远低于这两个数，等于没有门槛。

**改法**：

```toml
[tool.coverage.run]
source = ["."]
omit = ["tests/*", "test_caixin.py"]
branch = true

[tool.coverage.report]
fail_under = 90
```

### P2-3 三个测试文件各自复制 `sys.modules` 打桩

`tests/test_create_epub.py:15-24`、`tests/test_image_manager.py:19-25`、`tests/test_typography.py:18-24` 是同一段代码。

两个层次的改法：
- 便宜：抽到 `tests/conftest.py`（conftest 先于测试模块导入，打桩仍生效）。
- 更好：把 `playwright` / `questionary` 的 import 挪进 `main()` 函数内部（它们只在运行期需要）。这样 `import main` 本身就没有重副作用，三处打桩可以全删。

### P2-4 CSS 是 `create_epub` 里的局部字符串

**位置** `main.py:727-849`（123 行）

后果：
- 无法单独 lint / diff / 复用；
- 测试要「先构建一个 EPUB，再解包读 CSS」才能校验（`TestEmScale._css`），慢且绕；
- 改排版必须动 Python 文件。

**改法**：抽成 `style/default.css`，`create_epub` 用 `Path(...).read_text()` 读；测试直接读文件，`TestEmScale` 能快一个数量级，也让 CSS 可以被编辑器正确高亮。

### P2-5 单文件 1183 行，核心函数过长

```
   56-63       8 行  clean_title
   66-75      10 行  sanitize_inline_style
   78-95      18 行  strip_hard_indent
  111-118      8 行  safe_goto
  120-227    108 行  ImageManager
  229-553    325 行  scrape_article      ← 抓取 + 截断 + 清洗 + 图片 + 组装 五件事
  555-706    152 行  get_toc
  708-915    208 行  create_epub
  917-994     78 行  process_one_issue
  996-1180   185 行  main
```

建议拆分（也让覆盖率从 26% 提到 60%+，因为纯函数部分可以单独测）：

```
caixin/
  images.py       ImageManager
  scraper.py      safe_goto / get_toc / scrape_article
  postprocess.py  _truncate_after_end_marker / clean_watermark / strip_hard_indent / clean_title
  epub_builder.py create_epub
  cli.py          main
  style/default.css
```

`scrape_article` 325 行里，真正跟 Playwright 耦合的只有「打开页面 / 展开全文 / 取 HTML」约 80 行，其余全是 BeautifulSoup 后处理——拆出来就能测。

### P2-6 打包是空的

`pyproject.toml` 用 `uv_build` + `src/` 布局，但 `src/caixin_downloader/__init__.py` 只有：

```python
def hello() -> str:
    return "Hello from caixin-downloader!"
```

`uv build` 出来的 wheel 不含任何实际代码。同时：
- `description = "Add your description here"`（脚手架占位符）
- `version = "0.1.0"` vs `main.VERSION = "2.5"`（两套版本号）
- `src/caixin_downloader/py.typed` 声明了类型包，但包里没有代码

**二选一**：要么删掉 `src/`（纯脚本项目，`[tool.uv] package = false`），要么真按 P2-5 把代码搬进去。

### P2-7 魔法数字散落各处

| 值 | 位置 | 含义 |
|---|---|---|
| `35` | `main.py:946` | 每期最多抓几篇（且静默截断） |
| `2020` | `main.py:927` | 年份探测下界 |
| `12` | `main.py:1033` | 交互分页大小 |
| `2` | `main.py:961` | 抓取并发 |
| `15000/60000/3000/4000` | 多处 | 各种超时（ms） |
| UA 字符串 | `main.py:1025` | 写死 Chrome 122 |

集中到文件头常量区，`Semaphore` 与篇数上限最好能通过环境变量覆盖。

`links[:35]` 尤其要注意：**超出的文章被静默丢弃**，至少加一行 `console.log` 告警。

### P2-8 `test_caixin.py` 是网络脚本却叫 `test_`

- 谁执行 `pytest test_caixin.py` 就会真的去请求财新（`testpaths = ["tests"]` 只在无参数时生效）。
- 它和 `main.py:1008-1018` 的 cookie 解析逻辑**完全重复**。

**改法**：加 `__test__ = False`（或改名 `check_cookie.py`），并把 cookie 解析抽成 `load_cookies(path) -> list[dict]` 供两边共用。

### P2-9 文档与代码脱节

| 文档 | 写的 | 实际 |
|---|---|---|
| `README.md` | Python 3.10+ | `requires-python >= 3.12` |
| `README.md` | 1.85 倍行高 | `line-height: 1.8` |
| `README.md` | 自定义目录页 + **空白过渡页** | 过渡页已不存在 |
| `CLAUDE.md` | `uv pip install -r requirements.txt` | 无 `requirements.txt`，用 `uv sync` |
| `CLAUDE.md` | 依赖含 `requests` / `lxml` | 均未使用 |
| `pyproject.toml` | `description` 占位符 | 未填 |

### P2-10 `.git` 有 130MB

历史里塞过大文件（EPUB / 截图）。`git gc --aggressive` 能收一部分；真要瘦身得重写历史，风险高于收益，建议先记着。

---

## P3 · 可以再往前一步（非缺陷）

> **状态：7 条全部已实施（2026-10-17）**，实施备注见每条末尾。

1. **正文行长无上限** —— iPad 横屏单行 90+ 字符。给 `.page` 加 `max-width: 34em; margin: 0 auto` 即可（约 34 个汉字/行，接近印刷惯例）。注意与阅读器自带边距叠加，要在 `max()` 里再取一次较小值。
   > **已做**：取 `33em`（=660px）而非 34em —— 660 是 3 的倍数，34em 是 680，会破坏 `TestEmScale` 的网格约束。`margin: 0 auto` 居中，实测 1180px 视口下左右各留 189px。
2. **目录未分组** —— 财新天然有「封面报道 / 财新观察 / 周刊导播 / 专栏 / 财经数说」栏目，`get_toc` 抓的时候顺手带上栏目名，目录页就能做二级缩进。
   > **已做**：`fetcher.extract_article_links()` 按文档顺序遍历，遇到 `.magIntrotit` / `.reportTit` 就记下栏目名；`epub_builder.group_by_section()` 分组。真实期页实测 27 篇 → 10 个栏目，顺序与版面一致。标题里与栏目重复的前缀（`封面报道｜`、`财新周刊｜`）由 `strip_section_prefix()` 去掉，但 `专栏｜`「随笔｜」这类体裁标记保留。
   > ⚠️ **补丁（2026-10-17 实跑发现）**：上面那条「10 个栏目」是在 `extract_article_links` 层面验的，
   > 端到端其实没通 —— `scrape_article` 既不接收也不返回 `section`，`group_by_section` 只看到一个空栏目，
   > 目录页仍是 27 条平铺（且条目带「封面报道｜」前缀），**不报错**。已给 `scrape_article` 加 `section`
   > 参数（同时用它再剥一次文章页 `<h1>` 的前缀）并在返回值里透传；`cli` 传 `item["section"]`。
   > 重抓验收：`contents.xhtml` 10 个分组 / 27 条。教训见 `.workbuddy-ai/memory/2026-10-17.md`。
3. **空章节只告警不过滤** —— `< 80` 字的图集/视频栏目目前会作为空章节进书，建议默认跳过并汇总在结尾提示。
   > **已做**：`scrape_article` 返回 `empty` 标记（`< MIN_ARTICLE_CHARS` 字**且一张图都没有**），`cli` 汇总后跳过。有图无字的「显影」「天眼」类图集仍保留。
4. **`<img>` 没有 `alt`** —— 加载失败时是一片空白，无障碍也差。用图注文本兜底即可。
   > **已做**：图注 → alt；无图注 → `"配图"`；装饰图（「财」字标志）→ `alt=""` 让屏幕阅读器跳过。78 张图 0 缺失。
5. **`nav.xhtml` 没有 `landmarks`** —— 加上 `epub:type="landmarks"`（cover / toc / bodymatter）能让部分阅读器的「跳转到」菜单更好用。
   > **已做**：填 `book.guide` + 传 `epub3_landmark` 选项。同时显式 `epub2_guide: False` —— ebooklib 默认会把已废弃的 OPF `<guide>` 也写进去。
6. **章末只有「回到目录」** —— 加「上一篇 / 下一篇」能明显改善连续阅读。
   > **已做**：页脚从 `scrape_article` 挪到 `create_epub`（只有它知道章节序号与总数），首末篇各少一个链接避免死链。
7. **`_truncate_after_end_marker` 之外，`watermark_kws` 那轮也是「关键词 in 整段」** —— 目前有 `len(text) < 180` 兜底，风险可控，但同样建议改成叶子块判断。
   > **已做**：抽成 `text.strip_watermarks()`，与文末截断共用「只认叶子块」的规则。回归测试构造了「容器里含水印段 + 正文段」的场景。

---

## 建议的推进顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| **第 1 批（半天）** | P0-1 截断、P0-2 gitignore、P1-1/P1-2 日期、P1-3 重复封面、P1-9 md5 | 全是内容/元数据正确性，改动局部，可加回归测试 |
| **第 2 批（半天）** | P1-4 路由、P1-5 图片去重、P1-6 重试、P1-7 文件名、P1-8 首段缩进 | 性能与稳定性，可离线验证 |
| **第 3 批（1 天）** | P2-1 CI、P2-2 覆盖率口径、P2-3 conftest、P2-4 CSS 外置 | 让后续每次改动都有真实门禁兜底 |
| **第 4 批（1~2 天）** | P2-5 拆模块、P2-6 打包、P2-8 cookie 复用 | 结构性改造，做完覆盖率能上一个台阶 |
| 随时 | P2-9 文档、P3 体验项 | 低风险 |

> 第 3 批建议排在第 4 批之前：先把门禁修好，再动结构，否则大改没有保护网。
