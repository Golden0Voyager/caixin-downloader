# /// script
# dependencies = [
#   "playwright",
#   "beautifulsoup4",
#   "ebooklib",
#   "questionary",
#   "rich",
# ]
# ///

import asyncio
import json
import uuid
import re
import os
import time
import hashlib
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from ebooklib import epub
import questionary
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table

COOKIES_FILE = "cookies.json"
VERSION = "2.3"

console = Console()

DISCLAIMER = """
[bold red]免责声明 / Disclaimer[/bold red]

本程序仅供个人学习、研究及测试使用，通过自动化技术辅助获取已订阅内容。
严禁将本程序及生成的电子书用于任何商业用途或大规模传播。
This program is for personal learning, research, and testing purposes only.
Commercial use or large-scale distribution is strictly prohibited.

下载内容的版权归财新传媒 (Caixin Media) 所有。请在下载后 24 小时内删除。
The copyright of the content belongs to Caixin Media. Please delete after 24 hours.
用户需自行承担因不当使用该工具而产生的任何法律责任。
Users shall bear all legal responsibilities arising from improper use of this tool.
"""

async def safe_goto(page, url, retries=3, timeout=60000):
    """带重试机制的页面跳转"""
    for i in range(retries):
        try:
            return await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            if i == retries - 1: raise e
            await asyncio.sleep(2 * (i + 1))

class ImageManager:
    """管理电子书中的图片下载与嵌入"""
    def __init__(self, context):
        self.context = context
        self.images = {}  # url -> {id, bytes, mime, filename}

    async def download(self, url, retries=2):
        if not url: return None
        if url in self.images:
            return self.images[url]["filename"]
        
        # 补全 URL
        if url.startswith("//"): url = "https:" + url
        elif not url.startswith("http"): return None

        for i in range(retries):
            try:
                response = await self.context.request.get(url, timeout=15000)
                if response.status == 200:
                    content = await response.body()
                    if not content: continue
                    
                    # 智能探测扩展名
                    ext = "jpg"
                    if "image/png" in response.headers.get("content-type", ""): ext = "png"
                    elif "image/gif" in response.headers.get("content-type", ""): ext = "gif"
                    elif "image/webp" in response.headers.get("content-type", ""): ext = "webp"
                    else:
                        path_ext = url.split(".")[-1].split("?")[0].lower()
                        if path_ext in ["png", "gif", "webp", "jpeg", "jpg"]: ext = path_ext

                    img_hash = hashlib.md5(url.encode()).hexdigest()
                    filename = f"images/{img_hash}.{ext}"
                    mime = f"image/{'jpeg' if ext in ['jpg', 'jpeg'] else ext}"
                    
                    self.images[url] = {"id": f"img_{img_hash}", "content": content, "mime": mime, "filename": filename}
                    return filename
                break
            except Exception:
                await asyncio.sleep(1)
        return None

