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

        print("📥 正在访问财新周刊主页...")
        await page.goto("https://weekly.caixin.com/", wait_until="domcontentloaded")
        first_article = await page.locator("a[href*='weekly.caixin.com/202']").first.get_attribute("href")

        if not first_article:
            print("❌ 无法找到测试文章链接")
            await browser.close()
            return

        print(f"📥 正在访问真实测试文章: {first_article}")

        # 拦截 API 响应，检测认证状态
        api_result = {}

        async def handle_response(response):
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
                        api_result["code"] = api_json.get("code")
                        api_result["status"] = api_json.get("data", "")
                        if api_json.get("code") == 0 and api_json.get("data"):
                            data_str = api_json["data"]
                            cm = re.search(r'resetContentInfo\((.+)\)$', data_str, re.DOTALL)
                            if cm:
                                inner = json.loads(cm.group(1))
                                api_result["content_length"] = len(inner.get("content", ""))
                except Exception as e:
                    api_result["error"] = str(e)

        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        await page.goto(first_article, wait_until="networkidle")
        await page.wait_for_timeout(3000)

        if api_result.get("code") == 0 and api_result.get("content_length", 0) > 500:
            print(f"✅ 测试成功！API 认证通过，返回正文长度 {api_result['content_length']} 字符")
            print("   Cookies 有效，可以正常抓取原文")
        elif api_result.get("code") == -1 or api_result.get("code") != 0:
            print(f"❌ 测试失败！API 返回 code={api_result.get('code')}，Cookies 已失效")
            print("   请重新登录财新网站，导出新的 cookies.json")
        else:
            # 回退到 DOM 检查
            content = await page.content()
            paywall_keywords = ["马上订阅", "阅读全文需", "限时特惠", "订阅后继续阅读", "未开通会员", "请订阅后阅读"]
            hit = [kw for kw in paywall_keywords if kw in content]
            if hit:
                print("❌ 测试失败：遭遇付费墙拦截，Cookie 无效或会员已过期！")
                print(f"   付费墙关键词: {', '.join(hit)}")
                print("   请用已开通财新通会员的账号重新登录，并导出新的 cookies.json")
            else:
                paragraphs = await page.locator("p").all_inner_texts()
                text = "".join([p for p in paragraphs if len(p.strip()) > 10])
                if len(text) > 500:
                    print("✅ 测试成功！成功穿透付费墙")
                else:
                    print("⚠️ 页面加载成功，但正文内容异常短，可能仍存在问题")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(test_caixin_login())
