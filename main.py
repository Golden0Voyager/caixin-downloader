# /// script
# dependencies = [
#   "playwright",
#   "beautifulsoup4",
#   "ebooklib",
#   "pillow",
#   "questionary",
#   "rich",
# ]
# ///

"""财新周刊 EPUB 下载引擎 —— 命令行入口。

实现按职责拆在 caixin/ 包里：

    config.py        全局常量、终端输出对象
    text.py          纯文本 / HTML 后处理（标题、缩进、截断、日期）
    images.py        图片下载、分级与嵌入
    fetcher.py       页面抓取（目录、正文、资源拦截）
    epub_builder.py  EPUB 组装
    cli.py           交互选期与流程编排
    style/default.css  排版样式表

用法不变：`uv run python main.py`
"""

import asyncio

from caixin.cli import main

if __name__ == "__main__":
    asyncio.run(main())