async def scrape_article(page, url, image_manager, default_title=None):
    """抓取单篇文章的正文内容并处理图片"""
    try:
        # 设置 API 响应拦截器，捕获付费墙 JS 请求的完整正文
        api_content = {}

        async def intercept_content(response):
            if "checkAuthByIdJsonp" in response.url and "type=0" in response.url:
                try:
                    body = await response.text()
                    api_json = json.loads(body)
                    if api_json.get("code") == 0 and api_json.get("data"):
                        data_str = api_json["data"]
                        cm = re.search(r'resetContentInfo\((.+)\)$', data_str, re.DOTALL)
                        if cm:
                            inner = json.loads(cm.group(1))
                            api_content["html"] = inner.get("content", "")
                except Exception:
                    pass

        page.on("response", intercept_content)
        await safe_goto(page, url)

        # 等待页面 JS 发起 API 请求并完成
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        page.remove_listener("response", intercept_content)

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # 1. 标题提取
        title = "未知标题"
        title_tag = soup.select_one("h1, .wiv-title, .art-title, .title, #conTit h1")
        if title_tag:
            title = title_tag.get_text(strip=True)
        elif default_title:
            title = default_title
        title = re.sub(r"\{\{.*?\}\}", "", title).strip()

        # 2. 作者提取
        author_info = ""
        author_selectors = [".author", ".artInfo", "#author_top", ".editor-name", ".media-name"]
        for s in author_selectors:
            tag = soup.select_one(s)
            if tag:
                author_info = tag.get_text(separator=" ", strip=True)
                if "本文来源于" in author_info: author_info = ""
                if author_info: break

        # 3. 正文提取 — 优先使用拦截到的 API 全文
        if api_content.get("html"):
            content_div = BeautifulSoup(f"<div>{api_content['html']}</div>", "html.parser").div
        else:
            # 回退：从页面 DOM 提取
            content_div = soup.find(id="Main_Content_Val") or soup.select_one(".text, .article-content, article, #main_content, .art-content")
            if not content_div: return None

        # 4. 清理无效元素
        blacklist = [
            "script", "style", "canvas", ".share", ".ad", ".bottom-adv", 
            ".media-box", ".aitt", ".copyright", ".watermark", ".mask", 
            ".hide", ".hidden", ".artInfo-box", ".read-all", ".pay-all", 
            ".copyright-box", ".related-articles", ".recommend", ".keyword"
        ]
        for selector in blacklist:
            for tag in content_div.select(selector): tag.decompose()
            
        # 5. 水印与垃圾文本清理
        for tag in content_div.find_all(True):
            text = tag.get_text(strip=True)
            watermark_kws = ["财新网版权", "翻录必究", "财新版权", "翻版必究", "此材料受版权保护", "摘录来自", "本文为财新"]
            if (any(k in text for k in watermark_kws) and len(text) < 180) or re.match(r"^\d{10,}$", text):
                tag.decompose(); continue
            
            # 清理空的或只有空白字符的标签 (非自闭合标签)
            if not text and not tag.find_all() and tag.name not in ["img", "br", "hr"]:
                tag.decompose(); continue

        # 6. 图片处理 (支持 CXIMG 自定义标签 + 懒加载)
        img_tags = content_div.find_all("img")
        img_urls = []
        img_captions = []
        img_containers = []  # 需要清理的原始容器

        for img in img_tags:
            src = img.get("data-src") or img.get("data-original-src") or img.get("src")
            img_urls.append(src)

            cap_text = ""
            container = None

            # 策略1: CXIMG / article_img_container 结构
            cximg = img.find_parent("cximg") or img.find_parent(attrs={"class": "article_img_container"})
            if cximg:
                container = cximg
                talk = cximg.find(attrs={"class": "article_img_talk"})
                if talk:
                    cap_text = talk.get_text(strip=True)

            # 策略2: DL/DD 结构
            if not cap_text:
                dl = img.find_parent("dl")
                if dl:
                    container = container or dl
                    dd = dl.find("dd")
                    if dd:
                        cap_text = dd.get_text(strip=True)

            # 策略3: 兄弟节点（从 img 或其最近的块级父容器开始向后搜索）
            if not cap_text:
                search_base = img.parent if img.parent and img.parent.name in ["a", "div", "p"] else img
                nxt = search_base.find_next_sibling()
                caption_classes = [
                    "imageText", "caption", "artInfo", "pic_content", "source",
                    "img-caption", "pic_info", "article_img_talk"
                ]
                for _ in range(3):
                    if nxt and nxt.name in ["p", "div", "span", "figcaption"]:
                        n_cls = nxt.get("class", [])
                        if isinstance(n_cls, str): n_cls = [n_cls]
                        if any(c in n_cls for c in caption_classes) or nxt.get("id") == "pic_info":
                            cap_text = nxt.get_text(strip=True)
                            if not container: nxt.decompose()
                            break
                    if nxt: nxt = nxt.find_next_sibling()

            img_captions.append(cap_text)
            img_containers.append(container)

        if img_urls:
            local_paths = await asyncio.gather(*[image_manager.download(u) for u in img_urls])
            for idx, img in enumerate(img_tags):
                path = local_paths[idx]
                if path:
                    # 构建干净的 figure 结构
                    figure = soup.new_tag("figure", attrs={"class": "article-figure"})
                    new_img = soup.new_tag("img", attrs={"src": path, "class": "article-img"})
                    figure.append(new_img)

                    if img_captions[idx]:
                        fcap = soup.new_tag("figcaption", attrs={"class": "article-caption"})
                        fcap.string = img_captions[idx]
                        figure.append(fcap)

                    # 替换：如果有容器则替换容器，否则替换 img 本身
                    target = img_containers[idx] if img_containers[idx] else img
                    target.replace_with(figure)
                else:
                    # 下载失败：删除容器或 img
                    target = img_containers[idx] if img_containers[idx] else img
                    target.decompose()

        # 7. 属性清洗 (移除所有 ID 和不必要的 Class)
        preserved_classes = {"lead", "quote", "author-bar", "content-body", "article-figure", "article-img", "article-caption"}
        for tag in content_div.find_all(True):
            if tag.name == "img": continue
            c_cls = tag.get("class", [])
            if isinstance(c_cls, str): c_cls = [c_cls]
            n_attrs = {}
            valid_classes = [c for c in c_cls if c in preserved_classes]
            if valid_classes: n_attrs["class"] = " ".join(valid_classes)
            tag.attrs = n_attrs

        # 8. 组装 HTML
        article_html = f"<div class='article-header'><h1 class='article-title'>{title}</h1>"
        if author_info: article_html += f"<div class='author-bar'>{author_info}</div>"
        article_html += f"</div><div class='content-body'>{str(content_div)}</div>"
        article_html = re.sub(r"\{\{.*?\}\}", "", article_html)
        article_html += "<div class='article-footer'><a href='contents.xhtml' class='back-to-toc'>回到目录 / Back to TOC</a></div>"
        
        return {"title": title, "html": article_html, "url": url}
    except Exception as e:
        console.log(f"[dim]抓取异常 ({url}): {e}[/dim]")
        return None

