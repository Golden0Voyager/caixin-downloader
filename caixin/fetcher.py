"""页面抓取：导航、资源拦截、目录解析、单篇正文提取。"""

import asyncio
import contextlib
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from .config import (
    BLOCKED_IMAGE_EXTS,
    BLOCKED_URL_MARKERS,
    MIN_ARTICLE_CHARS,
    SECTION_HEADING_CLASSES,
    console,
)
from .images import ImageManager
from .text import (
    clean_title,
    parse_iso_date,
    sanitize_inline_style,
    section_name,
    strip_hard_indent,
    strip_section_prefix,
    strip_watermarks,
    truncate_after_end_marker,
)


def _is_section_heading(tag):
    """期页的栏目标题（`.reportTit` / `.magIntrotit`）。"""
    classes = tag.get("class") or []
    return tag.name in ("div", "h1", "h2", "h3", "h4") and any(
        c in classes for c in SECTION_HEADING_CLASSES
    )


def extract_article_links(root):
    """按文档顺序抽取文章链接，并标出每篇所属的栏目。

    财新期页把文章放在 `<div class="magIntrotit">金融 Finance</div>` 之后的一组
    `<dl>` 里 —— 栏目名只出现一次，作用于其后所有链接，所以必须顺序遍历、边走边记。
    封面报道用 `.reportTit`。首页没有这些标记，`section` 留空，目录页会退回平铺列表。

    返回 `[{"title", "url", "section"}]`，顺序即文档顺序（也就是版面阅读顺序）。
    """
    links, seen = [], set()
    section = ""
    for node in root.descendants:
        if not getattr(node, "name", None):
            continue
        if _is_section_heading(node):
            section = section_name(node.get_text(" ", strip=True))
            continue
        if node.name != "a":
            continue
        href = node.get("href")
        if not href or ".caixin.com/202" not in href or not href.endswith(".html"):
            continue
        if href in seen:
            continue
        title = re.sub(r"\s+", " ", node.get_text(strip=True)).replace("{{", "").replace("}}", "")
        if len(title) <= 2 or "期号" in title:
            continue
        seen.add(href)
        links.append({"title": strip_section_prefix(title, section), "url": href, "section": section})
    return links


async def safe_goto(page, url, retries=3, timeout=60000):
    """带重试机制的页面跳转"""
    for i in range(retries):
        try:
            return await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            if i == retries - 1: raise e
            await asyncio.sleep(2 * (i + 1))


async def _route_request(route):
    """单条路由负责全部资源拦截。

    Playwright 的路由按**注册倒序**匹配：后注册的 `**/*` 会先命中并对一切
    continue_()，先注册的图片路由永远轮不到（实测只触发后者）。所以拦截
    必须合并到一个 handler 里，不能拆成两条。
    """
    url = route.request.url
    if any(marker in url for marker in BLOCKED_URL_MARKERS):
        return await route.abort()
    if url.lower().split("?")[0].endswith(BLOCKED_IMAGE_EXTS) and "caixin.com" not in url:
        return await route.abort()
    return await route.continue_()


