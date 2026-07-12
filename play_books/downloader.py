#!/usr/bin/env python3
"""Core download logic for Google Play Books.

This module is deliberately free of any user-interface concerns so that it can
be driven equally well by the command-line script or by the web GUI. Progress
is reported through a callback and cancellation is cooperative via a callable,
which keeps the module agnostic about how it is being run.
"""

import argparse
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

# Wait between requests to reduce the risk of getting flagged for abuse.
GOOGLE_PAGE_DOWNLOAD_PACER = 0.1

# Per-request timeout (connect + between-bytes read) so a stalled connection
# can't hang a download thread forever.
REQUEST_TIMEOUT = 30

logger = logging.getLogger(__name__)


class CancelledError(Exception):
    """Raised when a download is cancelled through the ``cancel_check`` hook."""


@dataclass
class DownloadProgress:
    """A snapshot of an in-flight download, passed to ``progress_callback``."""

    status: str = "starting"
    book_id: str = ""
    title: str = ""
    percentage: int = 0
    message: str = ""
    current_page: int = 0
    total_pages: int = 0
    failed_pages: int = 0
    eta_seconds: Optional[int] = None


@dataclass
class DownloadResult:
    """The outcome of a successful (or partial) download."""

    book_id: str
    title: str
    book_dir: str
    total_pages: int
    downloaded_pages: int
    failed_pages: int
    pdf_path: Optional[str] = None
    files: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# curl.txt parsing (session cookies + headers)
# ---------------------------------------------------------------------------
def parse_curl_command(curl_command: str):
    """Parse a ``Copy as cURL`` command into ``(url, cookies, headers)``.

    Handles both POSIX shell quoting and Windows ``cmd.exe`` caret escaping so
    the command can be pasted verbatim regardless of the browser/OS it came from.
    """

    # Windows cmd.exe uses ^ as an escape character, so browsers put them when
    # copying a request as cURL, which breaks our command parsing.
    # See: https://github.com/devnoname120/google-play-book-downloader/issues/28
    def is_likely_cmd_exe_command(cmd: str):
        return re.search(r"\\^$", cmd, re.MULTILINE) or '^\\^"' in cmd or '^"^' in cmd

    if is_likely_cmd_exe_command(curl_command):
        logger.info(
            "The command seems to be for Windows cmd.exe "
            "(normal if you copied it from your browser running on Windows)"
        )
        import mslex

        def normalize_windows_cmd_caret(s: str) -> str:
            s = s.replace("\r\n", "\n").strip()
            s = re.sub(r"\s*\^\s*\n\s*", " ", s)
            s = re.sub(r"\^(.)", r"\1", s)
            return s

        try:
            [prog_name, *arg_list] = mslex.split(normalize_windows_cmd_caret(curl_command))
        except ValueError:
            logger.warning(
                "Failed to parse the command as a Windows command! Will try again "
                'assuming it is a command for Linux/macOS shells (NOT normal unless '
                'you used the option "Copy as cURL (bash)")'
            )
            import shlex

            [prog_name, *arg_list] = shlex.split(curl_command.strip())
    else:
        logger.info(
            "The command seems to be for Linux/macOS shells (normal if copied on "
            'these OSs, or from Windows using the option "Copy as cURL (bash)")'
        )
        import shlex

        [prog_name, *arg_list] = shlex.split(curl_command.strip())

    if prog_name != "curl":
        raise ValueError(
            f"Invalid curl command. The program name should be 'curl' but it is: "
            f"{prog_name}. Make sure you followed the instructions properly!"
        )

    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--header", "-H", action="append", dest="headers")
    parser.add_argument("--cookie", "-b", action="append", dest="cookies")

    args, _ = parser.parse_known_args(arg_list)

    url = urlparse(args.url)

    headers = {}
    if not args.headers:
        logger.warning(
            "No headers detected! You likely didn't properly copy the cURL request."
        )
    else:
        for header in args.headers:
            key, value = header.split(":", 1)
            headers[key.strip()] = value.strip()

    cookies_raw = args.cookies or []
    cookies = {}
    # Firefox puts the cookies in the headers directly instead of using curl's
    # --cookie/-b parameters.
    if "Cookie" in headers:
        if cookies_raw:
            logger.warning(
                "Cookies appear in both --cookie argument and headers, check the cURL request."
            )
        cookies_raw.append(headers["Cookie"])

    if not cookies_raw:
        logger.error(
            "No cookies detected! You didn't properly copy the cURL request so the "
            "book will not be able to download correctly."
        )
    else:
        for cookie in cookies_raw:
            cookie_parts = cookie.split(";")
            for cookie in cookie_parts:
                if "=" in cookie:
                    key, value = cookie.split("=", 1)
                    cookies[key.strip()] = value.strip()
                else:
                    logger.warning(f"Invalid cookie (no assignment): {cookie}")

    return url, cookies, headers