async def get_toc(page, issue_url=None):
    if issue_url: await page.goto(issue_url, wait_until="networkidle", timeout=60000)
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    cover_url = None
    issue_id = None
    
    # 1. 尝试提取封面和当前期 ID
    for s in [".mi img", ".fm img", ".mains img", ".leftCon img", ".cover img"]:
        tag = soup.select_one(s)
        if tag and tag.get("src"): cover_url = tag.get("src"); break
    
    mi_div = soup.select_one(".mi, .fm, .mains")
    if mi_div and mi_div.find("a", href=True):
        m = re.search(r"cw(\d+)", mi_div.find("a")["href"])
        if m: issue_id = m.group(0)
    
    # 2. 提取当前期标题和期号
    issue_no, issue_title = "XX", "《财新周刊》"
    issue_date = datetime.now().strftime("%Y年%m月%d日")
    
    # 尝试寻找包含期数的文本节点
    for text_node in soup.find_all(string=re.compile(r"202\d年第\d+期")):
        raw = text_node.strip().replace("{{", "").replace("}}", "")
        # 提取标题：优先找含“财新周刊”的长字符串
        cm = re.search(r"(《?财新周刊》?\s*\d{4}年第\d+期)", raw)
        if cm:
            issue_title = cm.group(1)
        elif "第" in raw and "期" in raw:
            # 如果没找到带书名的，直接用包含期数的这一行
            issue_title = raw.split("出版日期")[0].strip()
        
        # 提取期号
        nm = re.search(r"第(\d+)期", raw)
        if nm: issue_no = nm.group(1)
        
        # 提取日期
        dm = re.search(r"(\d{4}年\d{2}月\d{2}日)", raw)
        if dm: issue_date = dm.group(1)
        
        # 如果已经成功拿到期号，就不用再找了
        if issue_no != "XX": break

    # 3. 提取历史期刊列表
    past_issues = []
    for li in soup.select(".xsjCon li"):
        text = li.get_text(separator=" ", strip=True).replace("{{", "").replace("}}", "")
        dm, nm = re.search(r"(\d{4}年\d{2}月\d{2}日)", text), re.search(r"年度期号:(\d+)", text)
        p = [p.strip() for p in text.split(" ") if p.strip()]
        a = li.find("a", href=True)
        cwm = re.search(r"cw\d+", a["href"]) if a else None
        if cwm: 
            past_issues.append({
                "id": cwm.group(0), 
                "date": dm.group(1) if dm else "", 
                "no": nm.group(1) if nm else "", 
                "title": p[0] if p else "", 
                "url": a["href"]
            })
    
    # 4. 关键修复：如果当前期 ID 不在历史列表中，则将其置顶
    if issue_id and not any(p["id"] == issue_id for p in past_issues):
        current_year = datetime.now().year
        target_url = f"https://weekly.caixin.com/{current_year}/{issue_id}/"
        
        # 如果当前在首页（issue_url 为空或为 weekly.caixin.com），且没拿到准确期号，去该期页面“偷”一下准确信息
        is_home = not issue_url or "weekly.caixin.com/" in issue_url and issue_url.endswith("/")
        if is_home and (issue_no == "XX" or "《财新周刊》" == issue_title):
            try:
                temp_page = await page.context.new_page()
                await temp_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                temp_html = await temp_page.content()
                temp_soup = BeautifulSoup(temp_html, "html.parser")
                
                # 增强版搜索逻辑
                found_no = False
                current_year_str = str(datetime.now().year)
                
                # 1. 提取期号
                no_candidates = []
                for node in temp_soup.find_all(string=True):
                    t = node.strip()
                    if "第" in t and "期" in t and len(t) < 100:
                        nm = re.search(r"第(\d+)期", t)
                        if nm:
                            num = nm.group(1)
                            if len(num) <= 3: # 过滤掉总期号
                                no_candidates.append((t, num))
                
                if no_candidates:
                    # 优先选带年份的
                    no_candidates.sort(key=lambda x: (current_year_str not in x[0], len(x[0])))
                    issue_no = no_candidates[0][1]
                    issue_title = f"《财新周刊》 {current_year_str}年第{issue_no}期"
                    found_no = True

                # 2. 提取日期 (独立搜索全页面)
                for node in temp_soup.find_all(string=True):
                    t = node.strip()
                    dm = re.search(r"(\d{4})[年-](\d{1,2})[月-](\d{1,2})", t)
                    if dm:
                        issue_date = f"{dm.group(1)}年{int(dm.group(2)):02d}月{int(dm.group(3)):02d}日"
                        if "出版日期" in t: break # 优先用带“出版日期”字样的
                
                await temp_page.close()
            except Exception as e:
                console.log(f"[dim]获取新刊详情失败: {e}[/dim]")

        display_title = issue_title
        if "第" not in display_title and issue_no != "XX": 
            display_title += f" 第{issue_no}期"
        
        past_issues.insert(0, {
            "id": issue_id,
            "date": issue_date,
            "no": issue_no,
            "title": display_title,
            "url": target_url
        })


    # 5. 提取正文文章链接
    links = []
    seen = set()
    containers = [t for t in [soup.select_one(".lf"), soup.select_one(".ri"), soup.select_one(".mi")] if t] or [soup]
    for c in containers:
        for a in c.find_all("a", href=True):
            h = a["href"]
            if ".caixin.com/202" in h and h.endswith(".html"):
                if h not in seen:
                    seen.add(h); t = re.sub(r'\s+', ' ', a.get_text(strip=True)).replace("{{", "").replace("}}", "")
                    if len(t) > 2 and "期号" not in t: links.append({"title": t, "url": h})

    # 6. 提取当期封面主题 (取第一篇文章标题作为主题)
    topic = links[0]["title"] if links else ""
    topic = re.sub(r"^封面报道[｜|：:]\s*", "", topic)

    # 如果当前期的 title 是泛化的刊名，替换为封面主题
    if topic and past_issues:
        for pi in past_issues:
            if pi.get("id") == issue_id and pi.get("title", "").startswith("《财新周刊》"):
                pi["title"] = topic
                break

    return {
        "issue_title": issue_title,
        "issue_no": issue_no,
        "cover_url": cover_url,
        "links": links,
        "past_issues": past_issues,
        "topic": topic
    }

