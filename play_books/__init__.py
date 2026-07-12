"""Reusable core for the Google Play Books downloader.

The logic that used to live inside the ``google-play-book-downloader-pdf.py``
CLI script now lives here so that it can be shared between the CLI and the web
GUI (``web/server.py``).
"""

from .downloader import (
    DownloadProgress,
    DownloadResult,
    build_epub,
    build_pdf,
    decipher_key,
    decrypt,
    decrypt_segment,
    download_book,
    download_page,
    download_segments,
    extract_book_id,
    extract_decryption_key,
    extract_toc,
    fetch_book_info,
    fetch_manifest,
    fetch_segment,
    mime_to_ext,
    parse_curl_command,
    unescape_html,
)

__all__ = [
    "DownloadProgress",
    "DownloadResult",
    "build_epub",
    "build_pdf",
    "decipher_key",
    "decrypt",
    "decrypt_segment",
    "download_book",
    "download_page",
    "download_segments",
    "extract_book_id",
    "extract_decryption_key",
    "extract_toc",
    "fetch_book_info",
    "fetch_segment",
    "fetch_manifest",
    "mime_to_ext",
    "parse_curl_command",
    "unescape_html",
]
