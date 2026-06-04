## ⚠️ 环境约束（强制）

- **包管理器**：`uv pip install <pkg>`（禁止 `pip` / `python -m pip`）
- **运行脚本**：`uv run python <script>.py`（禁止直接 `python`）

---

# CLAUDE.md - Caixin Downloader

This file provides guidance to Claude Code when working with this repository.

## 项目概述

财新周刊全自动 EPUB 下载引擎 — 将已订阅的《财新周刊》转换为排版精美的离线 EPUB 电子书。

## 快速开始

```bash
uv pip install -r requirements.txt
uv run playwright install chromium

# 获取财新登录 Cookie
# 1. 浏览器访问 https://weekly.caixin.com/
# 2. 登录账户
# 3. 使用 EditThisCookie (Chrome) 或 Cookie-Editor (Firefox) 导出为 JSON
# 4. 保存为 cookies.json

# 验证 Cookie
uv run python test_caixin.py

# 运行下载
uv run python main.py
```

## 项目结构

```
caixin_downloader/
├── main.py              # 主程序（交互式选择期刊 → 下载 → EPUB 打包）
├── test_caixin.py       # Cookie 有效性测试
├── cookies.json         # 登录 Cookie（.gitignore 忽略）
├── download/            # 生成的 EPUB 文件输出目录
├── requirements.txt     # Python 依赖
└── pyproject.toml       # 项目配置
```

## 核心概念

### Cookie 生命周期
- 有效期：数天到数周
- 失效后：重新导出覆盖 `cookies.json`
- 建议：每周检查一次有效性

### EPUB 生成流程
1. **发现最新期号**：爬取 weekly.caixin.com 探测最新期刊
2. **下载文章**：并发获取各篇文章内容 + 封面图片
3. **处理图片**：离线嵌入所有图片到 EPUB
4. **排版优化**：1.85 倍行高、两端对齐、首段去缩进（Kindle 友好）
5. **打包 EPUB**：包含目录页、过渡页、完整文章正文

### 依赖说明
- **requests**：HTTP 请求（带 Cookie）
- **Playwright**：浏览器自动化（Chromium，用于 JavaScript 渲染页面）
- **lxml / BeautifulSoup**：HTML 解析
- **EPUBlib / ebooklib**：EPUB 打包和写入
- **Pillow**：图片处理（缩放、格式转换）
- **Rich**：终端界面（进度条、彩色输出）

## 开发注意事项

- 财新 HTML 结构变化可能导致爬虫失效 → 需要更新 CSS 选择器
- Cookie 失效时会返回登录页面 HTML → 解析会异常，`test_caixin.py` 可快速定位问题
- 图片 URL 有时包含 `redirect=true` 重定向参数 → 需要处理 302/301 跳转
- EPUB 目录生成顺序影响阅读器中的排序 → 必须按发布时间排序文章
- 中文界面，所有用户可见文本使用中文
