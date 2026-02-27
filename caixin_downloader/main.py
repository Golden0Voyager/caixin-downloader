import asyncio
import json
import uuid
import re
import os
import hashlib
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from ebooklib import epub

COOKIES_FILE = "cookies.json"

class ImageManager:
    """管理电子书中的图片下载与嵌入"""
    def __init__(self, context):
        self.context = context
        self.images = {}  # url -> {id, bytes, mime, filename}
        self.used_urls = set()

    async def download(self, url):
        if url in self.images:
            return self.images[url]["filename"]
        
        try:
            # 处理相对路径
            if url.startswith("//"):
                url = "https:" + url
            
            print(f"  🖼️ 下载图片: {url[:60]}...")
            response = await self.context.request.get(url, timeout=10000)
            if response.status == 200:
                content = await response.body()
                ext = url.split(".")[-1].split("?")[0].lower()
                if ext not in ["jpg", "jpeg", "png", "gif", "webp"]:
                    ext = "jpg"
                
                # 生成唯一且确定的文件名
                img_hash = hashlib.md5(url.encode()).hexdigest()
                filename = f"images/{img_hash}.{ext}"
                mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
                
                self.images[url] = {
                    "id": f"img_{img_hash}",
                    "content": content,
                    "mime": mime,
                    "filename": filename
                }
                return filename
        except Exception as e:
            print(f"  ⚠️ 图片下载失败: {e}")
        return None

async def scrape_article(page, url, image_manager):
    """抓取单篇文章的正文内容并处理图片"""
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    
    title_tag = soup.find("h1")
    title = title_tag.text.strip() if title_tag else "未知标题"
    
    author_info = ""
    author_tag = soup.select_one(".author, .artInfo, #author_top")
    if author_tag:
        author_info = author_tag.get_text(strip=True)
        
    content_div = soup.find(id="Main_Content_Val")
    if not content_div:
        content_div = soup.select_one(".text, .article-content, article")
        
    if not content_div:
        return None

    for tag in content_div.select("script, style, .share, .ad, .bottom-adv, .media-box"):
        tag.decompose()
        
    # 处理文章中的图片
    for img_tag in content_div.find_all("img"):
        src = img_tag.get("src")
        if src:
            local_path = await image_manager.download(src)
            if local_path:
                img_tag["src"] = local_path
            else:
                img_tag.decompose()

    # 深度清洗：移除所有标签的冗余属性 (class, id, style 等)
    for tag in content_div.find_all(True):
        if tag.name == "img":
            # 只保留 src
            src = tag.get("src")
            tag.attrs = {"src": src}
        else:
            # 移除所有属性，让 CSS 统一控制
            tag.attrs = {}

    # 组装更具语义化的 HTML
    article_html = f"<h1 class='article-title'>{title}</h1>"
    if author_info:
        article_html += f"<div class='author-bar'>{author_info}</div>"
    
    # 包装正文
    article_html += f"<div class='content-body'>{str(content_div)}</div>"

    return {
        "title": title,
        "html": article_html,
        "url": url
    }