def extract_book_id(text: str) -> str:
    """Extract a Play Books volume id from a raw id or a reader/store URL."""

    text = (text or "").strip()
    if not text:
        return ""

    # https://play.google.com/books/reader?id=XXXX or store details?id=XXXX
    match = re.search(r"[?&]id=([^&\s]+)", text)
    book_id = match.group(1) if match else text

    # The id becomes a directory name, so reject anything that could escape it.
    if "/" in book_id or "\\" in book_id or ".." in book_id:
        return ""

    return book_id


# ---------------------------------------------------------------------------
# Decryption key + table of contents extraction
# ---------------------------------------------------------------------------
def unescape_html(text):
    return re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)


def extract_decryption_key(google_reader_body):
    try:
        key_search = re.search(
            r'<body[\s\S]*?<[^>]+src\s*=\s*["\']data:.*?base64,([^"\']+)["\']',
            google_reader_body,
        )
        key_data = key_search.group(1)
        logger.info(f"Ciphered decryption key: {base64.b64decode(key_data)}")
    except Exception as e:
        raise Exception(
            "Failed to extract the encoded decryption key from Play Book Reader's HTML body."
        ) from e

    return decipher_key(base64.b64decode(key_data, validate=True))


def decipher_key(str_data):
    groups = re.findall(r"(\D+\d)", str_data.decode())
    if len(groups) != 128:
        logger.warning(
            f"Unexpected count of AES key groups. Expected: 128, got: {len(groups)}. "
            "Ignoring the error and continuing…"
        )

    bitfield = [str(1 if s[int(s[-1])] == s[-2] else 0) for s in groups]
    shift = 64 % len(bitfield)

    if shift > 0:
        bitfield = bitfield[-shift:] + bitfield[:-shift]
    elif shift < 0:
        bitfield = bitfield[-shift:] + bitfield[0:-shift]

    key = []
    for pos in range(0, len(bitfield), 8):
        bin_str = "".join(reversed(bitfield[pos : pos + 8]))
        key.append(int(bin_str, 2))
    return bytes(key)


def extract_toc(google_reader_body):
    try:
        toc_data = re.search(
            r'"toc_entry":\s*(\[[\s\S]*?}\s*])', google_reader_body
        ).group(1)
    except Exception as e:
        logger.warning(
            f"Failed to extract the table of contents from the book's main page. Error: {e}"
        )
        return None

    try:
        return json.loads(toc_data)
    except Exception as e:
        logger.warning(
            f"Failed to parse the table of contents as JSON. Content: {toc_data} Error: {e}"
        )
        return None


