## ⚠️ 环境约束（强制）

- **包管理器**：`uv sync` / `uv add <pkg>`（禁止 `pip` / `python -m pip`；本项目**没有** `requirements.txt`）
- **运行脚本**：`uv run python <script>.py`（禁止直接 `python`）
- **测试**：`uv run pytest -q`；**静态检查**：`uv run ruff check .`、`uv run mypy main.py caixin/`

---

# CLAUDE.md - Caixin Downloader

This file provides guidance to Claude Code when working with this repository.

## 项目概述

财新周刊全自动 EPUB 下载引擎 — 将已订阅的《财新周刊》转换为排版精美的离线 EPUB 电子书。

## 快速开始

```bash
uv sync                       # 安装依赖
uv run playwright install chromium

# 获取财新登录 Cookie
# 1. 浏览器访问 https://weekly.caixin.com/
# 2. 登录账户
# 3. 使用 EditThisCookie (Chrome) 或 Cookie-Editor (Firefox) 导出为 JSON
# 4. 保存为 cookies.json

# 验证 Cookie（会真实访问财新，不是单元测试）
uv run python test_caixin.py

# 运行下载
uv run python main.py
```

## 项目结构

```
caixin_downloader/
├── main.py                  # 入口（十几行，只负责 asyncio.run(cli.main())）
├── caixin/                  # 实现
│   ├── config.py            #   全局常量、终端 console 对象
│   ├── text.py              #   纯文本/HTML 后处理（标题、缩进、截断、日期）
│   ├── images.py            #   ImageManager：下载、分级、嵌入
│   ├── fetcher.py           #   页面抓取（目录、正文、资源拦截）
│   ├── epub_builder.py      #   EPUB 组装
│   ├── cookies.py           #   cookies.json → Playwright 格式
│   └── cli.py               #   交互选期与流程编排
├── style/default.css        # 排版样式表（独立文件，可单独 diff/lint）
├── tests/                   # 单元测试（不依赖 Playwright 浏览器）
├── test_caixin.py           # Cookie 有效性自检（`__test__ = False`，不被 pytest 收集）
├── cookies.json             # 登录 Cookie（.gitignore 忽略）
├── download/                # 生成的 EPUB 输出目录
└── pyproject.toml
```

## 核心概念

### Cookie 生命周期
- 有效期：数天到数周
- 失效后：重新导出覆盖 `cookies.json`
- 建议：每周检查一次有效性
- 解析逻辑在 `caixin/cookies.py:load_cookies()`，`main.py` 与 `test_caixin.py` 共用

### EPUB 生成流程
1. **发现最新期号**：爬取 weekly.caixin.com 探测最新期刊
2. **下载文章**：并发获取各篇文章内容 + 封面图片（`ARTICLE_CONCURRENCY = 2`）
3. **处理图片**：离线嵌入所有图片到 EPUB
4. **排版优化**：20px 基准字号、1.8 倍行高、两端对齐、首段去缩进
5. **打包 EPUB**：封面 → 目录页 → 各章 → nav（non-linear）

### 依赖说明
- **Playwright**：浏览器自动化（Chromium）。注意它和 `questionary` 都只在 `cli.main()` 内部导入，
  这样 `import caixin.*` 没有重副作用，测试不需要打桩
- **BeautifulSoup**：HTML 解析（项目**不用** `lxml`、**不用** `requests`）
- **ebooklib**：EPUB 打包
- **Pillow**：图片缩放与格式转换
- **questionary / Rich**：终端交互与输出

### 排版尺度（改动 CSS 前必读）
- 基准 `body { font-size: 1.25em }` = 20px，全表用 `em`
- 字号阶梯 `1.8 / 1.5 / 1.2 / 1.05 / 1 / 0.75 / 0.6`，折算回 px 必须是 3 的倍数
- `tests/test_create_epub.py::TestEmScale` 会校验这两条，改坏了会直接红
- `text-indent: 2em` 是「两个字符宽」的语义值，**不在**网格校验范围内

## 开发注意事项

- 财新 HTML 结构变化可能导致爬虫失效 → 需要更新 CSS 选择器
- Cookie 失效时会返回登录页面 HTML → 解析会异常，`test_caixin.py` 可快速定位问题
- 图片 URL 有时包含 `redirect=true` 重定向参数 → 需要处理 302/301 跳转
- EPUB 目录生成顺序影响阅读器中的排序 → 必须按发布时间排序文章
- 中文界面，所有用户可见文本使用中文
- **Playwright 路由按注册倒序匹配**：后注册的 `**/*` 会先命中，所以资源拦截只能有一个 handler
- **`find_all()` 不含节点自身**：清理根节点属性必须单独写一行
