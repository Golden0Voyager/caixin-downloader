"""EPUB 组装：元数据、封面、目录页、章节、导航。"""

import html as html_escape
import uuid
from datetime import datetime
from pathlib import Path

from ebooklib import epub
from rich.panel import Panel

from .config import STYLE_FILE, console
from .text import clean_title, format_cn_date, parse_iso_date


def load_stylesheet():
    """读取排版样式表（UTF-8）。"""
    return STYLE_FILE.read_text(encoding="utf-8")


def group_by_section(articles):
    """按栏目把章节下标分组，返回 `[(栏目名, [下标…])]`。

    栏目名来自期页（`get_toc` 填的 `section`），同一栏目在页面上是连续的一段，
    所以分组基本等价于「切成几块」。用有序字典而不是比较相邻元素，是为了容忍
    栏目不连续的情况：那样也能合成一组，而不是在目录里出现两个同名小标题。

    没有栏目信息（首页、手动输入期号、离线重排）时整本书就是一组，`name` 为空，
    目录页退回成原来那张平铺列表。
    """
    order, buckets = [], {}
    for index, article in enumerate(articles):
        name = (article.get("section") or "").strip()
        if name not in buckets:
            buckets[name] = []
            order.append(name)
        buckets[name].append(index)
    return [(name, buckets[name]) for name in order]


def _article_footer(index, total):
    """章末翻页条：上一篇 / 目录 / 下一篇。

    首末篇各少一个链接，避免出现指向不存在文件的死链。
    """
    parts = []
    if index > 0:
        parts.append(f'<a class="pager-link pager-prev" href="chapter_{index - 1}.xhtml">← 上一篇</a>')
    parts.append('<a class="back-to-toc" href="contents.xhtml">目录</a>')
    if index + 1 < total:
        parts.append(f'<a class="pager-link pager-next" href="chapter_{index + 1}.xhtml">下一篇 →</a>')
    return '<div class="article-footer">' + "".join(parts) + "</div>"


