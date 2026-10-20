"""命令行入口：交互选期、并发抓取、逐期打包。"""

import asyncio
import os
import time
from datetime import datetime

from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from .config import (
    ARTICLE_CONCURRENCY,
    COOKIES_FILE,
    EARLIEST_ISSUE_YEAR,
    MAX_ARTICLES_PER_ISSUE,
    VERSION,
    console,
)
from .cookies import load_cookies
from .epub_builder import create_epub
from .fetcher import _route_request, get_toc, scrape_article
from .images import ImageManager
from .text import safe_filename

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


async def process_one_issue(issue_id, issue_url, selected_past, home_info, context):
    """处理单期下载的核心逻辑"""
    console.print(f"\n[bold blue]>>> 正在处理:[/bold blue] [yellow]{issue_id}[/yellow]")
    page = await context.new_page()
    try:
        image_manager = ImageManager(context)
        info = await get_toc(page, issue_url)

        # 深度年份探测 (针对手动输入)
        if not info["links"] and selected_past is None:
            for y in range(datetime.now().year, EARLIEST_ISSUE_YEAR - 1, -1):
                alt = f"https://weekly.caixin.com/{y}/{issue_id}/"
                console.print(f"🔍 尝试年份 {y}...")
                try:
                    info = await get_toc(page, alt)
                    if info["links"]: break
                except Exception: continue

        if not selected_past:
            selected_past = {
                "date": info.get("issue_date") or datetime.now().strftime("%Y-%m-%d"),
                "no": info["issue_no"],
                "title": info.get("topic") or info["issue_title"],
            }

        cover_url = info.get("cover_url") or home_info["cover_url"]
        if cover_url:
            try:
                res = await context.request.get(cover_url)
                if res.status == 200: info["cover_data"] = await res.body()
            except Exception as e:
                console.print(f"[dim]封面下载跳过: {e}[/dim]")

        if len(info["links"]) > MAX_ARTICLES_PER_ISSUE:
            console.print(
                f"[yellow]⚠️ 本期识别到 {len(info['links'])} 篇，超过上限 "
                f"{MAX_ARTICLES_PER_ISSUE} 篇，其余将被跳过[/yellow]"
            )
        links = info["links"][:MAX_ARTICLES_PER_ISSUE]
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
            semaphore, articles = asyncio.Semaphore(ARTICLE_CONCURRENCY), [None] * len(links)

            async def worker(i, item):
                async with semaphore:
                    tp = None
                    try:
                        tp = await context.new_page()
                        # 只注册一条路由 —— 见 _route_request 的说明
                        await tp.route("**/*", _route_request)

                        progress.update(task_id, description=f"[cyan]正在抓取: [white]{item['title'][:15]}...")
                        data = await scrape_article(
                            tp, item["url"], image_manager,
                            default_title=item["title"],
                            section=item.get("section", ""),
                        )
                        if data: articles[i] = data
                    except Exception as e:
                        console.print(f"[dim]文章抓取失败 ({item['title'][:10]}): {e}[/dim]")
                    finally:
                        if tp: await tp.close()
                        progress.advance(task_id)

            await asyncio.gather(*[worker(i, item) for i, item in enumerate(links)])
            progress.update(task_id, description="[bold green]✅ 抓取完成")

        # 空章（图集/视频栏目、或 Cookie 失效只抓到摘要）不入书：
        # 留着会在目录里出现一条点进去只有标题的条目，比缺一篇更让人困惑。
        kept = [a for a in articles if a and not a.get("empty")]
        dropped = [a for a in articles if a and a.get("empty")]
        if dropped:
            console.print(f"[yellow]⚠️ 本期有 {len(dropped)} 篇空章未入书："
                          + "、".join(a["title"][:12] for a in dropped[:5])
                          + ("…" if len(dropped) > 5 else ""))

        if kept:
            # 标题直接来自网页，含 / : | ? 等字符时会写盘失败或写错目录
            title_part = safe_filename(selected_past["title"])
            date_part = safe_filename(selected_past["date"], fallback="未知日期", limit=20)
            filename = f"download/{date_part} (第{selected_past['no']}期) - {title_part}.epub"
            os.makedirs("download", exist_ok=True)
            create_epub(kept, info, image_manager, filename)
        else:
            console.print(f"[bold red]❌ {issue_id} 抓取结果为空。[/bold red]")

    except Exception as e:
        console.print(f"[bold red]❌ 处理 {issue_id} 时发生致命错误:[/bold red] {e}")
    finally:
        await page.close()

async def main():
    import questionary
    from playwright.async_api import async_playwright

    start_time = time.time()
    console.print(Panel(f"[bold cyan]财新周刊全自动下载引擎 v{VERSION}[/bold cyan]\n[dim]Caixin Weekly EPUB Generator[/dim]", border_style="cyan"))
    console.print(Panel(DISCLAIMER.strip(), title="Disclaimer", border_style="red"))

    try:
        agree = await questionary.confirm("您是否已阅读并同意上述免责声明？/ Do you agree to the disclaimer?").ask_async()
        if not agree:
            console.print("[bold red]🚫 用户未同意声明，退出程序。[/bold red]"); return
    except KeyboardInterrupt:
        return

    try:
        if not os.path.exists(COOKIES_FILE):
            console.print(f"[bold red]❌ 找不到 {COOKIES_FILE} 文件，请参考文档配置后重试。[/bold red]"); return

        with console.status("[bold green]正在加载认证信息..."):
            pw_cookies = load_cookies(COOKIES_FILE)
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

                def _sync_page_selections(results, selected_tasks, current_page_items):
                    """翻页时同步当前页的勾选状态到 selected_tasks"""
                    for r in results:
                        if isinstance(r, dict) and r["id"] not in [t[0] for t in selected_tasks]:
                            selected_tasks.append((r["id"], r["url"], r))
                    current_page_ids = [p["id"] for p in current_page_items]
                    selected_ids = {r["id"] for r in results if isinstance(r, dict)}
                    return [t for t in selected_tasks if t[0] not in current_page_ids or t[0] in selected_ids]

                if "NEXT" in results:
                    selected_tasks = _sync_page_selections(results, selected_tasks, current_page_items)
                    offset += page_size
                    nav_triggered = True

                elif "PREV" in results:
                    selected_tasks = _sync_page_selections(results, selected_tasks, current_page_items)
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
            if selected_tasks:
                # 按照期号排序，方便用户检查
                selected_tasks.sort(key=lambda x: x[0])

                # 展示确认清单
                table = Table(title="待下载任务清单", border_style="cyan", show_header=True, header_style="bold magenta")
                table.add_column("期号", style="yellow")
                table.add_column("出版日期", style="green")
                table.add_column("主题")

                for sid, _surl, spast in selected_tasks:
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
                    console.print("\n[bold blue]📦 正在开始批量任务...[/bold blue]")
                    for sid, _surl, spast in selected_tasks:
                        await process_one_issue(sid, _surl, spast, home_info, context)

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
