# AGENTS.md — 财新周刊 EPUB 下载器

> **通用规范不在此重复**：六条行为准则（先思后行 / 外科手术式修改 / Python 类型注解 /
> 异常处理 / 禁泄漏凭据 / 禁跨项目重构）、`uv` 强制、一文件一 commit、PR 合入、worktree 强制、
> 命令加 `rtk` 前缀，全部沿用 `/Code/AGENTS.md`（工作区级规范，不在本仓库内）。
> **本文只写本项目特例与踩过的坑。**

一句话：把已订阅的《财新周刊》抓成本地 EPUB。重点是**排版质量**与**不丢内容**，不是"能跑就行"。

## 环境约束（强制）

- **包管理**：`uv sync --dev` / `uv add <pkg>`，禁止 `pip`、`python -m pip`（本项目没有 requirements.txt）
- **运行脚本**：`uv run python main.py`，禁止裸 `python`
- **Python 3.12**（`.python-version`）；`[tool.uv] package = false` —— 脚本项目，不是可安装库，没有 `src/`
- **playwright 与 questionary 只在 `cli.main()` 函数体内延迟导入**，`import caixin.*` 不得有重副作用（测试依赖这一点）

## 常用命令

| 任务 | 命令 |
| --- | --- |
| 安装依赖 | `uv sync --dev` + `uv run playwright install chromium` |
| 交互抓取（选期 / 最新期） | `uv run python main.py` |
| Cookie 有效性自检（真实访问财新） | `uv run python test_caixin.py` |
| 全部测试 | `uv run pytest -q` |
| 测试 + 覆盖率（**唯一能刷新 `.coverage` 的方式**） | `uv run pytest --cov --cov-report=term-missing` |
| Lint | `uv run ruff check .` |
| 类型检查 | `uv run mypy main.py caixin/` |
| 安全扫描（bandit 不在 dev 依赖里） | `uv tool run bandit -r caixin/ main.py -q -ll` |
| 依赖审计 | `uv run --with pip-audit pip-audit` |
| 改排版不重抓（重放后处理） | `uv run python /tmp/rerender_epub.py <源.epub> <目标.epub>` |

## 项目结构

```
main.py                  # 薄入口，只做 asyncio.run(cli.main())
caixin/
  config.py              # 常量（并发、字数下限、图片阈值）、Rich console
  text.py                # 纯后处理：标题清洗、硬缩进、水印、文末截断、日期
  images.py              # ImageManager：下载、分级、缩放、嵌入
  fetcher.py             # 目录解析、正文抓取、资源拦截（本项目最脆的部分）
  epub_builder.py        # EPUB 组装：封面/目录页/章节/nav/页脚
  cookies.py             # cookies.json → Playwright 格式
  cli.py                 # 交互选期与流程编排
style/default.css        # 排版样式表（独立文件，可单独 diff/lint）
tests/                   # 纯单元测试，不启浏览器；fakes.py 是离线 Playwright 替身
docs/                    # 评审报告等长文档
.github/workflows/ci.yml # 四道门禁 + 依赖审计
test_caixin.py           # Cookie 自检（`__test__ = False`，不被 pytest 收集）
```

`download/`、`cookies*.json`、`*.epub`、`.worktrees/`、`.workbuddy-ai/` 均在 `.gitignore` 内。

## 架构与数据流

```
cookies.json → load_cookies() → get_toc() → process_one_issue()
    → scrape_article()  ×N（Semaphore(ARTICLE_CONCURRENCY=2)，每期 ≤ MAX_ARTICLES_PER_ISSUE=35）
    → ImageManager → create_epub()
```

- **正文**：拦截 `checkAuthByIdJsonp?type=0` 的 JSONP 响应，与最终 DOM 比长度取更长（Cookie 失效时 DOM 只有付费墙片段）
- **章末页脚、`.page` 版心壳只由 `create_epub` 生成** —— 只有它知道章节序号与总数；`scrape_article` 只产出 `article-header + content-body` 骨架
- **栏目必须三段贯通**（断一段就静默平铺、不报错）：
  1. `extract_article_links()` 顺序遍历记录栏目名（`SECTION_HEADING_CLASSES = ("magIntrotit", "reportTit")` 只出现一次，作用于其后所有链接）
  2. `scrape_article(..., section=)` 原样返回 `section`，并用它再剥一次文章页 `<h1>` 前缀
  3. `group_by_section()` 按 `article["section"]` 分组（有序、容忍不连续）

## 质量门禁（与全局的差异）

全局策略是「全局宽松、项目收紧」，本项目在两处收紧，**不要按全局默认改回去**：