async def scrape_article(page, url, image_manager, default_title=None, section=""):
    """抓取单篇文章的正文内容并处理图片"""
    try:
        # 使用 page.on 捕获所有 API 响应，可能有多页
        api_parts = []
        api_auth_failed = False

        async def intercept_content(response):
            nonlocal api_auth_failed
            if "checkAuthByIdJsonp" in response.url and "type=0" in response.url:
                try:
                    body = await response.text()
                    api_json = None
                    try:
                        api_json = json.loads(body)
                    except json.JSONDecodeError:
                        m = re.search(r'[^(]+\((.+)\)\s*$', body, re.DOTALL)
                        if m:
                            api_json = json.loads(m.group(1))

                    if api_json:
                        if api_json.get("code") == 0 and api_json.get("data"):
                            data_str = api_json["data"]
                            cm = re.search(r'resetContentInfo\((.+)\)$', data_str, re.DOTALL)
                            if cm:
                                inner = json.loads(cm.group(1))
                                part = inner.get("content", "")
                                if part:
                                    api_parts.append(part)
                        elif api_json.get("code") != 0:
                            api_auth_failed = True
                except Exception:
                    pass

        def _handler(r):
            return asyncio.create_task(intercept_content(r))
        page.on("response", _handler)

        await safe_goto(page, url)

        # 关键：等待 networkidle，让 JS 有足够时间加载完整内容
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15000)
        await page.wait_for_timeout(3000)

        # 点击「余下全文」加载后续分页（财新周刊长文为多页结构）。
        # 页面未就绪时点击会静默失败，导致只抓到第一页就被当成全文——必须校验并重试。
        async def _expand_fulltext():
            """展开「余下全文」；返回 True 表示单页文章，或已成功展开。"""
            link = page.locator("a[href*='#page2']", has_text="余下全文")
            try:
                if await link.count() == 0:
                    return True  # 单页文章，无需展开
                before = len(await page.inner_text("body"))
            except Exception:
                return True

            with contextlib.suppress(Exception):
                await link.first.click(timeout=8000)
            await page.wait_for_timeout(4000)

            try:
                return len(await page.inner_text("body")) > before
            except Exception:
                return False

        if not await _expand_fulltext():
            # 首次展开失败：重新加载页面后重试一次
            console.log(f"[dim]「余下全文」未展开，重新加载后重试: {url}[/dim]")
            with contextlib.suppress(Exception):
                await safe_goto(page, url)
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=15000)
                await page.wait_for_timeout(3000)
                if not await _expand_fulltext():
                    console.log(f"[yellow]⚠️ 「余下全文」两次均未展开，正文可能不完整: {url}[/yellow]")

        with contextlib.suppress(Exception):
            page.remove_listener("response", _handler)

        # 合并 API 响应。
        # 注意：财新的 checkAuthByIdJsonp 返回的是「从头开始」的累积正文——
        # 点击「余下全文」后会再次请求该接口，返回的是第 1 页 + 第 2 页的完整内容，
        # 而不是只返回第 2 页。若直接拼接，第一页会整块重复一次。
        # 故：当所有响应都被最长的那次包含时（累积型），只保留最长的那次。
        if api_parts:
            longest = max(api_parts, key=len)
            api_html = longest if all(part in longest for part in api_parts) else "".join(api_parts)
        else:
            api_html = ""
        api_content = {"html": api_html} if api_html else {}

        # 无论 API 是否成功，都获取最终 DOM 作为比对基准
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # 1. 标题提取
        title = "未知标题"
        title_tag = soup.select_one("h1, .wiv-title, .art-title, .title, #conTit h1")
        if title_tag:
            title = title_tag.get_text(strip=True)
        elif default_title:
            title = default_title
        title = clean_title(title)
        # 文章页自己的 <h1> 带栏目前缀（「封面报道｜抢占办公室」）。期页的链接标题
        # 已经在 extract_article_links 里剥过一次，这里补上同一刀，保证正文标题与
        # 目录条目一致；「专栏｜」「随笔｜」这类体裁标记 strip_section_prefix 会保留。
        title = strip_section_prefix(title, section)

        # 2. 作者提取
        author_info = ""
        author_selectors = [".author", ".artInfo", "#author_top", ".editor-name", ".media-name"]
        for s in author_selectors:
            tag = soup.select_one(s)
            if tag:
                author_info = tag.get_text(separator=" ", strip=True)
                if "本文来源于" in author_info: author_info = ""
                if author_info: break

        # 3. 正文提取 — 对比 API 和 DOM，取更完整的内容
        dom_div = soup.find(id="Main_Content_Val") or soup.select_one(".text, .article-content, article, #main_content, .art-content")
        api_div = None
        if api_content.get("html"):
            api_div = BeautifulSoup(f"<div>{api_content['html']}</div>", "html.parser").div

        # 选择更长的内容，确保拿到完整正文
        if api_div and dom_div:
            api_len = len(api_div.get_text(strip=True))
            dom_len = len(dom_div.get_text(strip=True))
            content_div = api_div if api_len >= dom_len else dom_div
            if api_len > 0 and dom_len > 0 and abs(api_len - dom_len) > max(api_len, dom_len) * 0.3:
                console.log(f"[dim]文章正文来源: API={api_len}字 vs DOM={dom_len}字，选择{'API' if api_len >= dom_len else 'DOM'}[/dim]")
        elif api_div:
            content_div = api_div
        elif dom_div:
            content_div = dom_div
        else:
            return None

        # 4. 清理无效元素
        blacklist = [
            "script", "style", "canvas", ".share", ".ad", ".bottom-adv",
            ".media-box", ".aitt", ".copyright", ".watermark", ".mask",
            ".hide", ".hidden", ".artInfo-box", ".read-all", ".pay-all",
            ".copyright-box", ".related-articles", ".recommend", ".keyword"
        ]
        for selector in blacklist:
            for tag in content_div.select(selector): tag.decompose()

        # 5. 正文边界截断：财新文章末尾有"财"小蓝标，其后是相关推荐/视频，不应入 EPUB
        if truncate_after_end_marker(content_div):
            console.log("[dim]  已截断文末相关推荐区块[/dim]")

        # 6. 水印与垃圾文本清理（判定逻辑见 text.strip_watermarks）
        strip_watermarks(content_div)

        # 6. 图片处理 (支持 CXIMG 自定义标签 + 懒加载)
        img_tags = content_div.find_all("img")
        img_urls = []
        img_captions = []
        img_containers = []  # 需要清理的原始容器

        for img in img_tags:
            src = ImageManager.normalize_url(
                img.get("data-src") or img.get("data-original-src") or img.get("src"))
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

            def _build_figure(idx: int):
                # 构建干净的 figure 结构；装饰图（文末「财」字标志、署名图标）单独标类，按小尺寸展示
                is_marker = bool((image_manager.images.get(img_urls[idx]) or {}).get("is_marker"))
                figure = soup.new_tag("figure", attrs={
                    "class": "article-figure article-figure--marker" if is_marker else "article-figure"})
                # alt 必须有：没有 alt 的 <img> 在屏幕阅读器里会念出整条文件名
                # （md5 十六进制串）。装饰图是纯设计元素，给空 alt 让它被跳过。
                alt_text = "" if is_marker else (img_captions[idx] or "配图")
                new_img = soup.new_tag("img", attrs={
                    "src": local_paths[idx], "alt": alt_text,
                    "class": "article-img article-img--marker" if is_marker else "article-img"})
                figure.append(new_img)

                if img_captions[idx]:
                    # 样式全部交给 CSS 的 figcaption.article-caption（带 !important），
                    # 这里不再写内联 style —— 否则与 CSS 打架，且是无效死代码。
                    fcap = soup.new_tag("figcaption", attrs={"class": "article-caption"})
                    fcap.string = img_captions[idx]
                    figure.append(fcap)
                return figure

            # ⚠️ 财新的多图轮播组件把几张 <img> 放在同一个 cximg / article_img_container 里
            # （2025-08-02 那篇实测：展开「余下全文」后同一容器内出现两张不同 URL 的图）。
            # 容器只能整体替换一次：若按 img 逐个 replace_with，第二个 img 的目标节点
            # 已被摘出树，bs4 抛 "Cannot replace one element with another when the element
            # to be replaced is not part of a tree"，整篇文章会被上面的 except 吞掉、
            # 静默消失。所以先按容器分组，整组替换成一个装有多张 figure 的 div。
            grouped: dict[int, list[int]] = {}
            singles: list[int] = []
            for idx in range(len(img_tags)):
                container = img_containers[idx]
                if container is None:
                    singles.append(idx)
                else:
                    grouped.setdefault(id(container), []).append(idx)

            for idxs in grouped.values():
                container = img_containers[idxs[0]]
                try:
                    wrapper = soup.new_tag("div")
                    for idx in idxs:
                        if local_paths[idx]:
                            wrapper.append(_build_figure(idx))
                        # 下载失败：只丢这一张，不牵连同容器的其他图
                    if wrapper.contents:
                        container.replace_with(wrapper)
                    else:
                        container.decompose()
                except Exception as e:  # noqa: BLE001
                    console.log(f"[yellow]⚠️ 图片替换失败，已跳过该图组: {e}[/yellow]")

            for idx in singles:
                try:
                    if local_paths[idx]:
                        img_tags[idx].replace_with(_build_figure(idx))
                    else:
                        # 下载失败或已判定为装饰性图标：删除 img
                        img_tags[idx].decompose()
                except Exception as e:  # noqa: BLE001
                    console.log(f"[yellow]⚠️ 图片替换失败，已跳过: {e}[/yellow]")

        # 清理因图片被丢弃而留下的空容器（否则 .article-figure 的 margin 会撑出空白）
        for tag in content_div.find_all(["figure", "a"]):
            if not tag.get_text(strip=True) and not tag.find("img"):
                tag.decompose()

        # 6.5 剥掉段首硬编码的全角缩进，缩进完全交给 CSS text-indent
        stripped = strip_hard_indent(content_div)
        if stripped:
            console.log(f"[dim]  剥除 {stripped} 处段首硬缩进[/dim]")

        # 7. 属性清洗 (移除所有 ID 和不必要的 Class，保留内联 style 和关键属性)
        preserved_classes = {
            "lead", "quote", "author-bar", "content-body",
            "article-figure", "article-figure--marker",
            "article-img", "article-img--marker", "article-caption",
        }

        # 根节点自身不在 find_all 的结果里，必须单独清洗：它带着 id、class
        # 和那张 62KB 的内联 base64 背景纹理（整期重复 23 次 ≈ 1.4MB）。
        content_div.attrs = {}

        for tag in content_div.find_all(True):
            if tag.name == "img": continue
            c_cls = tag.get("class", [])
            if isinstance(c_cls, str): c_cls = [c_cls]
            inline_style = sanitize_inline_style(tag.get("style"))
            n_attrs = {}
            valid_classes = [c for c in c_cls if c in preserved_classes]
            if valid_classes: n_attrs["class"] = " ".join(valid_classes)
            if inline_style: n_attrs["style"] = inline_style
            if tag.name == "a":
                href = tag.get("href")
                if href: n_attrs["href"] = href
            tag.attrs = n_attrs

        # 署名行（正文首段的「文｜财新周刊 徐路易 王克柔」）单独标类，
        # 免得被当成普通段落缩进排版。必须在属性清洗之后加，否则会被清掉。
        first_p = content_div.find("p")
        if first_p and re.match(r"^\s*文\s*[｜|]", first_p.get_text(strip=True)):
            first_p["class"] = "article-byline"

        # 8. 组装 HTML（版心包装层 .page 与章末翻页条由 create_epub 统一加 ——
        #    只有它知道章节序号与总数，这里产不出「上一篇 / 下一篇」的链接）
        article_html = f"<div class='article-header'><h1 class='article-title'>{title}</h1>"
        if author_info: article_html += f"<div class='author-bar'>{author_info}</div>"
        article_html += f"</div><div class='content-body'>{str(content_div)}</div>"

        # 完整性验证
        content_text = content_div.get_text(strip=True)
        img_count = len(content_div.find_all("img"))
        is_empty = len(content_text) < MIN_ARTICLE_CHARS and img_count == 0

        if "余下全文" in content_text or "阅读全文需" in content_text or "订阅后继续阅读" in content_text:
            console.log(f"[yellow]⚠️ 文章 ({title[:15]}) 可能不完整，正文包含未加载标志[/yellow]")
        elif api_auth_failed and not api_content.get("html"):
            console.log(f"[yellow]⚠️ Cookie 可能已失效，文章 ({title[:15]}) 仅抓到摘要[/yellow]")
        elif is_empty:
            console.log(f"[yellow]⚠️ 文章 ({title[:15]}) 正文仅 {len(content_text)} 字且无图，判为空章，不入书[/yellow]")
        elif len(content_text) < MIN_ARTICLE_CHARS:
            console.log(f"[dim]· {title[:15]}... ({len(content_text)} 字 / {img_count} 图，按图集保留)[/dim]")
        else:
            src = "API" if api_div and (not dom_div or len(api_div.get_text(strip=True)) >= len(dom_div.get_text(strip=True))) else "DOM"
            console.log(f"[dim]✓ {title[:15]}... ({len(content_text)}字, {img_count}图, 来源:{src})[/dim]")

        # `section` 只是透传：期页给了栏目名，目录页才能分组。首页/手动输入期号时为空，
        # 目录退回平铺。不要在这里推断栏目 —— 文章页上没有可靠的栏目标记。
        return {"title": title, "html": article_html, "url": url, "empty": is_empty, "section": section}
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
    issue_date = datetime.now().strftime("%Y-%m-%d")

    # 尝试寻找包含期数的文本节点
    for text_node in soup.find_all(string=re.compile(r"202\d年第\d+期")):
        raw = text_node.strip().replace("{{", "").replace("}}", "")
        # 提取标题：优先找含“财新周刊”的长字符串
        cm = re.search(r"(《?财新周刊》?\s*\d{4}年第\d+期)", raw)
        if cm:
            issue_title = cm.group(1)
        elif "第" in raw and "期" in raw:  # pragma: no cover - 见下
            # 防御性分支：上面 `find_all` 的过滤器要求节点含「2026年第N期」，
            # 而 cm 的正则一定能匹配这同一段文字（刊名与空白都是可选的），
            # 所以走到这里是不可能的。留着只是防止过滤器以后被放宽。
            issue_title = raw.split("出版日期")[0].strip()

        # 提取期号
        nm = re.search(r"第(\d+)期", raw)
        if nm: issue_no = nm.group(1)

        # 如果已经成功拿到期号，就不用再找了
        if issue_no != "XX": break

    # 出版日期单独找：命中期号的那个文本节点不一定带日期，所以整页扫一遍。
    # 优先带「出版日期」字样的节点，其次任何能解析出日期的短文本。
    dated = [
        node.strip() for node in soup.find_all(string=re.compile(r"\d{4}[年\-/]\d{1,2}[月\-/]\d{1,2}"))
        if len(node.strip()) < 200
    ]
    for candidate in sorted(dated, key=lambda s: "出版日期" not in s):
        parsed = parse_iso_date(candidate, "")
        if parsed:
            issue_date = parsed
            break

    # 3. 提取历史期刊列表
    past_issues = []
    for li in soup.select(".xsjCon li"):
        text = li.get_text(separator=" ", strip=True).replace("{{", "").replace("}}", "")
        nm = re.search(r"年度期号:(\d+)", text)
        p = [p.strip() for p in text.split(" ") if p.strip()]
        a = li.find("a", href=True)
        cwm = re.search(r"cw\d+", a["href"]) if a else None
        if cwm:
            past_issues.append({
                "id": cwm.group(0),
                "date": parse_iso_date(text, ""),
                "no": nm.group(1) if nm else "",
                "title": p[0] if p else "",
                "url": a["href"]
            })

    # 4. 关键修复：如果当前期 ID 不在历史列表中，则将其置顶
    if issue_id and not any(p["id"] == issue_id for p in past_issues):
        current_year = datetime.now().year
        target_url = f"https://weekly.caixin.com/{current_year}/{issue_id}/"

        # 如果当前在首页（issue_url 为空或为 weekly.caixin.com），且没拿到准确期号，去该期页面“偷”一下准确信息
        is_home = (not issue_url) or ("weekly.caixin.com/" in issue_url and issue_url.endswith("/"))
        if is_home and (issue_no == "XX" or issue_title == "《财新周刊》"):
            try:
                temp_page = await page.context.new_page()
                await temp_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                temp_html = await temp_page.content()
                temp_soup = BeautifulSoup(temp_html, "html.parser")

                # 增强版搜索逻辑
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

                # 2. 提取日期 (独立搜索全页面，优先带「出版日期」字样的节点)
                temp_dated = [
                    node.strip() for node in temp_soup.find_all(
                        string=re.compile(r"\d{4}[年\-/]\d{1,2}[月\-/]\d{1,2}"))
                    if len(node.strip()) < 200
                ]
                for t in sorted(temp_dated, key=lambda s: "出版日期" not in s):
                    parsed = parse_iso_date(t, "")
                    if parsed:
                        issue_date = parsed
                        break

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


    # 5. 提取正文文章链接（带栏目）。栏目信息只用于目录页分组，不改变文章顺序。
    links = []
    seen = set()
    containers = [t for t in [
        soup.select_one(".lf"), soup.select_one(".ri"), soup.select_one(".mi"),
        soup.select_one(".main"), soup.select_one(".content"), soup.select_one(".article-list")
    ] if t] or [soup]
    for c in containers:
        for link in extract_article_links(c):
            if link["url"] in seen:
                continue
            seen.add(link["url"])
            links.append(link)

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
        "issue_date": issue_date,   # ISO 8601（YYYY-MM-DD）—— create_epub 直接写进 dc:date
        "cover_url": cover_url,
        "links": links,
        "past_issues": past_issues,
        "topic": topic
    }
