# Caixin Downloader | 财新周刊全自动下载打包引擎

这是一个基于 Python 的高级自动化工具，专门用于抓取《财新周刊》的付费文章，并将其转化为排版精美、支持离线图片的专业级 EPUB 电子书。

## 📂 项目结构

```text
caixin_downloader/
├── main.py           # 核心引擎 v2.0：负责抓取、图片下载与 EPUB 组装
├── test_caixin.py    # 验证工具：用于测试 cookies.json 的有效性与付费墙穿透
├── cookies.json      # 身份凭证：存储财新网登录后的 Cookie (需手动更新)
└── .venv/            # 由 uv 管理的虚拟环境
```

## 🚀 核心特性 (v2.0)

### 1. 深度自动化抓取
- **双阶探测逻辑**：自动从主页识别最新期号（如 `cw1194`），并跳转至对应的专属目录页进行全量抓取。
- **全量打包**：单期文章抓取量通常为 20~30 篇，包含封面报道、专题、专栏及显影等所有版块。

### 2. 杂志级阅读体验
- **图片离线化**：自动下载文章内所有插图并嵌入 EPUB，支持离线阅读。
- **高级排版样式**：
    - **1.8 倍行高**：显著缓解电子墨水屏或手机阅读的视觉疲劳。
    - **专业对齐**：采用两端对齐（Justify）和首段去缩进的纸质杂志排版规范。
    - **结构化导航**：自定义 `CONTENTS` 目录页，并在其后插入**空白过渡页**，确保在 iPad/Kindle 的双页模式下，目录与正文实现物理隔离。

### 3. 规范化命名
- **自动命名规则**：`YYYY-财新周刊—第XX期YYMMDD.epub`
- **示例**：`2026-财新周刊—第07期260227.epub`

## 🛠 运行指引

项目强制使用 `uv` 进行依赖管理。

### 环境检查与测试
```bash
# 验证 Cookie 是否有效
uv run python test_caixin.py
```

### 执行下载任务
```bash
# 抓取并生成最新一期电子书
uv run python main.py
```

## ⚠️ 维护说明
- **Cookie 更新**：若 `test_caixin.py` 报错提示遭遇付费墙，请在浏览器登录财新网后，使用 EditThisCookie 等插件将最新的 Cookie 导出并覆盖 `cookies.json`。
- **依赖库**：核心依赖包括 `playwright`, `beautifulsoup4`, `ebooklib`。首次运行请确保执行过 `playwright install chromium`。

---
*Last Updated: 2026-02-27 by Gemini CLI*
