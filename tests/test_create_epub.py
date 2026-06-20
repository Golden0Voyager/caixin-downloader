"""Minimal test for create_epub — the only testable pure function in main.py."""

from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# ── Block heavy Playwright + questionary imports before main is loaded ──
_MOCK_MODULES = {
    "playwright": MagicMock(),
    "playwright.async_api": MagicMock(),
    "questionary": MagicMock(),
}
for mod_name, mock in _MOCK_MODULES.items():
    sys.modules[mod_name] = mock

# Now it's safe to import main; create_epub is defined in it at module scope.
import main as caixin_main  # noqa: E402


@pytest.fixture()
def sample_info() -> dict:
    return {"issue_title": "《财新周刊》2026年第25期"}


@pytest.fixture()
def sample_articles() -> list[dict]:
    return [
        {
            "title": "深度报道：AI 时代的就业变局",
            "html": "<div class='content-body'><p>文章正文内容...</p></div>",
        },
        {
            "title": "经济观察：下半年宏观政策展望",
            "html": "<div class='content-body'><p>经济分析内容...</p><p>第二段...</p></div>",
        },
    ]


@pytest.fixture()
def sample_image_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.images = {
        "https://example.com/img1.jpg": {
            "id": "img_abc123",
            "filename": "images/img_abc123.jpg",
            "mime": "image/jpeg",
            "content": b"fake-image-bytes",
        },
    }
    return mgr


class TestCreateEpub:
    def test_creates_valid_epub_file(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name

        try:
            with patch.object(caixin_main.console, "print"):
                caixin_main.create_epub(
                    sample_articles, sample_info, sample_image_manager, out_path
                )

            assert os.path.exists(out_path)
            assert os.path.getsize(out_path) > 0

            # Verify it's a valid ZIP-based EPUB
            with open(out_path, "rb") as f:
                sig = f.read(4)
            # ZIP magic bytes: PK\x03\x04
            assert sig == b"PK\x03\x04", "Not a valid ZIP (EPUB) file"

        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_output_filename_is_respected(
        self, sample_articles: list[dict], sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        custom_path = "/tmp/test_caixin_epub.epub"
        try:
            with patch.object(caixin_main.console, "print"):
                caixin_main.create_epub(
                    sample_articles, sample_info, sample_image_manager, custom_path
                )

            assert os.path.exists(custom_path)
        finally:
            if os.path.exists(custom_path):
                os.unlink(custom_path)

    def test_handles_cover_image(
        self, sample_articles: list[dict], sample_image_manager: MagicMock
    ) -> None:
        info_with_cover = {
            "issue_title": "《财新周刊》2026年第26期",
            "cover_data": b"fake-cover-image-bytes",
        }
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name

        try:
            with patch.object(caixin_main.console, "print"):
                caixin_main.create_epub(
                    sample_articles, info_with_cover, sample_image_manager, out_path
                )

            assert os.path.exists(out_path)
            assert os.path.getsize(out_path) > 0
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_empty_articles_creates_minimal_epub(
        self, sample_info: dict, sample_image_manager: MagicMock
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            out_path = f.name

        try:
            with patch.object(caixin_main.console, "print"):
                caixin_main.create_epub([], sample_info, sample_image_manager, out_path)

            assert os.path.exists(out_path)
            assert os.path.getsize(out_path) > 0
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)
