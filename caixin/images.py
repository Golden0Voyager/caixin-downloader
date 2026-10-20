"""图片下载、分级与嵌入。"""

import asyncio
import hashlib
import io

from PIL import Image

from .config import (
    IMAGE_TIMEOUT_MS,
    JPEG_QUALITY,
    MARKER_IMAGE_DIM,
    MAX_IMAGE_WIDTH,
    MIN_IMAGE_DIM,
    RETRY_BACKOFF_SECONDS,
)


class ImageManager:
    """管理电子书中的图片下载与嵌入"""
    def __init__(self, context):
        self.context = context
        self.images = {}   # 规范化 url -> {id, content, mime, filename, is_marker}
        self.skipped = set()  # 已判定为装饰性图标而丢弃的 url
        self._inflight = {}   # 规范化 url -> Task，用于并发去重

    @staticmethod
    def normalize_url(url):
        """把协议相对写法（//host/path）统一成绝对 URL。

        财新的图片地址有绝对（https://img.caixin.com/...）和协议相对
        （//datanews.caixin.com/...）两种混用；只有统一规范化，`images`
        字典的键才能和调用方持有的 URL 对上（否则 is_marker 之类的元信息查不到）。
        """
        if not url:
            return None
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        return None

    @staticmethod
    def _prepare(content, ext):
        """按需处理图片字节，返回 (content, ext, is_marker)。

        - 长边 < MIN_IMAGE_DIM 的视为追踪像素，返回 (None, None, False) 丢弃；
        - 长边 < MARKER_IMAGE_DIM 的视为行内装饰图（文末「财」字标志、署名图标），
          返回 is_marker=True —— 保留**原始像素**、仅统一转成 PNG，显示尺寸由 CSS 限制；
        - 宽度 > MAX_IMAGE_WIDTH 的等比缩小；
        - 带透明通道的转 PNG，其余转 JPEG 以压缩体积；
        - 已符合要求且无需转码的，原样返回，避免二次压缩损失画质。
        """
        try:
            with Image.open(io.BytesIO(content)) as src:
                w, h = src.size
                if max(w, h) < MIN_IMAGE_DIM:
                    return None, None, False

                # 行内装饰图：保留原始像素，只把格式统一成 PNG。
                # 注意**不要放大**：文末「财」字标志的真实素材是 16×16 的 Windows ICO，
                # 插值放大不会凭空造出细节，只会变糊。显示尺寸交给 CSS 的 max-width。
                if max(w, h) < MARKER_IMAGE_DIM:
                    buf = io.BytesIO()
                    src.convert("RGBA").save(buf, format="PNG", optimize=True)
                    return buf.getvalue(), "png", True

                has_alpha = src.mode in ("RGBA", "LA") or (src.mode == "P" and "transparency" in src.info)
                if w <= MAX_IMAGE_WIDTH and not has_alpha:
                    return content, ext, False

                im = src
                if w > MAX_IMAGE_WIDTH:
                    im = src.resize((MAX_IMAGE_WIDTH, max(1, round(h * MAX_IMAGE_WIDTH / w))), Image.Resampling.LANCZOS)

                buf = io.BytesIO()
                if has_alpha:
                    im.save(buf, format="PNG", optimize=True)
                    return buf.getvalue(), "png", False
                im.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
                return buf.getvalue(), "jpg", False
        except Exception:
            # 无法解码时保留原始字节，交给阅读器处理
            return content, ext, False

    async def download(self, url, retries=2):
        """下载并登记一张图片，返回 EPUB 内的相对路径（失败返回 None）。

        同一 URL 可能被多篇文章同时请求 —— 文末「财」字标志在一期里出现 23 次，
        而 `self.images` 缓存只在下载**完成**后才写入。并发下两个协程会同时
        看到「缓存未命中」然后各下一遍，所以这里再加一层 in-flight 复用：
        后来的调用直接 await 前一个 Task 的结果。
        """
        url = self.normalize_url(url)
        if not url:
            return None
        if url in self.images:
            return self.images[url]["filename"]
        if url in self.skipped:
            return None

        task = self._inflight.get(url)
        if task is None:
            task = asyncio.ensure_future(self._fetch(url, retries))
            self._inflight[url] = task
            task.add_done_callback(lambda t, u=url: self._forget(u, t))
        # shield：等待方被取消时不要连带取消真正在跑的下载
        return await asyncio.shield(task)

    def _forget(self, url, task):
        """下载结束后清理 in-flight 表，并消费掉可能的异常。"""
        self._inflight.pop(url, None)
        if not task.cancelled():
            task.exception()

    async def _fetch(self, url, retries):
        """真正的下载与转码；失败一律返回 None，不向外抛异常。"""
        for attempt in range(retries):
            try:
                response = await self.context.request.get(url, timeout=IMAGE_TIMEOUT_MS)
            except Exception:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            # 非 200 也要重试。早先这里写的是 break —— 502/503 这类「正常返回的
            # 失败」会直接跳出循环，retries 形同虚设。
            if response.status != 200:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            content = await response.body()
            if not content:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            ext = self._guess_ext(url, response.headers.get("content-type", ""))
            content, ext, is_marker = self._prepare(content, ext)
            if content is None:          # 追踪像素
                self.skipped.add(url)
                return None

            # usedforsecurity=False：这里只做文件名去重，不涉及任何安全用途。
            # 显式声明可以让 bandit 的 B324(HIGH) 不再误报，避免真问题被淹没。
            img_hash = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()
            filename = f"images/{img_hash}.{ext}"
            self.images[url] = {
                "id": f"img_{img_hash}",
                "content": content,
                "mime": f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}",
                "filename": filename,
                "is_marker": is_marker,
            }
            return filename
        return None

    @staticmethod
    def _guess_ext(url, content_type):
        """优先信 content-type，其次看 URL 后缀。"""
        for mime, ext in (("image/png", "png"), ("image/gif", "gif"), ("image/webp", "webp")):
            if mime in content_type:
                return ext
        path_ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
        return path_ext if path_ext in ("png", "gif", "webp", "jpeg", "jpg") else "jpg"