| 项 | 全局默认 | 本项目 | 原因 |
| --- | --- | --- | --- |
| 覆盖率阈值 | `fail_under = 85` | **90**（实测 97.58%） | 抓取链路已可离线测，无需留缓冲 |
| mypy | `\|\| true` 软失败 | **硬失败** | 类型错误曾掩盖真实 bug，不留后门 |

- CI（`.github/workflows/ci.yml`，单个 `test` job）在 `push: main` 与 `pull_request: main` 都触发，**四道门禁都不带 `|| true`**
- ⚠️ **`pytest -q` 不刷新 `.coverage`**：随后 `uv run coverage report` 会读到旧数据（曾假报 88% 并因 `fail_under=90` 失败）。要覆盖率就必须带 `--cov`
- ⚠️ **本机沙箱里 `tmp_path` 会炸**（`EEXIST` 被改写成 `PermissionError`）：测试用自建 `mkdtemp` 夹具，见 `tests/test_cli.py::workdir`

## 排版铁律（动 CSS 前必读）

1. **缩进只由 CSS 负责**。财新每段行首两个 `\u3000`，不在 CSS 可折叠空白集合里 → 抓取阶段必须 `strip_hard_indent`（段内保留），否则叠加 `text-indent: 2em` 会变成缩进 4 字
2. **版心内边距挂 `.page`，不挂 `body`**（挂 body 会让封面也留白边）：
   `.page { margin: 0 auto; padding: 0 max(6%, 1.5em); max-width: 33em }` —— `33em = 660px` 是行长上限（≈33 汉字/行）。
   取正文片段的工具**不能丢 `.page` 壳**；封面用 `.cover-page`（不吃 `max-width`）
3. **尺度体系**：`body { font-size: 1.25em }` = 20px 基准，所有字号折算回 20px 后**必须是 3 的倍数**（`tests/test_create_epub.py::TestEmScale` 锁死）。
   字号用 `em`；行高用**无单位倍数**；间距/缩进/圆角用 `em`；发丝线是唯一保留的 `px`；**禁用 `rem`**（ADE/RMSDK 不支持）。
   `font-size` 的 em 相对父元素，`margin/padding/max-width` 的 em 相对自身会复利 —— 同样 12px 段距，20px 正文写 `0.6em`、12px 图注写 `1em`。
   `text-indent: 2em` 是语义值（两个字符宽），已从网格校验中排除
4. **属性清洗必须单独清根节点**：`find_all(True)` **不含自身**。曾因此让正文容器带着 46KB base64 背景 PNG，整期重复 23 次 ≈1.4MB
5. 其他：`preserved_classes` 里的类必须在 CSS 里有规则（`lead`/`quote` 曾保留却无样式）；`EpubNav.is_linear` 显式 `False`；中文 `em/i` 不斜体；品牌色 `#1f286f`；`.article-figure { width: fit-content }` 让图注线与图左右对齐

## 已修复的静默故障（改这块前必读）

共同点：**不报错**。内容少一段、样式没生效、整篇消失，日志里只有一行 dim 文字。

| 现象 | 根因 | 现在的契约 |
| --- | --- | --- |
| 全书样式从未生效 | ebooklib 用 book 模板重建文档，只搬 `<body>`，content 里的 `<link>` 被扔掉 | 必须 `ch.add_link(...)`；验证要解包看内容文档 `<head>` 是否引用它，不能只看 OPF |
| 正文第一页重复两遍 | `checkAuthByIdJsonp` 是**累积型**，第二次响应 = 第 1 页 + 第 2 页 | 取最长且校验其余均为其子串，禁止 `"".join(api_parts)` |
| 文章只有第一页 | 页面未就绪时点「余下全文」静默失败 | 点击前后比 `page.inner_text("body")` 长度，失败则 `safe_goto` 重试 |
| 装饰图变成整页大图 | 图片 URL 是协议相对 `//host/path`，`images` 字典与调用方查询用了不同形式 | 一律经 `normalize_url()` 规范化后再查 |
| **整篇文章无声消失** | 同一 `cximg` / `article_img_container` 里有**多张** `<img>`（展开全文后出现），逐个 `replace_with` 容器 → 第二张的目标已脱离树，bs4 抛错被 `except` 吞掉 | 按容器分组，整组替换成一个装多张 `figure` 的 `div`；组内单图失败只丢它自己；替换本身也包 try/except（宁可少图不丢文） |
| 目录标题带「最新封面报道｜」 | 期页编辑习惯性加「最新」引导词 | `strip_section_prefix` 同时剥「最新」+ 栏目名 |
| 正文整篇被清零 | 旧截断逻辑用「关键词 in `get_text()`」在 `find_all` 结果里找标志，祖先节点整体命中 | `find_end_marker` 四重限制：只认叶子块、必须以关键词开头、其后不能有长段落（`MAX_TAIL_BLOCK_LEN=60`）、取最后一个；`_drop_from` 只删祖先的后续兄弟 |

