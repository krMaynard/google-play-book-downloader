"""Web server for the Google Play Books downloader.

A tiny ``http.server`` based application (no third-party web framework) that
serves the static frontend and exposes a small JSON API to drive the core
downloader. Mirrors the shape of the O'Reilly ingest web GUI.
"""

import json
import platform
import subprocess
import sys
import threading
import traceback
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

from play_books import downloader
from play_books.downloader import DownloadProgress

# The curl.txt session file lives at the repository root, shared with the CLI.
CURL_FILE = Path(__file__).resolve().parent.parent / "curl.txt"
DEFAULT_OUTPUT_DIR = (Path(__file__).resolve().parent.parent / "books").resolve()

# Whether the optional PDF-building dependencies are importable.
try:
    import img2pdf  # noqa: F401
    import pikepdf  # noqa: F401

    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

# Whether the optional EPUB-building dependencies are importable.
try:
    import bs4  # noqa: F401
    import ebooklib  # noqa: F401

    EPUB_AVAILABLE = True
except ImportError:
    EPUB_AVAILABLE = False


class DownloaderHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the downloader web interface."""

    download_progress: dict = {}
    _progress_lock = threading.Lock()
    _cancel_requested = threading.Event()

    @classmethod
    def _set_progress(cls, data: dict):
        with cls._progress_lock:
            cls.download_progress = data

    def __init__(self, *args, **kwargs):
        self.static_dir = Path(__file__).parent / "static"
        super().__init__(*args, directory=str(self.static_dir), **kwargs)

    # -- session helpers ----------------------------------------------------
    @staticmethod
    def _load_session():
        """Return ``(cookies, headers)`` parsed from curl.txt, or raise ValueError."""
        if not CURL_FILE.exists():
            raise ValueError("No session set. Paste your cURL command first.")
        curl_command = CURL_FILE.read_text(encoding="utf-8").strip()
        if not curl_command:
            raise ValueError("No session set. Paste your cURL command first.")
        url, cookies, headers = downloader.parse_curl_command(curl_command)
        if url.netloc != "play.google.com":
            raise ValueError(
                f"The cURL command should target 'play.google.com' but targets '{url.netloc}'."
            )
        if not cookies:
            raise ValueError("No cookies found in the cURL command.")
        return cookies, headers

    # -- routing ------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/status":
            self._handle_status()
        elif path == "/api/settings":
            self._handle_settings()
        elif path.startswith("/api/book/"):
            # The book id / reader URL is percent-encoded by the browser.
            self._handle_book_info(unquote(path[len("/api/book/") :]))
        elif path == "/api/progress":
            self._handle_progress()
        else:
            super().do_GET()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if self.path == "/api/curl":
            self._handle_save_curl(data)
        elif self.path == "/api/download":
            self._handle_download(data)
        elif self.path == "/api/cancel":
            self._handle_cancel()
        elif self.path == "/api/reveal":
            self._handle_reveal(data)
        else:
            self._send_json({"error": "Not found"}, 404)

    # -- handlers -----------------------------------------------------------
    def _handle_status(self):
        try:
            cookies, _headers = self._load_session()
            self._send_json({"valid": True, "cookie_count": len(cookies)})
        except ValueError as e:
            self._send_json({"valid": False, "reason": str(e)})

    def _handle_settings(self):
        self._send_json(
            {
                "output_dir": str(DEFAULT_OUTPUT_DIR),
                "pdf_available": PDF_AVAILABLE,
                "epub_available": EPUB_AVAILABLE,
            }
        )

    def _handle_save_curl(self, data: dict):
        curl_command = (data.get("curl") or "").strip()
        if not curl_command:
            self._send_json({"error": "Empty cURL command."}, 400)
            return
        try:
            url, cookies, _headers = downloader.parse_curl_command(curl_command)
        except Exception as e:
            self._send_json({"error": f"Could not parse cURL command: {e}"}, 400)
            return
        if url.netloc != "play.google.com":
            self._send_json(
                {"error": f"The cURL command should target 'play.google.com', not '{url.netloc}'."},
                400,
            )
            return
        if not cookies:
            self._send_json({"error": "No cookies found in the cURL command."}, 400)
            return
        try:
            CURL_FILE.write_text(curl_command, encoding="utf-8")
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return
        self._send_json({"success": True, "cookie_count": len(cookies)})

    def _handle_book_info(self, raw_id: str):
        book_id = downloader.extract_book_id(raw_id)
        if not book_id:
            self._send_json({"error": "A book ID is required."}, 400)
            return
        try:
            cookies, headers = self._load_session()
        except ValueError as e:
            self._send_json({"error": str(e)}, 401)
            return
        try:
            info = downloader.fetch_book_info(book_id, cookies, headers)
            self._send_json(info)
        except Exception as e:
            self._send_json({"error": f"Failed to fetch book: {e}"}, 400)

    def _handle_progress(self):
        with self._progress_lock:
            self._send_json(dict(self.download_progress))

    def _handle_cancel(self):
        with self._progress_lock:
            status = self.download_progress.get("status")
            if status and status not in ("completed", "error", "cancelled"):
                DownloaderHandler._cancel_requested.set()
                self._send_json({"success": True})
            else:
                self._send_json({"success": False, "message": "No active download"})

    def _handle_reveal(self, data: dict):
        path_str = data.get("path", "")
        if not path_str:
            self._send_json({"error": "path required"}, 400)
            return
        path = Path(path_str).resolve()
        # Confine reveals to the downloads directory to avoid disclosing
        # arbitrary filesystem locations.
        try:
            path.relative_to(DEFAULT_OUTPUT_DIR)
        except ValueError:
            self._send_json({"error": "Access denied: path must be within the books directory"}, 403)
            return
        if not path.exists():
            self._send_json({"error": "Path does not exist"}, 404)
            return
        try:
            self._reveal_in_file_manager(path)
            self._send_json({"success": True})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    @staticmethod
    def _reveal_in_file_manager(path: Path):
        """Open the OS file manager at ``path`` (best effort, cross-platform)."""
        target = str(path if path.is_dir() else path.parent)
        system = platform.system()
        # "--" stops a leading-hyphen path from being read as a CLI option.
        if system == "Darwin":
            subprocess.Popen(["open", "--", target])
        elif system == "Windows":
            subprocess.Popen(["explorer", target])
        else:
            subprocess.Popen(["xdg-open", "--", target])

    def _handle_download(self, data: dict):
        raw_id = data.get("book_id", "")
        book_id = downloader.extract_book_id(raw_id)
        # "pdf" (scanned page images) or "epub" (reflowable text segments).
        fmt = (data.get("format") or "pdf").lower()
        # Whether to build the output file (PDF/EPUB) after downloading raw assets.
        # `build_pdf` is accepted for backward compatibility with older clients.
        build = bool(data.get("build", data.get("build_pdf")))
        # Downloads are always confined to the books directory; the book id is
        # sanitized by extract_book_id so it cannot escape it.
        output_dir = str(DEFAULT_OUTPUT_DIR)

        if not book_id:
            self._send_json({"error": "book_id required"}, 400)
            return
        if fmt not in ("pdf", "epub"):
            self._send_json({"error": f"Unknown format '{fmt}'."}, 400)
            return
        if fmt == "pdf" and build and not PDF_AVAILABLE:
            self._send_json(
                {"error": "PDF building needs the 'img2pdf' and 'pikepdf' packages."}, 400
            )
            return
        if fmt == "epub" and not EPUB_AVAILABLE:
            self._send_json(
                {"error": "EPUB building needs the 'ebooklib' and 'beautifulsoup4' packages."},
                400,
            )
            return

        try:
            cookies, headers = self._load_session()
        except ValueError as e:
            self._send_json({"error": str(e)}, 401)
            return

        with self._progress_lock:
            status = self.download_progress.get("status")
            if status and status not in ("completed", "error", "cancelled"):
                self._send_json({"error": "Download already in progress"}, 409)
                return
            DownloaderHandler.download_progress = {"status": "starting", "book_id": book_id}

        thread = threading.Thread(
            target=self._download_async,
            args=(book_id, cookies, headers, output_dir, fmt, build),
            daemon=True,
        )
        thread.start()
        self._send_json({"status": "started", "book_id": book_id})

    def _download_async(self, book_id, cookies, headers, output_dir, fmt, build):
        DownloaderHandler._cancel_requested.clear()
        unit = "segments" if fmt == "epub" else "pages"

        def on_progress(progress: DownloadProgress):
            self._set_progress(
                {
                    "status": progress.status,
                    "book_id": progress.book_id,
                    "title": progress.title,
                    "percentage": progress.percentage,
                    "message": progress.message,
                    "current_page": progress.current_page,
                    "total_pages": progress.total_pages,
                    "failed_pages": progress.failed_pages,
                    "eta_seconds": progress.eta_seconds,
                    "format": fmt,
                    "unit": unit,
                }
            )

        try:
            if fmt == "epub":
                result = downloader.download_segments(
                    book_id=book_id,
                    cookies=cookies,
                    headers=headers,
                    output_dir=output_dir,
                    progress_callback=on_progress,
                    cancel_check=DownloaderHandler._cancel_requested.is_set,
                )
            else:
                result = downloader.download_book(
                    book_id=book_id,
                    cookies=cookies,
                    headers=headers,
                    output_dir=output_dir,
                    progress_callback=on_progress,
                    cancel_check=DownloaderHandler._cancel_requested.is_set,
                )

            pdf_path = epub_path = None
            # EPUB reconstruction is the whole point of the epub format, so it
            # always runs; PDF building is optional.
            if fmt == "epub":
                self._set_progress(
                    {
                        "status": "building_epub",
                        "book_id": book_id,
                        "title": result.title,
                        "percentage": 100,
                        "message": "Reconstructing EPUB (this can take a while)…",
                        "format": fmt,
                        "unit": unit,
                    }
                )
                epub_path = downloader.build_epub(result.book_dir)
            elif build:
                self._set_progress(
                    {
                        "status": "building_pdf",
                        "book_id": book_id,
                        "title": result.title,
                        "percentage": 100,
                        "message": "Building PDF (this can take a while)…",
                        "format": fmt,
                        "unit": unit,
                    }
                )
                pdf_path = downloader.build_pdf(result.book_dir)

            self._set_progress(
                {
                    "status": "completed",
                    "book_id": result.book_id,
                    "title": result.title,
                    "percentage": 100,
                    "book_dir": result.book_dir,
                    "total_pages": result.total_pages,
                    "downloaded_pages": result.downloaded_pages,
                    "failed_pages": result.failed_pages,
                    "format": fmt,
                    "unit": unit,
                    "pdf": pdf_path,
                    "epub": epub_path,
                }
            )
        except downloader.CancelledError:
            self._set_progress({"status": "cancelled", "message": "Download cancelled"})
        except Exception as e:
            traceback.print_exc()
            self._set_progress({"status": "error", "error": str(e)})

    # -- utilities ----------------------------------------------------------
    def _send_json(self, data: dict, status: int = 200):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0] if args else ''}")


def create_server(host: str = "localhost", port: int = 8000) -> HTTPServer:
    return HTTPServer((host, port), DownloaderHandler)


def run_server(host: str = "localhost", port: int = 8000):
    server = create_server(host, port)
    print(f"Google Play Books downloader running at http://{host}:{port}")
    if not PDF_AVAILABLE:
        print("Note: install 'img2pdf' and 'pikepdf' to enable PDF building.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