async def get_toc(page, issue_url):
    """获取目录、期号和封面"""
    print(f"📥 正在获取目录: {issue_url}")
    await page.goto(issue_url, wait_until="networkidle", timeout=60000)
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    
    # 1. 提取封面图和期号 ID (从中间区域 .mi 提取)
    cover_url = None
    issue_id = "Latest"
    mi_div = soup.select_one(".mi")
    if mi_div:
        cover_tag = mi_div.find("img")
        if cover_tag:
            cover_url = cover_tag.get("src")
        a_tag = mi_div.find("a", href=True)
        if a_tag:
            # 从 https://weekly.caixin.com/2026/cw1194/ 提取 cw1194
            match = re.search(r"cw(\d+)", a_tag["href"])
            if match:
                issue_id = match.group(0)

    # 2. 提取期数标题
    issue_no = "XX"
    issue_title = f"财新周刊 {issue_id}"
    
    # 策略 A: 寻找包含“202x年第xx期”的文本节点
    text_match = soup.find(string=re.compile(r"202\d年第\d+期"))
    if text_match:
        issue_title = text_match.strip()
        no_match = re.search(r"第(\d+)期", issue_title)
        if no_match:
            issue_no = no_match.group(1)
    else:
        # 策略 B: 从往期列表的第一项推断当前期号 (往期+1)
        latest_past = soup.select_one(".xsjCon li")
        if latest_past:
            text = latest_past.get_text()
            match = re.search(r"年度期号:(\d+)", text)
            if match:
                curr_no_int = int(match.group(1)) + 1
                issue_no = f"{curr_no_int:02d}"
                issue_title = f"财新周刊 2026年第{issue_no}期 ({issue_id})"

    # 3. 提取本期文章链接
    links = []
    seen = set()
    
    # 定义文章链接的正则匹配模式
    def is_article_link(url):
        # 匹配 2026-02-13/102414391.html 这种格式
        return "weekly.caixin.com/202" in url and url.endswith(".html")

    # 尝试在特定区域抓取，如果没找到，则全页抓取
    targets = [soup.select_one(".lf"), soup.select_one(".ri"), soup.select_one(".mi")]
    containers = [t for t in targets if t]
    
    if not containers or "/cw" in page.url: # 如果在专属目录页，直接全页扫描
        containers = [soup]

    for container in containers:
        for a in container.find_all("a", href=True):
            href = a["href"]
            if is_article_link(href):
                if href not in seen:
                    seen.add(href)
                    title = re.sub(r'\s+', ' ', a.get_text(strip=True))
                    # 过滤掉一些过短或无意义的标题
                    if len(title) > 2 and "期号" not in title: 
                        links.append({"title": title, "url": href})
                    
    return {
        "issue_title": issue_title,
        "issue_no": issue_no,
        "cover_url": cover_url,
        "links": links
    }

def create_epub(articles, info, image_manager, filename="Caixin_Weekly.epub"):
    """打包 EPUB：支持自定义目录页、跨页分离和高级样式"""
    print(f"📚 开始组装 EPUB: {filename} ...")
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(info["issue_title"])
    book.set_language('zh-CN')
    book.add_author("财新周刊")
    
    # 1. 注入高级样式表
    style = '''
    body { font-family: "PingFang SC", "Source Han Sans CN", sans-serif; line-height: 1.8; padding: 0 5%; color: #333; }
    
    /* 目录页专用样式 */
    .toc-container { padding: 5% 5%; }
    .toc-header { font-size: 2.2em; border-bottom: 3px solid #000; padding-bottom: 10px; margin-bottom: 40px; font-weight: bold; }
    .toc-list { list-style: none; padding: 0; }
    .toc-item { margin: 15px 0; border-bottom: 1px dashed #ddd; padding-bottom: 8px; display: block; text-decoration: none; color: #333; }
    .toc-item-title { font-size: 1.15em; font-weight: bold; }
    
    /* 正文样式 */
    .article-title { font-size: 1.8em; font-weight: bold; line-height: 1.3; margin-top: 1.5em; margin-bottom: 0.5em; text-align: left; }
    .author-bar { font-size: 0.9em; color: #888; margin-bottom: 2.5em; padding-bottom: 0.5em; border-bottom: 1px solid #eee; }
    .content-body { font-size: 1.1em; text-align: justify; }
    .content-body p { margin: 1.2em 0; }
    .content-body p + p { text-indent: 2em; }
    img { max-width: 100%; height: auto; display: block; margin: 2em auto; border-radius: 4px; }
    h2, h3 { color: #000; margin-top: 2em; border-left: 4px solid #333; padding-left: 10px; }
    '''
    default_css = epub.EpubItem(uid="style_default", file_name="style/default.css", media_type="text/css", content=style)
    book.add_item(default_css)
    
    # 2. 嵌入图片资源
    for url, data in image_manager.images.items():
        img_item = epub.EpubItem(uid=data["id"], file_name=data["filename"], media_type=data["mime"], content=data["content"])
        book.add_item(img_item)

    # 3. 设置封面
    if info.get("cover_data"):
        book.set_cover("images/cover.jpg", info["cover_data"])

    # 4. 创建自定义目录页 (CONTENTS)
    toc_html = '<div class="toc-container"><div class="toc-header">目录 / CONTENTS</div><div class="toc-list">'
    for i, art in enumerate(articles):
        toc_html += f'<a class="toc-item" href="chapter_{i}.xhtml"><div class="toc-item-title">{art["title"]}</div></a>'
    toc_html += '</div></div>'
    
    contents_page = epub.EpubHtml(title="目录", file_name="contents.xhtml", lang='zh-CN')
    contents_page.content = f'<html><head><link href="style/default.css" rel="stylesheet" type="text/css"/></head><body>{toc_html}</body></html>'
    book.add_item(contents_page)

    # 5. 创建空白过渡页 (用于跨页分离)
    blank_page = epub.EpubHtml(title=" ", file_name="blank.xhtml", lang='zh-CN')
    blank_page.content = '<html><body><div style="height:100vh;"></div></body></html>'
    book.add_item(blank_page)

    # 6. 生成文章章节
    spine_chapters = []
    toc_links = []
    for i, art in enumerate(articles):
        chapter = epub.EpubHtml(title=art["title"], file_name=f"chapter_{i}.xhtml", lang='zh-CN')
        chapter.content = f'<html><head><link href="style/default.css" rel="stylesheet" type="text/css"/></head><body>{art["html"]}</body></html>'
        book.add_item(chapter)
        spine_chapters.append(chapter)
        toc_links.append(chapter)
        
    # 系统级目录 (侧边栏)
    book.toc = tuple(toc_links)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    # 7. 设定阅读顺序 (Spine)
    # 顺序：系统导航 -> 自定义目录页 -> 空白过渡页 -> 正文
    book.spine = ['nav', contents_page, blank_page] + spine_chapters
    
    epub.write_epub(filename, book, {})
    print(f"✅ 成功保存至: {Path(filename).absolute()}")

