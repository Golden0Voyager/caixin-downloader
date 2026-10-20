"""全局常量与终端输出对象。所有可调参数集中在这里。"""

import re
from pathlib import Path

from rich.console import Console

COOKIES_FILE = "cookies.json"
VERSION = "2.5"

# 图片处理阈值
MIN_IMAGE_DIM = 8        # 长边小于此值视为追踪像素，丢弃
MARKER_IMAGE_DIM = 120   # 长边小于此值视为行内装饰图（文末「财」字标志、署名图标），保留但按小尺寸展示
MAX_IMAGE_WIDTH = 1600   # 超过此宽度等比缩小
JPEG_QUALITY = 85
IMAGE_TIMEOUT_MS = 15000
RETRY_BACKOFF_SECONDS = 1.0   # 第 n 次重试前等 n 倍（测试里会置 0）

# 抓取并发：太低会慢，太高容易触发限流
ARTICLE_CONCURRENCY = 2
MAX_ARTICLES_PER_ISSUE = 35
EARLIEST_ISSUE_YEAR = 2020

# 正文短于此长度**且一张图都没有**时判为空章（图集/视频类栏目，或 Cookie 失效
# 只抓到摘要），不入书。有图但无字的情况保留 —— 「显影」「天眼」这类栏目就是图集。
MIN_ARTICLE_CHARS = 80

# 期页上的栏目标题：`.reportTit` 是封面报道，`.magIntrotit` 是其余各栏。
# 栏目名只出现一次、作用于其后所有文章链接，所以抽取时必须按文档顺序记住它。
SECTION_HEADING_CLASSES = ("magIntrotit", "reportTit")

# 资源拦截：这些 host/路径下的请求与正文无关，直接 abort
BLOCKED_URL_MARKERS = ("analytics", "adserver", "fonts", "hm.baidu.com")
BLOCKED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 文件名里不能出现的字符（Windows 最严格，Unix 也不该出现 /）
FILENAME_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 财新正文的每个段落都以两个全角空格（U+3000）做「硬缩进」。CSS 里又设了
# text-indent: 2em，两者叠加会让首行实际缩进 4 个字符宽。缩进必须只由 CSS 负责，
# 所以要把这些硬编码的空白剥掉。
HARD_INDENT_CHARS = "\u3000\u0020\t\u00a0"

# 内联 style 里出现这些内容一律丢弃：财新在正文容器上挂了一张 62KB 的
# base64 背景纹理（整期重复 23 次 = 1.4MB），既撑大文件又可能被渲染到文字后面。
DROP_STYLE_MARKERS = ("data:image", "base64,")
MAX_INLINE_STYLE_LEN = 300   # 超长内联样式一律丢弃，避免残留的整段 CSS

# 文末标志：这些词出现时，其后的内容（相关推荐 / 视频 / 版权声明）不属于正文。
TRUNC_KEYWORDS = (
    "相关视频", "视频说明", "更多报道详见", "相关阅读",
    "延伸阅读", "热门文章", "推荐阅读", "对此文亦有贡献",
)
TRUNC_BLOCK_TAGS = ("p", "div", "section", "h2", "h3", "h4", "figcaption")
# 出现这些子元素就说明该标签是「容器」而不是「叶子块」
_NESTED_BLOCK_TAGS = (
    "p", "div", "section", "h1", "h2", "h3", "h4",
    "figure", "table", "ul", "ol", "blockquote",
)
MAX_END_MARKER_LEN = 40   # 文末标志本身很短；长于这个长度的块是正文，不是标志
MAX_TAIL_BLOCK_LEN = 60   # 标志之后只剩短标题/链接；出现长段落说明那是正文小标题
_LEADING_NOISE = "　 \t\u00a0【】[]（）()《》<>·—－-:：|｜"

# 水印 / 版权声明：整段文本提到这些词、且够短，就整块删掉。
# 同样只认叶子块 —— 否则包着两段正文的 <div> 会被一起删掉。
WATERMARK_KEYWORDS = (
    "财新网版权", "翻录必究", "财新版权", "翻版必究",
    "此材料受版权保护", "摘录来自", "本文为财新",
)
MAX_WATERMARK_LEN = 180
LONG_NUMBER_RE = re.compile(r"^\d{10,}$")   # 纯长数字串是防盗码，不是正文

console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STYLE_FILE = PROJECT_ROOT / "style" / "default.css"