## 测试体系（别再说"抓取链路没法测"）

- `tests/fakes.py` 是**离线 Playwright 替身**：`FakePage/FakeLocator/FakeResponse/FakeContext/FakeBrowser/FakeChromium`，只实现被用到的方法，等待全是空操作。
  `FakePage.emit()` 可造「读响应体时抛异常」；`FakeContext.created_pages` 用来断言 worker 页是否关闭
- `cli.main()` 的接缝是**函数内延迟导入**：`monkeypatch.setitem(sys.modules, "questionary", …)` / `"playwright.async_api"` 即可替换
- `asyncio.sleep` 用 `FastAsyncio` 代理替换 `caixin.cli.asyncio`，**不要全局 patch**（会波及事件循环本身）
- 两个点击计数器不能合并：`FakePage.clicked`（点「余下全文」，locator.click）vs `selector_clicks`（`page.click(".xsjMore, a.more")`）

## 验证纪律（本文件最重要的一条）

**单测全绿 ≠ 真实抓取可用。** 夹具会默认满足你要验证的前提，把断点挡在视野外：

- 栏目断链时 240+ 测试全绿，因为夹具自带 `section` 字段 → **改数据流（谁传给谁）必须跑一次真实抓取验收**
- 多图容器 bug 用 curl / 静态 HTML 复现不出来（未展开时只有 1 张图）→ **必须在真浏览器里点「余下全文」**
- 全量比对期页链接前先确认期页仍在线（2026 第 1 期已被财新下线，302 → 404，会被误报成漏抓）
- 改排版不必重抓：解包 EPUB 重放后处理再打包（不跑水印/截断，字数会有微小差异）

## 凭据与产物

- `load_cookies()` 只读 `name/value/domain/path/sameSite`，**忽略 `expirationDate`**（按 session cookie 处理）
- 换 Cookie：登录 weekly.caixin.com → 浏览器插件导出 JSON → 覆盖 `cookies.json` → `uv run python test_caixin.py`
- **绝不读取 / 修改 / 提交** `cookies.json` 及其任何变体（`.bak.*` 里同样是真实 token，所以 `.gitignore` 必须同时写 `cookies.json`、`cookies.json.*`、`cookies*.json`）、`download/`、`*.epub`

## Git / PR / CI（本项目特例）

继承 `/Code/AGENTS.md` 的一文件一 commit、PR 合入、合并后删分支、worktree 强制（`.worktrees/<branch-slug>/`）。项目特例只有两条：

- **用 merge commit，不用 squash**（历史 PR #1–#4 都是 merge commit；`gh pr merge <n> --merge --delete-branch`）
- **提交信息双语**：英文 subject（`fix:` / `feat:` / `docs:` / `refactor:` / `ci:`）+ 中文正文，正文写清**根因与验证方式**

**每个 commit 都要能跑通**（`git bisect` 走到任何一点都该是绿的）：重构与配套测试改动必须同一个提交。

## 已知问题（未修）

1. `get_toc` 的容器列表含 `.ri` 右栏：期页不触发（只有 `.main`），首页有串入侧栏链接的理论风险
2. `questionary.checkbox` 把导航项与文章勾选混在同一列表；`MORE` 分支失败时不推进 offset
3. 2026 第 40 期成品仍带「最新」标题前缀（修复前抓的），需重抓该期才干净
4. 财新首页偶发数据错误（第 28 期链接写成 `cw116`，少末位 `5`），需人工核对后单独补抓
5. 目录条目用的是文章页 `<h1>`，比期页标题长得多；若要让目录更清爽，可让 `scrape_article` 优先用 `default_title`，缺失时再回落到 `h1`

## 常见改动指引

- **加/改排版** → 只动 `style/default.css`，跑 `tests/test_create_epub.py` 看 `TestEmScale`，再用 `rerender_epub.py` 目测，不必重抓
- **财新改版导致抓不到** → 改 `fetcher.py` 选择器，配 `tests/test_fetcher.py` 用例，然后**实跑一期**验收
- **图片异常** → 看 `ImageManager._prepare()` 的三档阈值（`MIN_IMAGE_DIM=8` / `MARKER_IMAGE_DIM=120` / `MAX_IMAGE_WIDTH=1600`）与 `preserved_classes`
- **文字被截断或多余** → `text.py` 的 `strip_watermarks` / `truncate_after_end_marker`，注意只认叶子块