def create_epub(articles, info, image_manager, filename):
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(info["issue_title"])
    book.set_language('zh-CN')
    book.add_author("财新周刊")
    
    style = '''
    body { font-family: "STSong", "Songti SC", "PingFang SC", serif; line-height: 1.85; padding: 0 2%; color: #1a1a1a; text-align: justify; }
    .toc-container { padding: 8% 5%; }
    .toc-header { font-size: 2.2em; border-bottom: 4px solid #000; padding-bottom: 12px; margin-bottom: 40px; font-weight: bold; }
    .toc-cover { width: 100%; max-width: 280px; margin-bottom: 30px; box-shadow: 0 8px 15px rgba(0,0,0,0.1); }
    .toc-list { list-style: none; padding: 0; }
    .toc-item { margin: 18px 0; border-bottom: 1px solid #f0f0f0; padding-bottom: 10px; display: block; text-decoration: none; color: #222; }
    .article-header { margin: 3em 0 4em 0; text-align: center; }
    .article-title { font-size: 2.2em; font-weight: bold; line-height: 1.2; margin-bottom: 0.8em; }
    .author-bar { font-size: 0.85em; color: #888; text-align: center; margin-top: 0.5em; }
    .content-body { font-size: 1.1em; }
    .content-body p { margin: 1.6em 0; text-indent: 2em; }
    .content-body p:first-of-type { text-indent: 0; }
    .article-figure { margin: 2.5em 0; padding: 0; text-align: center; page-break-inside: avoid; width: 100%; }
    .article-img { width: 100% !important; max-width: 100% !important; height: auto !important; display: block; margin: 0; padding: 0; }
    .article-caption { font-family: "PingFang SC", "Helvetica Neue", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; font-size: 0.55em; color: #999; text-align: center; margin-top: 0.6em; padding: 0; text-indent: 0; line-height: 1.4; }
    .article-footer { margin-top: 4em; text-align: center; }
    .back-to-toc { display: inline-block; padding: 12px 30px; border: 1.5px solid #222; text-decoration: none; color: #222; font-weight: bold; border-radius: 30px; }
    '''
    book.add_item(epub.EpubItem(uid="style_default", file_name="style/default.css", media_type="text/css", content=style))
    
    for url, data in image_manager.images.items():
        book.add_item(epub.EpubItem(uid=data["id"], file_name=data["filename"], media_type=data["mime"], content=data["content"]))

    if info.get("cover_data"): 
        book.set_cover("images/cover.jpg", info["cover_data"])

    # 1. 创建自定义目录页 (CONTENTS)
    clean_it = info["issue_title"].replace("《", "").replace("》", "")
    toc_html = f'<div class="toc-container"><div class="toc-header">《{clean_it}》 · 目录</div>'
    if info.get("cover_data"): toc_html += '<img src="images/cover.jpg" class="toc-cover" />'
    toc_html += '<div class="toc-list">'
    for i, art in enumerate(articles):
        clean_t = art["title"].replace("{{", "").replace("}}", "").strip()
        toc_html += f'<a class="toc-item" href="chapter_{i}.xhtml"><div class="toc-item-title">{i+1}. {clean_t}</div></a>'
    toc_html += '</div></div>'
    
    cp = epub.EpubHtml(uid="contents", title="目录", file_name="contents.xhtml", content=f'<html><head><link href="style/default.css" rel="stylesheet" type="text/css"/></head><body>{toc_html}</body></html>', lang='zh-CN')
    book.add_item(cp)
    
    # 2. 创建空白页
    bp = epub.EpubHtml(uid="blank", title=" ", file_name="blank.xhtml", content='<html><body><div style="height:100vh;"></div></body></html>', lang='zh-CN')
    book.add_item(bp)

    # 3. 生成文章章节
    spine_chapters = []
    for i, art in enumerate(articles):
        ch = epub.EpubHtml(uid=f"chapter_{i}", title=art["title"], file_name=f"chapter_{i}.xhtml", content=f'<html><head><link href="style/default.css" rel="stylesheet" type="text/css"/></head><body>{art["html"]}</body></html>', lang='zh-CN')
        book.add_item(ch)
        spine_chapters.append(ch)
        
    # 4. 设置系统导航和阅读顺序
    book.toc = tuple(spine_chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ['nav', cp, bp] + spine_chapters
    
    epub.write_epub(filename, book, {})
    console.print(Panel(f"[bold green]✅ 成功保存至:[/bold green] [cyan]{Path(filename).absolute()}[/cyan]", border_style="green"))

async def process_one_issue(issue_id, issue_url, selected_past, home_info, context):
    """处理单期下载的核心逻辑"""
    console.print(f"\n[bold blue]>>> 正在处理:[/bold blue] [yellow]{issue_id}[/yellow]")
    page = await context.new_page()
    try:
        image_manager = ImageManager(context)
        info = await get_toc(page, issue_url)
        
        # 深度年份探测 (针对手动输入)
        if not info["links"] and selected_past is None:
            for y in range(datetime.now().year, 2020, -1):
                alt = f"https://weekly.caixin.com/{y}/{issue_id}/"
                console.print(f"🔍 尝试年份 {y}...")
                try:
                    info = await get_toc(page, alt)
                    if info["links"]: break
                except Exception: continue

        if not selected_past:
            selected_past = {"date": datetime.now().strftime("%Y年%m月%d日"), "no": info["issue_no"], "title": info.get("topic") or info["issue_title"]}

        cover_url = info.get("cover_url") or home_info["cover_url"]
        if cover_url:
            try:
                res = await context.request.get(cover_url)
                if res.status == 200: info["cover_data"] = await res.body()
            except Exception as e:
                console.print(f"[dim]封面下载跳过: {e}[/dim]")
        
        links = info["links"][:35] # 放宽限制至35
        if not links:
            console.print(f"[bold red]❌ 未能在 {issue_id} 中找到有效文章链接。[/bold red]")
            return

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True # 完成后自动清除进度条，保持界面干净
        ) as progress:
            task_id = progress.add_task(f"[cyan]🚀 正在下载《{selected_past['title']}》...", total=len(links))
            semaphore, articles = asyncio.Semaphore(4), [None] * len(links)

            async def worker(i, item):
                async with semaphore:
                    tp = None
                    try:
                        tp = await context.new_page()
                        # 拦截不必要的广告和资源
                        await tp.route("**/*.{png,jpg,jpeg,gif,webp}", lambda r: r.continue_() if "caixin.com" in r.request.url else r.abort())
                        await tp.route("**/*", lambda r: r.abort() if any(x in r.request.url for x in ["analytics", "adserver", "fonts", "hm.baidu.com"]) else r.continue_())
                        
                        progress.update(task_id, description=f"[cyan]正在抓取: [white]{item['title'][:15]}...")
                        data = await scrape_article(tp, item["url"], image_manager, default_title=item["title"])
                        if data: articles[i] = data
                    except Exception as e:
                        console.print(f"[dim]文章抓取失败 ({item['title'][:10]}): {e}[/dim]")
                    finally:
                        if tp: await tp.close()
                        progress.advance(task_id)

            await asyncio.gather(*[worker(i, item) for i, item in enumerate(links)])
            progress.update(task_id, description="[bold green]✅ 抓取完成")

        if arts := [a for a in articles if a]:
            filename = f"download/{selected_past['date']} (第{selected_past['no']}期) - {selected_past['title']}.epub"
            os.makedirs("download", exist_ok=True)
            create_epub(arts, info, image_manager, filename)
        else:
            console.print(f"[bold red]❌ {issue_id} 抓取结果为空。[/bold red]")
            
    except Exception as e:
        console.print(f"[bold red]❌ 处理 {issue_id} 时发生致命错误:[/bold red] {e}")
    finally:
        await page.close()