# ---------------------------------------------------------------------------
# Page download + decryption
# ---------------------------------------------------------------------------
def download_page(src, cookies, headers):
    url_parts = list(urlparse(src))
    query = parse_qs(url_parts[4])
    query.update(
        {
            # Arbitrarily high numbers to make sure we retrieve the highest resolution.
            "w": ["10000"],
            "h": ["10000"],
            "zoom": ["3"],  # Zoom values 1 and 2 are for thumbnails (degraded quality).
            "enc_all": ["1"],
            "img": ["1"],
        }
    )
    url_parts[4] = urlencode(query, doseq=True)
    page_url = urlunparse(url_parts)
    logger.debug(f"Downloading url: {page_url}")

    response = requests.get(page_url, cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    mime_type = response.headers.get("content-type")
    buffer = response.content

    return mime_type, buffer


def decrypt(buf, aes_key):
    # Imported lazily so the module (and the web UI) can load even when
    # pycryptodomex isn't installed; only real downloads need it.
    from Cryptodome.Cipher import AES

    iv = buf[:16]
    data = buf[16:]

    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    return cipher.decrypt(data)


def decrypt_segment(buf, aes_key):
    """Decrypt a reflowable text segment.

    Segments use the same AES-CBC scheme as page images but prefix the payload
    with a little-endian length, and the plaintext is a UTF-8 JSON string.
    """
    from Cryptodome.Cipher import AES

    iv = buf[:16]
    expected_length = int.from_bytes(buf[16:20], "little")
    data = buf[20:]

    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(data)
    return decrypted[:expected_length].decode("utf-8")


def fetch_segment(url, cookies, headers):
    """Fetch a single reflowable segment (base64-wrapped, encrypted JSON)."""
    segment_url = urlparse(url)
    query = parse_qs(segment_url.query)
    query["enc_all"] = ["1"]
    query["hl"] = ["en"]  # Fix encoding issues with Cyrillic.
    segment_url = segment_url._replace(query=urlencode(query, doseq=True))

    response = requests.get(
        urlunparse(segment_url), cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response


def mime_to_ext(mime):
    lookup = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/webp": "webp",
        "image/apng": "apng",
        "image/jp2": "jp2",
        "image/jpx": "jpx",
        "image/jpm": "jpm",
        "image/bmp": "bmp",
        "image/svg+xml": "svg",
    }
    return lookup.get(mime, "unk")


# ---------------------------------------------------------------------------
# High-level helpers used by both the CLI and the web GUI
# ---------------------------------------------------------------------------
def fetch_manifest(book_id: str, cookies: dict, headers: dict) -> dict:
    """Fetch and return the book manifest (metadata, pages, toc)."""

    response = requests.get(
        f"https://play.google.com/books/volumes/{book_id}/manifest"
        f"?hl=en&authuser=2&source=ge-web-app",
        cookies=cookies,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return json.loads(response.text)


def cover_url_for(book_id: str) -> str:
    """Public Google Books cover thumbnail (no authentication required)."""

    return (
        f"https://books.google.com/books/content?id={book_id}"
        f"&printsec=frontcover&img=1&zoom=1&source=gbs_api"
    )


def fetch_book_info(book_id: str, cookies: dict, headers: dict) -> dict:
    """Return light-weight book metadata for display before downloading."""

    manifest = fetch_manifest(book_id, cookies, headers)
    metadata = manifest.get("metadata", {})

    pages = manifest.get("page", []) or []
    total = len(pages)
    missing = sum(1 for p in pages if not p.get("src") or not isinstance(p["src"], str))
    preview = metadata.get("preview")

    segments = manifest.get("segment", []) or []
    num_segments = len(segments)

    return {
        "id": book_id,
        "volume_id": metadata.get("volume_id", book_id),
        "title": unescape_html(metadata.get("title", "") or ""),
        "authors": unescape_html(metadata.get("authors", "") or ""),
        "publisher": metadata.get("publisher", ""),
        "pub_date": metadata.get("pub_date", ""),
        "language": manifest.get("language", ""),
        "num_pages": total,
        "downloadable_pages": total - missing,
        "missing_pages": missing,
        "num_segments": num_segments,
        # Which output formats this book actually supports.
        "has_scanned": total > 0,       # "Original Pages" → images → PDF
        "has_reflowable": num_segments > 0,  # flowing text → segments → EPUB
        "preview": preview,
        "is_full": preview == "full",
        "cover_url": cover_url_for(metadata.get("volume_id", book_id)),
    }


def _noop(*_args, **_kwargs):
    return None


def download_book(
    book_id: str,
    cookies: dict,
    headers: dict,
    output_dir: str = "books",
    progress_callback: Callable[[DownloadProgress], None] = _noop,
    cancel_check: Callable[[], bool] = lambda: False,
    pacer: float = GOOGLE_PAGE_DOWNLOAD_PACER,
) -> DownloadResult:
    """Download and decrypt every page of a book into ``output_dir/book_id``.

    Progress is reported through ``progress_callback`` and the download can be
    interrupted cooperatively by returning ``True`` from ``cancel_check``.
    """

    def emit(progress: DownloadProgress):
        try:
            progress_callback(progress)
        except Exception:  # A misbehaving UI callback must never break a download.
            logger.exception("progress_callback raised")

    def check_cancel():
        if cancel_check():
            raise CancelledError("Download cancelled")

    book_dir = Path(output_dir) / book_id
    book_dir.mkdir(parents=True, exist_ok=True)

    emit(DownloadProgress(status="fetching", book_id=book_id, message="Fetching book metadata…"))

    # &hl=en is necessary to fix encoding issues with Cyrillic.
    reader_response = requests.get(
        f"https://play.google.com/books/reader?id={book_id}&hl=en",
        cookies=cookies,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    body = reader_response.text

    aes_key = extract_decryption_key(body)
    (book_dir / "aes_key.bin").write_bytes(aes_key)
    logger.info(f"Found AES decryption key: [{aes_key.hex()}]")

    toc = extract_toc(body)

    manifest = fetch_manifest(book_id, cookies, headers)
    (book_dir / "manifest.json").write_text(json.dumps(manifest, indent=4))

    metadata = manifest.get("metadata", {})
    title = unescape_html(metadata.get("title", "") or book_id)

    if metadata.get("preview") != "full":
        logger.error(
            "The server indicates that the book is in preview mode "
            f"'{metadata.get('preview')}' (expected 'full'). This either means that "
            "you don't own the book on this account, or that your session is invalid/expired."
        )

    if not toc:
        toc = manifest.get("toc_entry")
        if toc:
            logger.warning(
                "Using the table of contents from the manifest as a fallback. Note "
                "that it's inferior because everything is flattened to the top level."
            )
        else:
            logger.error("Couldn't find the table of contents in the book manifest")

    if toc:
        (book_dir / "toc.json").write_text(json.dumps(toc, indent=4))
        logger.info("Extracted the table of contents to toc.json")
        try:
            human_toc = "\n".join(
                f"{'    ' * t['depth']}{unescape_html(t['label'])} ........".ljust(80, ".")
                + f" p.{t['page_index'] + 1}"
                for t in toc
            )
            (book_dir / "toc.txt").write_text(human_toc)
            logger.info("Wrote human-readable table of contents to toc.txt")
        except Exception as e:
            logger.warning(f"Couldn't produce a human-readable table of contents:\n{e}")

    pages = manifest.get("page", []) or []
    total = len(pages)
    missing_pages = [p["pid"] for p in pages if not p.get("src") or not isinstance(p["src"], str)]
    if missing_pages:
        missing_percent = f"{(len(missing_pages) / total):.2%}" if total else "0%"
        logger.error(
            f"Couldn't find a download link for {len(missing_pages)} pages "
            f"({missing_percent} missing, total: {total} pages)."
        )

    page_files = []
    failed = 0
    start_time = time.monotonic()

    logger.info(f"Starting to download {total} pages…")
    emit(
        DownloadProgress(
            status="downloading",
            book_id=book_id,
            title=title,
            total_pages=total,
            message=f"Downloading {total} pages…",
        )
    )

    for i, page in enumerate(pages):
        check_cancel()

        pid, src = page.get("pid"), page.get("src")
        page_no = i + 1

        if src:
            try:
                mime_type, buf_enc = download_page(src, cookies, headers)
                buf = decrypt(buf_enc, aes_key)

                ext = mime_to_ext(mime_type)
                filename = f"{pid}.{ext}"
                (book_dir / filename).write_bytes(buf)
                page_files.append(filename)
                logger.info(f"[{page_no}/{total}] Saved to {filename}")
            except CancelledError:
                raise
            except Exception as e:
                failed += 1
                logger.error(f"[{page_no}/{total}] Download or decrypt failed with {e}")
        else:
            failed += 1
            logger.error(f"[{page_no}/{total}] Skipped: download link for {pid} is missing…")

        elapsed = time.monotonic() - start_time
        eta = int((elapsed / page_no) * (total - page_no)) if page_no else None
        emit(
            DownloadProgress(
                status="downloading",
                book_id=book_id,
                title=title,
                total_pages=total,
                current_page=page_no,
                failed_pages=failed,
                percentage=int(page_no / total * 100) if total else 0,
                eta_seconds=eta,
                message=f"Page {page_no} of {total}",
            )
        )

        time.sleep(pacer)  # Be gentle with Google Play Books.

    (book_dir / "pages.txt").write_text("\n".join(page_files))
    logger.info(f'Finished. Downloaded pages can be found in "{book_dir}".')

    result = DownloadResult(
        book_id=book_id,
        title=title,
        book_dir=str(book_dir),
        total_pages=total,
        downloaded_pages=len(page_files),
        failed_pages=failed,
    )
    result.files["pages"] = str(book_dir)

    emit(
        DownloadProgress(
            status="downloaded",
            book_id=book_id,
            title=title,
            total_pages=total,
            current_page=total,
            failed_pages=failed,
            percentage=100,
            message=f"Downloaded {len(page_files)} of {total} pages",
        )
    )

    return result


def build_pdf(book_dir: str) -> str:
    """Build a PDF (with metadata + TOC) from previously downloaded pages.

    Requires the optional ``img2pdf`` and ``pikepdf`` dependencies. Returns the
    path to the generated PDF.
    """

    # Imported lazily: PDF building is optional and pulls in heavy dependencies.
    from play_book_pdf_tool.play_book_pdf_tool import (
        add_metadata,
        add_toc,
        create_pdf,
        generate_output_pdf_filename,
    )
    from pikepdf import Pdf

    base_path = Path(book_dir)
    manifest = json.loads((base_path / "manifest.json").read_text(encoding="UTF-8"))

    pages_filename = (base_path / "pages.txt").read_text(encoding="UTF-8").splitlines()

    if manifest.get("is_right_to_left"):
        from pydash import _

        logger.info(
            "The manifest indicates right-to-left page ordering; swapping page order."
        )
        front = _.head(pages_filename)
        back = _.last(pages_filename)
        reversed_middle = _(pages_filename).initial().tail().chunk(2).map(_.reverse).flatten()
        pages_filename = reversed_middle.unshift(front).push(back).value()

    page_paths = [str(base_path / fn) for fn in pages_filename if fn]
    if not page_paths:
        raise RuntimeError("No downloaded pages found to build a PDF from.")

    tmp_pdf = base_path / "book-tmp.pdf"
    create_pdf(page_paths, tmp_pdf)

    with Pdf.open(tmp_pdf) as pdf:
        add_metadata(base_path, pdf)
        try:
            add_toc(base_path, pdf)
        except Exception as e:  # A missing/empty TOC shouldn't fail the whole build.
            logger.warning(f"Skipping table of contents: {e}")

        filename = generate_output_pdf_filename(base_path)
        output_pdf = base_path / filename
        pdf.save(str(output_pdf), linearize=True)

    if tmp_pdf.exists():
        tmp_pdf.unlink()

    logger.info(f'PDF saved to "{output_pdf}"')
    return str(output_pdf)


def download_segments(
    book_id: str,
    cookies: dict,
    headers: dict,
    output_dir: str = "books",
    progress_callback: Callable[[DownloadProgress], None] = _noop,
    cancel_check: Callable[[], bool] = lambda: False,
    pacer: float = GOOGLE_PAGE_DOWNLOAD_PACER,
) -> DownloadResult:
    """Download and decrypt every reflowable text *segment* of a book.

    Each segment is written as ``<label>.xhtml`` (+ ``<label>.css``) alongside the
    ``manifest.json`` — exactly what :func:`build_epub` needs to reconstruct an EPUB.
    Only books with a "flowing text" edition expose segments.
    """

    def emit(progress: DownloadProgress):
        try:
            progress_callback(progress)
        except Exception:
            logger.exception("progress_callback raised")

    def check_cancel():
        if cancel_check():
            raise CancelledError("Download cancelled")

    book_dir = Path(output_dir) / book_id
    book_dir.mkdir(parents=True, exist_ok=True)

    emit(DownloadProgress(status="fetching", book_id=book_id, message="Fetching book metadata…"))

    # &hl=en is necessary to fix encoding issues with Cyrillic.
    reader_response = requests.get(
        f"https://play.google.com/books/reader?id={book_id}&hl=en",
        cookies=cookies,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    body = reader_response.text

    aes_key = extract_decryption_key(body)
    (book_dir / "aes_key.bin").write_bytes(aes_key)

    manifest = fetch_manifest(book_id, cookies, headers)
    (book_dir / "manifest.json").write_text(json.dumps(manifest, indent=4))

    metadata = manifest.get("metadata", {})
    title = unescape_html(metadata.get("title", "") or book_id)

    if metadata.get("preview") != "full":
        logger.error(
            "The server indicates that the book is in preview mode "
            f"'{metadata.get('preview')}' (expected 'full'). You may not own this "
            "book on this account, or your session may be invalid/expired."
        )

    segments = manifest.get("segment", []) or []
    total = len(segments)
    if total == 0:
        raise RuntimeError(
            "This book has no reflowable text segments (no EPUB edition available). "
            "Try the PDF format if it has scanned 'Original Pages' instead."
        )

    (book_dir / "segments.txt").write_text(
        "".join(f"{s.get('label') or ''}\n" for s in segments)
    )

    logger.info(f"Starting to download {total} segments…")
    emit(
        DownloadProgress(
            status="downloading",
            book_id=book_id,
            title=title,
            total_pages=total,
            message=f"Downloading {total} segments…",
        )
    )

    saved = 0
    failed = 0
    start_time = time.monotonic()

    for i, segment in enumerate(segments):
        check_cancel()
        seg_no = i + 1
        # `or` (not a .get default) so an explicit "label": null also falls back,
        # otherwise every null-labelled segment would clobber None.xhtml.
        label = segment.get("label") or f"segment-{seg_no}"

        try:
            url = "https://play.google.com" + segment["link"]
            enc_b64 = fetch_segment(url, cookies, headers).text
            decrypted = decrypt_segment(base64.b64decode(enc_b64), aes_key)
            segment_obj = json.loads(decrypted)

            (book_dir / f"{label}.xhtml").write_text(segment_obj["content"], encoding="utf-8")
            (book_dir / f"{label}.css").write_text(segment_obj.get("style", ""), encoding="utf-8")
            saved += 1
            logger.info(f"[{seg_no}/{total}] Saved segment {label}")
        except CancelledError:
            raise
        except Exception as e:
            failed += 1
            logger.error(f"[{seg_no}/{total}] Segment {label} failed with {e}")

        elapsed = time.monotonic() - start_time
        eta = int((elapsed / seg_no) * (total - seg_no)) if seg_no else None
        emit(
            DownloadProgress(
                status="downloading",
                book_id=book_id,
                title=title,
                total_pages=total,
                current_page=seg_no,
                failed_pages=failed,
                percentage=int(seg_no / total * 100) if total else 0,
                eta_seconds=eta,
                message=f"Segment {seg_no} of {total}",
            )
        )

        time.sleep(pacer)  # Be gentle with Google Play Books.

    logger.info(f'Finished. Downloaded segments can be found in "{book_dir}".')

    result = DownloadResult(
        book_id=book_id,
        title=title,
        book_dir=str(book_dir),
        total_pages=total,
        downloaded_pages=saved,
        failed_pages=failed,
    )

    emit(
        DownloadProgress(
            status="downloaded",
            book_id=book_id,
            title=title,
            total_pages=total,
            current_page=total,
            failed_pages=failed,
            percentage=100,
            message=f"Downloaded {saved} of {total} segments",
        )
    )

    return result


def build_epub(book_dir: str) -> str:
    """Reconstruct a valid, self-contained EPUB from downloaded segments.

    Requires the optional ``ebooklib`` and ``beautifulsoup4`` dependencies. Returns
    the path to the generated EPUB.
    """

    # Imported lazily: EPUB building is optional and pulls in extra dependencies.
    from play_book_epub_tool.play_book_epub_tool import build_epub as reconstruct_epub

    output = reconstruct_epub(Path(book_dir))
    logger.info(f'EPUB saved to "{output}"')
    return str(output)
