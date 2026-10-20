# CLAUDE.md

本项目的完整 agent 指南见 **[AGENTS.md](AGENTS.md)**（唯一事实来源，含环境约束、数据流、
已修复的静默故障清单、排版铁律、测试体系与验证纪律）。

@AGENTS.md

---

## 三条红线（从这里先记住）

1. **只用 `uv`**：`uv sync --dev` / `uv run python …`，禁止裸 `pip` / `python`。
2. **改数据流必须实跑验收**：单测会因夹具自带前提而全绿，真实抓取才是判据。
3. **绝不提交** `cookies*.json`、`download/`、`*.epub`、`.workbuddy-ai/`。
