"""财新周刊 EPUB 下载引擎的内部实现。

入口仍是仓库根目录的 main.py；这里按职责拆开，便于单独测试。
"""

from .config import VERSION

__all__ = ["VERSION"]
