"""Cookie 文件的读取与 Playwright 格式转换。

`main.py` 与 `test_caixin.py` 都要做同一件事，此前各写了一份。
"""

import json

# Playwright 只认这三个值，其余一律忽略
SAMESITE_VALUES = ("Strict", "Lax", "None")


def load_cookies(path):
    """读取浏览器插件导出的 cookies.json，转成 `context.add_cookies()` 需要的形状。

    只保留 name/value/domain/path/sameSite 五个字段：`expirationDate` 被有意忽略，
    于是 Playwright 按 session cookie 处理 —— 与项目一直以来的行为一致。
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    cookies = []
    for item in raw:
        cookie = {
            "name": item["name"],
            "value": item["value"],
            "domain": item["domain"],
            "path": item.get("path", "/"),
        }
        if item.get("sameSite") in SAMESITE_VALUES:
            cookie["sameSite"] = item["sameSite"]
        cookies.append(cookie)
    return cookies