async def main():
    print("🚀 财新周刊全自动下载打包引擎 v2.0")
    
    # 加载 Cookie
    pw_cookies = []
    try:
        with open(COOKIES_FILE, "r") as f:
            cookies = json.load(f)
            for c in cookies:
                pc = {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/")}
                if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
                    pc["sameSite"] = c["sameSite"]
                pw_cookies.append(pc)
    except Exception as e:
        print(f"❌ 读取 Cookies 失败: {e}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(pw_cookies)
        page = await context.new_page()
        
        image_manager = ImageManager(context)
        
        # 1. 第一步：从主页获取封面和期号 ID
        print("🔍 正在主页寻找最新期号...")
        home_info = await get_toc(page, "https://weekly.caixin.com/")
        issue_id_match = re.search(r"cw\d+", home_info["issue_title"])
        issue_id = issue_id_match.group(0) if issue_id_match else "Latest"
        
        # 2. 第二步：访问专属目录页抓取全部链接
        full_toc_url = f"https://weekly.caixin.com/2026/{issue_id}/"
        print(f"📥 正在专属目录页抓取完整文章列表: {full_toc_url}")
        info = await get_toc(page, full_toc_url)
        
        # 3. 信息合并：如果专属目录页丢失了期号或封面，沿用主页获取的
        info["cover_url"] = home_info["cover_url"]
        if info["issue_no"] == "XX" and home_info["issue_no"] != "XX":
            info["issue_no"] = home_info["issue_no"]
            info["issue_title"] = home_info["issue_title"]
        
        # 设定抓取上限
        links_to_fetch = info["links"][:30] 
        
        print(f"📋 识别期号: {info['issue_no']} ({info['issue_title']})")
        print(f"🔗 找到 {len(info['links'])} 篇文章，准备下载前 {len(links_to_fetch)} 篇...")
        
        # 4. 下载封面
        if info["cover_url"]:
            print(f"🖼️ 正在下载封面图...")
            try:
                response = await context.request.get(info["cover_url"])
                if response.status == 200:
                    info["cover_data"] = await response.body()
                    print(" ✅ 封面下载成功")
            except Exception as e:
                print(f" ⚠️ 封面下载报错: {e}")
        
        # 4. 抓取文章
        articles = []
        for i, item in enumerate(links_to_fetch):
            print(f"[{i+1}/{len(links_to_fetch)}] {item['title'][:20]}...", end="", flush=True)
            try:
                art_data = await scrape_article(page, item["url"], image_manager)
                if art_data:
                    articles.append(art_data)
                    print(" ✓")
                else:
                    print(" ❌ 无内容")
            except Exception as e:
                print(f" ⚠️ 错误: {e}")
            await asyncio.sleep(2) 
            
        await browser.close()
        
    if articles:
        # 构造用户指定格式的文件名: 2026-财新周刊—第xx期260227
        date_str = datetime.now().strftime("%y%m%d")
        issue_no = info.get("issue_no", "XX")
        filename = f"2026-财新周刊—第{issue_no}期{date_str}.epub"
        create_epub(articles, info, image_manager, filename)
    else:
        print("❌ 未能抓取到文章。")

if __name__ == "__main__":
    asyncio.run(main())
