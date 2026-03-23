import asyncio
import json
import re
from playwright.async_api import async_playwright

async def test_caixin_login():
    print("🚀 启动财新 Cookie 测试...")

    with open("cookies.json", "r") as f:
        cookies = json.load(f)

    pw_cookies = []
    for c in cookies:
        pc = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
        }
        if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
            pc["sameSite"] = c["sameSite"]
        pw_cookies.append(pc)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(pw_cookies)

        page = await context.new_page()

        # 先访问主页获取一个真实的文章链接
        print("📥 正在访问财新周刊主页...")
        await page.goto("https://weekly.caixin.com/", wait_until="domcontentloaded")
        first_article = await page.locator("a[href*='weekly.caixin.com/202']").first.get_attribute("href")

        if not first_article:
            print("❌ 无法找到测试文章链接")
            await browser.close()
            return

        print(f"📥 正在访问真实测试文章: {first_article}")
        await page.goto(first_article, wait_until="domcontentloaded")

        # 等待页面 JS 执行完毕
        await page.wait_for_timeout(3000)
        content = await page.content()
        title = await page.title()
        print(f"📖 页面标题: {title}")

        # 检查付费墙（覆盖所有已知的付费墙文案）
        paywall_keywords = ["马上订阅", "阅读全文需", "限时特惠", "订阅后继续阅读", "未开通会员", "请订阅后阅读"]
        hit = [kw for kw in paywall_keywords if kw in content]
        if hit:
            print("❌ 测试失败：Cookie 无效或会员已过期，遭遇付费墙拦截！")
            print(f"   付费墙关键词: {', '.join(hit)}")
            m = re.search(r'当前登录账号[:：](.*?)(?:<|")', content)
            if m:
                print(f"   当前登录账号: {m.group(1).strip()}")
            print("")
            print("💡 请用已开通财新通会员的账号重新登录，并导出新的 cookies.json")
        else:
            # 尝试抓取正文的前几百个字
            paragraphs = await page.locator("p").all_inner_texts()
            text = "".join([p for p in paragraphs if len(p.strip()) > 10])
            if text:
                print("✅ 测试成功！成功穿透付费墙，拿到正文：")
                print("-" * 50)
                print(text[:200] + "......")
                print("-" * 50)
            else:
                print("⚠️ 页面加载成功，但没找到正文标签。")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_caixin_login())