def create_epub(articles, info, image_manager, filename):
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(info["issue_title"])
    book.set_language('zh-CN')
    book.add_author("财新周刊")

    # 元数据：之前只有 title/language/creator，阅读器的「图书信息」几乎是空的
    #
    # dc:date 必须是 ISO 8601（EPUB 3 硬性要求，写「2026年10月12日」epubcheck 会报
    # ERROR）。内部统一存 ISO，展示时再用 format_cn_date() 转回中文。
    issue_date = info.get("issue_date") or ""
    if not issue_date:
        for pi in info.get("past_issues") or []:
            if pi.get("date"):
                issue_date = pi["date"]
                break
    issue_date = parse_iso_date(issue_date, "") or datetime.now().strftime("%Y-%m-%d")

    book.add_metadata('DC', 'publisher', '财新周刊')
    book.add_metadata('DC', 'date', issue_date)
    book.add_metadata('DC', 'description', f'{info["issue_title"]} · 共 {len(articles)} 篇')
    if info.get("topic"):
        book.add_metadata('DC', 'subject', info["topic"])

    book.add_item(epub.EpubItem(uid="style_default", file_name="style/default.css",
                                media_type="text/css", content=load_stylesheet()))

    for _url, data in image_manager.images.items():
        book.add_item(epub.EpubItem(uid=data["id"], file_name=data["filename"], media_type=data["mime"], content=data["content"]))

    if info.get("cover_data"):
        # create_page=False：封面页由下面的 cover_page.xhtml 自己建。
        # 默认 True 会额外生成一个无样式、且不进 spine 的 cover.xhtml，
        # 与 cover_page.xhtml 在 manifest 里并存 —— 有些阅读器会挑中那个没样式的。
        book.set_cover("images/cover.jpg", info["cover_data"], create_page=False)

    # ebooklib 的 EpubHtml.get_content() 会用 book 模板重建文档：只取 content 的 <body>
    # 子节点，<head> 完全丢弃。所以样式表**必须**通过 add_link() 注册，
    # 写在 content 里的 <link> 会被静默扔掉（曾导致全书的 CSS 从未生效）。
    def _styled(page):
        page.add_link(href="style/default.css", rel="stylesheet", type="text/css")
        return page

    # 1. 创建独立封面页（整页满幅：样式走 .cover-page/.cover-img，不留内边距）
    cover_page = None
    if info.get("cover_data"):
        cover_html = '<html><body><div class="cover-page"><img class="cover-img" src="images/cover.jpg" alt="封面" /></div></body></html>'
        cover_page = _styled(epub.EpubHtml(uid="cover_page", title="封面", file_name="cover_page.xhtml", content=cover_html, lang='zh-CN'))
        book.add_item(cover_page)

    # 2. 创建自定义目录页 (CONTENTS) —— 按期页的栏目分组
    clean_it = html_escape.escape(info["issue_title"].replace("《", "").replace("》", ""))
    toc_html = f'<div class="page toc-container"><div class="toc-header">《{clean_it}》 · 目录</div>'
    toc_html += f'<div class="toc-meta">{format_cn_date(issue_date)}　共 {len(articles)} 篇</div>'
    for name, indexes in group_by_section(articles):
        toc_html += '<div class="toc-group">'
        if name:
            toc_html += f'<div class="toc-group-title">{html_escape.escape(name)}</div>'
        toc_html += '<div class="toc-list">'
        for i in indexes:
            clean_t = html_escape.escape(clean_title(articles[i].get("title"), fallback="（无标题）"))
            toc_html += (f'<a class="toc-item" href="chapter_{i}.xhtml">'
                         f'<div class="toc-item-title"><span class="toc-index">{i + 1:02d}</span>{clean_t}</div></a>')
        toc_html += '</div></div>'

    cp = _styled(epub.EpubHtml(uid="contents", title="目录", file_name="contents.xhtml", content=f'<html><body>{toc_html}</body></html>', lang='zh-CN'))
    book.add_item(cp)

    # 3. 生成文章章节
    total = len(articles)
    spine_chapters = []
    for i, art in enumerate(articles):
        # 章节标题同样要清洗 —— 否则残留的 {{ }} 会带进 nav.xhtml 与阅读器的目录
        ch_title = clean_title(art.get("title"), fallback=f"第 {i + 1} 篇")
        # 版心内边距（左右 5%）统一挂在 .page 包装层，而不是 body ——
        # 这样封面页可以整页满幅，不会被正文的左右内边距顶出白边。
        # 章末翻页条也在这里加：只有 create_epub 知道章节序号与总数。
        body = f'<div class="page">{art["html"]}{_article_footer(i, total)}</div>'
        ch = _styled(epub.EpubHtml(uid=f"chapter_{i}", title=ch_title, file_name=f"chapter_{i}.xhtml",
                                   content=f'<html><body>{body}</body></html>', lang='zh-CN'))
        book.add_item(ch)
        spine_chapters.append(ch)

    # 4. 设置系统导航和阅读顺序
    book.toc = tuple(spine_chapters)
    book.add_item(epub.EpubNcx())
    nav = epub.EpubNav(title="目录")
    # nav 标记为非线性的：它必须留在 spine 里以保持兼容，但不应被翻页读到 ——
    # 否则读完最后一篇会多出一页与自建目录重复的朴素目录。
    nav.is_linear = False
    book.add_item(nav)
    # spine: 封面 → 目录 → 正文章节 → nav（non-linear，翻页时跳过）
    spine = []
    if cover_page: spine.append(cover_page)
    spine.append(cp)
    spine.extend(spine_chapters)
    spine.append(nav)
    book.spine = spine

    # landmarks（EPUB 3 的「导览」导航）：阅读器的「跳转到…」菜单靠它定位封面/
    # 目录/正文起点。ebooklib 只在 book.guide 非空、且传入 epub3_landmark 选项时
    # 才生成这一段。
    # 同时显式关掉 epub2_guide（默认是 True）：它会把 EPUB 2 那套已废弃的
    # OPF <guide> 也写进去，与 landmarks 重复，epubcheck 还会报 deprecation。
    book.guide = []
    if cover_page:
        book.guide.append({"type": "cover", "title": "封面", "href": "cover_page.xhtml"})
    book.guide.append({"type": "toc", "title": "目录", "href": "contents.xhtml"})
    if spine_chapters:
        book.guide.append({"type": "text", "title": "正文", "href": "chapter_0.xhtml"})

    epub.write_epub(filename, book, {
        "epub3_landmark": True,
        "epub2_guide": False,
        "landmark_title": "导览",
    })
    console.print(Panel(f"[bold green]✅ 成功保存至:[/bold green] [cyan]{Path(filename).absolute()}[/cyan]", border_style="green"))
