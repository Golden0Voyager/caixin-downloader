import asyncio
import json
from playwright.async_api import async_playwright

async def test_caixin_login():
    print("🚀 启动财新 Cookie 测试...")
    
    with open("cookies.json", "r") as f:
        cookies = json.load(f)
        
    # Playwright requires sameSite to be specific string formats or omitted if 'unspecified'
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
        
        # 检查页面是否包含“马上订阅”或“付费可见”的提示
        content = await page.content()
        print(f"📖 页面标题: {await page.title()}")
        if "马上订阅" in content or "阅读全文需" in content or "限时特惠" in content:
            print("❌ 测试失败：Cookie 无效或权限不足，遭遇付费墙拦截！")
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
