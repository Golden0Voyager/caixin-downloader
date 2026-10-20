"""ImageManager 的纯逻辑测试：URL 规范化 + 图片分级（正文图 / 文末标志 / 追踪像素）。

回归背景：财新的图片地址混用绝对与协议相对两种写法，而 `images` 字典
是以规范化后的 URL 为键的。若调用方拿原始 URL 去查 `is_marker`，
协议相对的装饰图会被误判成正文图并渲染成整页大小。
"""

from __future__ import annotations

import asyncio
import io
from unittest.mock import MagicMock

import pytest
from PIL import Image

import caixin.images as images_module  # noqa: E402
from caixin.config import MARKER_IMAGE_DIM, MAX_IMAGE_WIDTH  # noqa: E402
from caixin.images import ImageManager  # noqa: E402


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """重试退避在测试里没意义，置 0 让用例保持秒级。"""
    monkeypatch.setattr(images_module, "RETRY_BACKOFF_SECONDS", 0)

# ── normalize_url ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 协议相对 -> 补 https
        ("//datanews.caixin.com/a/editorIcon.png",
         "https://datanews.caixin.com/a/editorIcon.png"),
        # 绝对 URL 原样返回
        ("https://img.caixin.com/2026-10-10/x.jpg", "https://img.caixin.com/2026-10-10/x.jpg"),
        ("http://img.caixin.com/x.jpg", "http://img.caixin.com/x.jpg"),
        # 站内相对路径、空值一律视为不可下载
        ("/relative/a.jpg", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_url(raw, expected):
    assert ImageManager.normalize_url(raw) == expected


def test_normalize_url_is_idempotent():
    """规范化两次结果不变，保证 download() 与调用方使用同一个键。"""
    once = ImageManager.normalize_url("//img.caixin.com/a.jpg")
    assert ImageManager.normalize_url(once) == once


# ── _prepare 分级 ──────────────────────────────────────────────────────

def _png(size, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size, (10, 20, 30) if mode == "RGB" else (10, 20, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_drops_tracking_pixel():
    """长边 < MIN_IMAGE_DIM 视为追踪像素，丢弃。"""
    content, ext, is_marker = ImageManager._prepare(_png((1, 1)), "png")
    assert content is None and ext is None and is_marker is False


def test_prepare_keeps_marker_at_native_size():
    """文末「财」字标志 / 署名图标：保留原始像素，仅统一转 PNG，标记 is_marker。

    回归点：曾把 16×16 的标志插值放大到 96×96，结果又大又糊 ——
    放大不会增加细节。显示尺寸必须交给 CSS 的 max-width。
    """
    content, ext, is_marker = ImageManager._prepare(_png((16, 16)), "png")
    assert is_marker is True and ext == "png"
    with Image.open(io.BytesIO(content)) as im:
        assert im.size == (16, 16), "装饰图不应被放大"


def test_prepare_marker_never_upscaled():
    """任意尺寸的装饰图都保持原始像素。"""
    for size in [(16, 16), (67, 67), (119, 119), (119, 20)]:
        content, _, is_marker = ImageManager._prepare(_png(size), "png")
        assert is_marker is True
        with Image.open(io.BytesIO(content)) as im:
            assert im.size == size, f"{size} 被改成了 {im.size}"


def test_prepare_converts_ico_like_marker_to_png():
    """服务端把 ICO 谎报成 jpeg 时，也应按真实像素转成 PNG。"""
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), (20, 40, 120, 255)).save(buf, format="ICO")
    content, ext, is_marker = ImageManager._prepare(buf.getvalue(), "jpg")
    assert (ext, is_marker) == ("png", True)
    with Image.open(io.BytesIO(content)) as im:
        assert im.format == "PNG" and im.size == (16, 16)


def test_prepare_leaves_normal_image_untouched():
    """尺寸合规且无透明通道的正文图：原样返回字节，避免二次压缩。"""
    raw = _png((800, 533))
    content, ext, is_marker = ImageManager._prepare(raw, "png")
    assert content == raw and is_marker is False


def test_prepare_downscales_oversized_image():
    content, ext, is_marker = ImageManager._prepare(_png((3333, 4714)), "png")
    assert is_marker is False and ext == "jpg"
    with Image.open(io.BytesIO(content)) as im:
        assert im.size == (MAX_IMAGE_WIDTH, round(4714 * MAX_IMAGE_WIDTH / 3333))


def test_prepare_keeps_alpha_when_downscaling():
    """带透明通道的图缩小时必须存成 PNG —— 存 JPEG 会让透明区变成黑块。"""
    buf = io.BytesIO()
    Image.new("RGBA", (MAX_IMAGE_WIDTH + 200, 600), (10, 20, 30, 128)).save(buf, format="PNG")
    content, ext, is_marker = ImageManager._prepare(buf.getvalue(), "png")
    assert (ext, is_marker) == ("png", False)
    with Image.open(io.BytesIO(content)) as im:
        assert im.size == (MAX_IMAGE_WIDTH, round(600 * MAX_IMAGE_WIDTH / (MAX_IMAGE_WIDTH + 200)))
        assert im.mode == "RGBA"


def test_prepare_boundary_between_marker_and_normal():
    """刚好达到 MARKER_IMAGE_DIM 的图按正文图处理。"""
    _, _, is_marker = ImageManager._prepare(_png((MARKER_IMAGE_DIM, MARKER_IMAGE_DIM)), "png")
    assert is_marker is False


def test_prepare_undecodable_keeps_raw_bytes():
    """无法解码（例如服务端把 ICO 谎报成 jpeg）时保留原始字节，不抛异常。"""
    raw = b"\x00\x01not-an-image\x02"
    assert ImageManager._prepare(raw, "jpg") == (raw, "jpg", False)


# ── download 的键一致性 ────────────────────────────────────────────────

def test_download_registers_under_normalized_url():
    """download() 收到协议相对 URL 时，必须能以规范化 URL 反查到 is_marker。

    回归点：scrape_article 是拿「原始 URL」查 is_marker 的。此前 download()
    把键规范化成 https://，调用方却用 // 开头的原始 URL 查，导致装饰图
    （「显影」全篇图片都是协议相对写法）被当成正文图渲染成整页大小。
    """
    raw = _png((16, 16))
    im = ImageManager(context=MagicMock())

    response = MagicMock()
    response.status = 200
    response.headers = {"content-type": "image/png"}

    async def _body():
        return raw

    response.body = _body

    async def _get(url, timeout=None):
        return response

    im.context.request.get = _get

    rel = "//datanews.caixin.com/mobile/article/common/images/editorIcon.png"
    filename = asyncio.run(im.download(rel))
    assert filename is not None

    # 无论用规范化 URL 还是原始 URL，查到的都应是同一份记录
    normalized = ImageManager.normalize_url(rel)
    assert im.images[normalized]["is_marker"] is True
    assert im.images[normalized]["filename"] == filename


# ── download 的并发与重试 ─────────────────────────────────────────────


def _ok_response(raw: bytes):
    response = MagicMock()
    response.status = 200
    response.headers = {"content-type": "image/png"}

    async def _body():
        return raw

    response.body = _body
    return response


def test_download_dedupes_concurrent_requests():
    """同一 URL 被并发请求时只能真正下载一次。

    回归点：`images` 缓存只在下载**完成**后才写入，并发下两个协程会同时
    看到「未命中」然后各下一遍。文末「财」字标志一期出现 23 次，很容易撞上。
    """
    raw = _png((16, 16))
    calls: list[str] = []

    async def _get(url, timeout=None):
        calls.append(url)
        await asyncio.sleep(0.05)   # 制造重叠窗口
        return _ok_response(raw)

    im = ImageManager(context=MagicMock())
    im.context.request.get = _get
    url = "https://img.caixin.com/marker.png"

    async def _run():
        return await asyncio.gather(*(im.download(url) for _ in range(4)))

    results = asyncio.run(_run())
    assert len(calls) == 1, f"重复下载了 {len(calls)} 次"
    assert len(set(results)) == 1 and results[0] is not None


def test_download_retries_on_server_error():
    """502/503 是「正常返回的失败」，必须重试而不是直接放弃。"""
    raw = _png((16, 16))
    calls: list[int] = []

    async def _get(url, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            response = MagicMock()
            response.status = 503
            response.headers = {}
            return response
        return _ok_response(raw)

    im = ImageManager(context=MagicMock())
    im.context.request.get = _get

    filename = asyncio.run(im.download("https://img.caixin.com/flaky.png", retries=2))
    assert filename is not None
    assert len(calls) == 2, "第一次 503 之后应当再试一次"


def test_download_gives_up_after_retries():
    calls: list[int] = []

    async def _get(url, timeout=None):
        calls.append(1)
        response = MagicMock()
        response.status = 500
        response.headers = {}
        return response

    im = ImageManager(context=MagicMock())
    im.context.request.get = _get

    assert asyncio.run(im.download("https://img.caixin.com/dead.png", retries=2)) is None
    assert len(calls) == 2


def test_download_returns_none_for_unusable_url():
    """相对路径 / 空值根本不该发起请求。"""
    im = ImageManager(context=MagicMock())
    assert asyncio.run(im.download("")) is None
    assert asyncio.run(im.download("/relative/a.jpg")) is None


def test_download_hits_cache_on_second_call():
    raw = _png((16, 16))
    calls: list[int] = []

    async def _get(url, timeout=None):
        calls.append(1)
        return _ok_response(raw)

    im = ImageManager(context=MagicMock())
    im.context.request.get = _get
    url = "https://img.caixin.com/marker.png"

    first = asyncio.run(im.download(url))
    second = asyncio.run(im.download(url))
    assert first == second
    assert len(calls) == 1, "第二次应直接命中缓存"


def test_download_remembers_skipped_tracking_pixels():
    """追踪像素判定一次就够了，重复请求不该再下载一遍。"""
    raw = _png((1, 1))
    calls: list[int] = []

    async def _get(url, timeout=None):
        calls.append(1)
        return _ok_response(raw)

    im = ImageManager(context=MagicMock())
    im.context.request.get = _get
    url = "https://img.caixin.com/pixel.png"

    assert asyncio.run(im.download(url)) is None
    assert asyncio.run(im.download(url)) is None
    assert len(calls) == 1
    assert url in im.skipped


def test_download_retries_when_request_raises():
    """连接直接失败（DNS/超时）也要重试，而不是一次就放弃。"""
    raw = _png((16, 16))
    calls: list[int] = []

    async def _get(url, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connect timeout")
        return _ok_response(raw)

    im = ImageManager(context=MagicMock())
    im.context.request.get = _get

    assert asyncio.run(im.download("https://img.caixin.com/flaky2.png", retries=2)) is not None
    assert len(calls) == 2


def test_download_retries_on_empty_body():
    """200 但响应体为空（CDN 缓存穿透）也要重试。"""
    raw = _png((16, 16))
    calls: list[int] = []

    async def _get(url, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            response = MagicMock()
            response.status = 200
            response.headers = {"content-type": "image/png"}

            async def _empty():
                return b""

            response.body = _empty
            return response
        return _ok_response(raw)

    im = ImageManager(context=MagicMock())
    im.context.request.get = _get

    assert asyncio.run(im.download("https://img.caixin.com/empty.png", retries=2)) is not None
    assert len(calls) == 2


def test_forget_swallows_cancelled_task():
    """下载被取消时 `_forget` 不能因为 `task.exception()` 抛 CancelledError 而炸掉。"""
    im = ImageManager(context=MagicMock())
    task = MagicMock()
    task.cancelled.return_value = True
    im._inflight["https://img.caixin.com/x.jpg"] = task

    im._forget("https://img.caixin.com/x.jpg", task)

    assert "https://img.caixin.com/x.jpg" not in im._inflight
    task.exception.assert_not_called()


@pytest.mark.parametrize(
    ("url", "content_type", "expected"),
    [
        # content-type 优先
        ("https://img.caixin.com/x.bin", "image/png", "png"),
        ("https://img.caixin.com/x.bin", "image/gif", "gif"),
        ("https://img.caixin.com/x.bin", "image/webp", "webp"),
        # 服务端常把 ICO 谎报成 jpeg，这时 content-type 说了不算，只能看后缀
        ("https://img.caixin.com/x.png", "image/jpeg", "png"),
        ("https://img.caixin.com/x.jpeg", "image/jpeg", "jpeg"),
        # 带查询串的后缀也要认
        ("https://img.caixin.com/x.webp?w=800", "application/octet-stream", "webp"),
        # 都认不出时兜底 jpg
        ("https://img.caixin.com/x", "", "jpg"),
        ("https://img.caixin.com/x.php", "text/html", "jpg"),
    ],
)
def test_guess_ext(url, content_type, expected):
    assert ImageManager._guess_ext(url, content_type) == expected