async def main():
    start_time = time.time()
    console.print(Panel(f"[bold cyan]财新周刊全自动下载引擎 v{VERSION}[/bold cyan]\n[dim]Caixin Weekly EPUB Generator[/dim]", border_style="cyan"))
    console.print(Panel(DISCLAIMER.strip(), title="Disclaimer", border_style="red"))
    
    try:
        agree = await questionary.confirm("您是否已阅读并同意上述免责声明？/ Do you agree to the disclaimer?").ask_async()
        if not agree:
            console.print("[bold red]🚫 用户未同意声明，退出程序。[/bold red]"); return
    except KeyboardInterrupt:
        return

    pw_cookies = []
    try:
        if not os.path.exists(COOKIES_FILE):
            console.print(f"[bold red]❌ 找不到 {COOKIES_FILE} 文件，请参考文档配置后重试。[/bold red]"); return
            
        with console.status("[bold green]正在加载认证信息..."):
            with open(COOKIES_FILE, "r") as f:
                cookies_data = json.load(f)
                for c in cookies_data:
                    pc = {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/")}
                    if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]: pc["sameSite"] = c["sameSite"]
                    pw_cookies.append(pc)
    except Exception as e:
        console.print(f"[bold red]❌ Cookies 解析错误:[/bold red] {e}"); return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
            await context.add_cookies(pw_cookies)
            page = await context.new_page()
            
            with console.status("[bold green]正在获取期刊列表..."):
                home_info = await get_toc(page, "https://weekly.caixin.com/")
            
            # --- 极简版交互逻辑：回车即下载 ---
            offset, page_size = 0, 12
            selected_tasks = []

            while True:
                pasts = home_info.get("past_issues", [])
                total = len(pasts)
                current_page_items = pasts[offset : offset + page_size]

                # 构建选项列表
                choices = []
                for p in current_page_items:
                    # 状态保持：如果之前已选，则默认勾选
                    is_checked = p["id"] in [t[0] for t in selected_tasks]
                    choices.append(questionary.Choice(
                        title=f"{p['id']} | {p['date']} | 第{p['no']}期 | {p['title']}",
                        value=p,
                        checked=is_checked
                    ))

                choices.append(questionary.Separator("-" * 45))

                # 智能导航选项
                if offset + page_size < total:
                    choices.append(questionary.Choice(title="[ ➔ 下一页 / Next Page ]", value="NEXT"))
                else:
                    choices.append(questionary.Choice(title="[ 🔄 加载更多 / Load More ]", value="MORE"))

                if offset > 0:
                    choices.append(questionary.Choice(title="[ ⬅ 上一页 / Prev Page ]", value="PREV"))

                choices.extend([
                    questionary.Choice(title="[ ⌨ 手动输入 ID ]", value="INPUT"),
                    questionary.Choice(title="[ ✖ 退出程序 ]", value="EXIT")
                ])

                console.print(f"\n[bold green]已选任务:[/bold green] [yellow]{len(selected_tasks)}[/yellow] 期 | [dim]方向键移动，空格勾选，回车直接开始下载[/dim]")
                try:
                    results = await questionary.checkbox(
                        f"请勾选期刊 (第 {offset//page_size + 1} 页):",
                        choices=choices,
                        style=questionary.Style([
                            ('checkbox', 'fg:#00ffff'),
                            ('selected', 'fg:#00ff00'),
                            ('separator', 'fg:#666666'),
                        ])
                    ).ask_async()
                except KeyboardInterrupt:
                    break

                if results is None or "EXIT" in results: 
                    selected_tasks = [] # 清空以示退出
                    break

                # 优先级 1：处理导航指令 (如果有导航，则不触发立即下载)
                nav_triggered = False

                if "NEXT" in results:
                    # 翻页前保存当前页的所有勾选状态
                    for r in results:
                        if isinstance(r, dict) and r["id"] not in [t[0] for t in selected_tasks]:
                            selected_tasks.append((r["id"], r["url"], r))
                    # 移除未勾选但之前在 selected_tasks 里的本页项目
                    current_page_ids = [p["id"] for p in current_page_items]
                    selected_tasks = [t for t in selected_tasks if not (t[0] in current_page_ids and t[0] not in [r["id"] for r in results if isinstance(r, dict)])]
                    offset += page_size
                    nav_triggered = True

                elif "PREV" in results:
                    for r in results:
                        if isinstance(r, dict) and r["id"] not in [t[0] for t in selected_tasks]:
                            selected_tasks.append((r["id"], r["url"], r))
                    current_page_ids = [p["id"] for p in current_page_items]
                    selected_tasks = [t for t in selected_tasks if not (t[0] in current_page_ids and t[0] not in [r["id"] for r in results if isinstance(r, dict)])]
                    offset = max(0, offset - page_size)
                    nav_triggered = True

                elif "MORE" in results:
                    with console.status("[bold green]从服务器获取更多历史期刊..."):
                        try:
                            await page.click(".xsjMore, a.more"); await asyncio.sleep(2)
                            home_info = await get_toc(page)
                            offset += page_size
                        except Exception: 
                            console.print("[yellow]⚠️ 已到达最末页。[/yellow]")
                    nav_triggered = True

                elif "INPUT" in results:
                    manual_id = await questionary.text("请输入期号 (如 cw1180):").ask_async()
                    if manual_id and manual_id.strip().startswith("cw"):
                        mid = manual_id.strip()
                        selected_tasks.append((mid, f"https://weekly.caixin.com/{datetime.now().year}/{mid}/", None))
                    nav_triggered = True

                # 优先级 2：如果没有触发导航，且结果不为空，则意味着用户按下回车确认当前选择
                if not nav_triggered:
                    # 更新最终选择
                    final_batch = []
                    # 先保留不在当前页的其他页选择
                    current_page_ids = [p["id"] for p in current_page_items]
                    final_batch = [t for t in selected_tasks if t[0] not in current_page_ids]
                    # 加上当前页的勾选
                    for r in results:
                        if isinstance(r, dict):
                            final_batch.append((r["id"], r["url"], r))

                    selected_tasks = final_batch
                    if selected_tasks:
                        break # 跳出循环，开始下载
                    else:
                        console.print("[yellow]⚠️ 请至少勾选一期周刊。[/yellow]")

            # --- 批量执行下载 ---

            # --- 批量执行下载 ---
            if selected_tasks:
                # 按照期号排序，方便用户检查
                selected_tasks.sort(key=lambda x: x[0])
                
                # 展示确认清单
                table = Table(title="待下载任务清单", border_style="cyan", show_header=True, header_style="bold magenta")
                table.add_column("期号", style="yellow")
                table.add_column("出版日期", style="green")
                table.add_column("主题")
                
                for sid, surl, spast in selected_tasks:
                    if spast:
                        table.add_row(sid, spast["date"], spast["title"])
                    else:
                        table.add_row(sid, "手动输入", "未知")
                
                console.print("\n")
                console.print(table)
                
                confirm_download = await questionary.confirm(
                    f"确认开始下载以上 {len(selected_tasks)} 期杂志吗？",
                    default=True
                ).ask_async()
                
                if confirm_download:
                    console.print(f"\n[bold blue]📦 正在开始批量任务...[/bold blue]")
                    for sid, surl, spast in selected_tasks:
                        await process_one_issue(sid, surl, spast, home_info, context)
                    
                    duration = time.time() - start_time
                    console.print(Panel(f"[bold green]✨ 批量下载任务全部完成！[/bold green]\n[dim]总耗时: {duration:.1f} 秒[/dim]", border_style="green"))
                else:
                    console.print("[yellow]已取消下载，您可以重新选择。[/yellow]")
                    # 重新进入循环（这里简单处理，退出即可，用户可以再次运行）
            else:
                console.print("[dim]👋 未选择任何任务，程序退出。[/dim]")
        except Exception as e:
            console.print(f"[bold red]💥 运行时发生未知错误:[/bold red] {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
