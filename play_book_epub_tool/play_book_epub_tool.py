"""Reconstruct a valid, self-contained EPUB from downloaded Google Play Books segments.

The EPUB downloader (`google-play-book-downloader-epub.py`) saves each reflowable
"segment" of a book to `books/<BOOK_ID>/<label>.xhtml` (+ `<label>.css`), together
with a `manifest.json`. Those XHTML fragments still reference images and fonts by
their absolute `https://play.google.com/...` URLs, so on their own they don't make a
valid offline EPUB.

This tool packages those segments into a spec-compliant EPUB:

  * Resources (images/fonts referenced from the HTML and the CSS) are resolved once,
    embedded as in-package items, and every reference is rewritten to a relative path
    — so nothing points at the network anymore.
  * ebooklib rebuilds each fragment into a well-formed XHTML content document.
  * The table of contents is reconstructed *hierarchically* from the manifest's
    `toc_entry` list (falling back to a flat per-segment TOC when the entries can't be
    mapped to downloaded segments).
  * The cover, identifier (a stable `urn:uuid`), and Dublin Core metadata are set from
    the manifest.

Usage:
    poetry run play-book-epub-build books/BwCMEAAAQBAJ
"""

import base64
import hashlib
import html
import json
import logging
import mimetypes
import pathlib
import re
import uuid
from urllib.parse import unquote_to_bytes, urlparse

import click
from bs4 import BeautifulSoup
from ebooklib import epub

logging.basicConfig(format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RESOURCE_DIR = "resources"

# Elements whose resource is carried by these attributes.
_RESOURCE_ATTRS = {
    "img": "src",
    "image": "xlink:href",
    "source": "src",
    "audio": "src",
    "video": "src",
    "track": "src",
    "embed": "src",
    "object": "data",
    "input": "src",
}

# url(...) inside CSS text or a `style="..."` attribute.
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)")

# A stable namespace so identical source URLs always yield the same urn:uuid
# (Google volume ids aren't guaranteed to be valid UUIDs on their own).
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "play.google.com/books")


class Resource:
    """A resolved, deduplicated in-package resource file."""

    __slots__ = ("file_name", "media_type", "content")

    def __init__(self, file_name, media_type, content):
        self.file_name = file_name
        self.media_type = media_type
        self.content = content


def _guess_extension(media_type, url):
    if media_type:
        ext = mimetypes.guess_extension(media_type.split(";")[0].strip())
        if ext:
            return ".jpg" if ext == ".jpe" else ext
    ext = pathlib.PurePosixPath(urlparse(url).path).suffix
    return ext or ".bin"


def default_resolver(base_path):
    """Build the default resource resolver.

    It natively decodes `data:` URIs and reads already-downloaded files from the book
    directory. Remote `http(s)` resources are fetched with the cookies/headers from a
    `curl.txt` placed in the book directory (or the current directory) — the same file
    the downloaders use. Without `curl.txt`, remote resources are skipped (and logged)
    rather than crashing the build, so offline reconstruction still works.

    Returns a callable `resolver(url) -> (bytes, media_type) | None`.
    """

    session_state = {}

    def _load_session():
        if "session" in session_state:
            return session_state["session"], session_state["cookies"], session_state["headers"]
        import requests

        cookies, headers = {}, {}
        for candidate in (base_path / "curl.txt", pathlib.Path("curl.txt")):
            if candidate.exists():
                cookies, headers = _parse_curl(candidate.read_text(encoding="utf-8"))
                break
        else:
            logger.warning(
                "No curl.txt found; remote resources will be skipped. Provide one "
                "(see the PDF downloader instructions) to embed remote images/fonts."
            )
        session = requests.Session()
        session_state.update(session=session, cookies=cookies, headers=headers)
        return session, cookies, headers

    def resolve(url):
        if url.startswith("data:"):
            try:
                header, _, payload = url[len("data:"):].partition(",")
                media_type = header.split(";")[0] or "application/octet-stream"
                # Non-base64 data: payloads are percent-encoded (e.g. inline SVG), so decode
                # the escapes rather than taking the raw text bytes.
                data = base64.b64decode(payload) if ";base64" in header else unquote_to_bytes(payload)
                return data, media_type
            except Exception as exc:  # noqa: BLE001 - a bad data URI shouldn't abort the build
                logger.warning("Skipping malformed data: URI (%s)", exc)
                return None

        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            session, cookies, headers = _load_session()
            if not cookies:
                return None
            try:
                response = session.get(url, cookies=cookies, headers=headers, timeout=30)
                response.raise_for_status()
                return response.content, response.headers.get("content-type", "application/octet-stream")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to download resource %s (%s)", url, exc)
                return None

        # Otherwise treat it as a file already sitting in the book directory.
        local = base_path / parsed.path.lstrip("/")
        if local.exists():
            media_type, _ = mimetypes.guess_type(str(local))
            return local.read_bytes(), media_type or "application/octet-stream"
        return None

    return resolve


def _parse_curl(curl_command):
    """Minimal `curl.txt` parser: extract cookies and headers. Mirrors the downloaders."""
    import shlex

    cookies, headers = {}, {}
    try:
        tokens = shlex.split(curl_command.strip())
    except ValueError:
        return cookies, headers

    it = iter(tokens)
    for token in it:
        if token in ("-H", "--header"):
            header = next(it, "")
            key, _, value = header.partition(":")
            headers[key.strip()] = value.strip()
        elif token in ("-b", "--cookie"):
            cookies_raw = next(it, "")
            for part in cookies_raw.split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    cookies[key.strip()] = value.strip()
    if "Cookie" in headers:  # Firefox puts cookies in a header
        for part in headers["Cookie"].split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                cookies.setdefault(key.strip(), value.strip())
    return cookies, headers


class ResourcePackager:
    """Resolves referenced resources once and rewrites HTML/CSS to relative paths."""

    def __init__(self, resolver):
        self._resolver = resolver
        self._by_url = {}  # url -> Resource (or None if unresolvable)

    @property
    def resources(self):
        # Deduplicate by file_name: it's a content hash, so identical bytes reached via
        # different URLs collapse to a single packaged file.
        unique = {}
        for resource in self._by_url.values():
            if resource is not None:
                unique.setdefault(resource.file_name, resource)
        return list(unique.values())

    def _package(self, url):
        if url in self._by_url:
            return self._by_url[url]
        resolved = self._resolver(url)
        if not resolved:
            self._by_url[url] = None
            return None
        data, media_type = resolved
        digest = hashlib.sha1(data).hexdigest()[:16]
        file_name = f"{RESOURCE_DIR}/{digest}{_guess_extension(media_type, url)}"
        self._by_url[url] = Resource(file_name, media_type, data)
        return self._by_url[url]

    def rewrite_html(self, content):
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup.find_all(True):
            attr = _RESOURCE_ATTRS.get(tag.name)
            if attr and tag.has_attr(attr):
                new = self._rewrite_value(tag[attr])
                if new is not None:
                    tag[attr] = new
                    tag.attrs.pop("width", None)
                    tag.attrs.pop("height", None)
            if tag.has_attr("style"):
                tag["style"] = self._rewrite_css_urls(tag["style"])
            if tag.name == "style":
                # get_text() covers <style> tags with multiple children (comments/text
                # nodes) where tag.string would be None; the assignment collapses them
                # into the single rewritten stylesheet.
                tag.string = self._rewrite_css_urls(tag.get_text())
        return str(soup)

    def rewrite_css(self, css):
        return self._rewrite_css_urls(css)

    def _rewrite_css_urls(self, text):
        def repl(match):
            new = self._rewrite_value(match.group(2))
            return f"url({new})" if new is not None else match.group(0)

        return _CSS_URL_RE.sub(repl, text)

    def _rewrite_value(self, url):
        if not url or url.startswith("#"):
            return None
        resource = self._package(url)
        return resource.file_name if resource is not None else None


def _load_manifest(base_path):
    manifest_path = base_path / "manifest.json"
    try:
        with open(manifest_path) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("Couldn't find %s! Aborting...", manifest_path)
        raise


def _set_metadata(book, manifest):
    metadata = manifest["metadata"]
    volume_id = metadata["volume_id"]
    source_url = f"https://books.google.com/books?id={volume_id}"

    book.set_identifier(f"urn:uuid:{uuid.uuid5(_UUID_NAMESPACE, source_url)}")
    book.set_title(html.unescape(metadata["title"]))
    book.set_language(manifest.get("language") or "en")

    for i, author in enumerate(metadata.get("authors", "").split(",")):
        author = html.unescape(author).strip()
        if author:
            book.add_author(author, uid=f"creator{i}")  # unique id per author

    if metadata.get("publisher"):
        book.add_metadata("DC", "publisher", html.unescape(metadata["publisher"]))
    if metadata.get("pub_date"):
        book.add_metadata("DC", "date", metadata["pub_date"].replace(".", "-"))
    book.add_metadata("DC", "source", source_url)
    book.add_metadata("DC", "source", f"https://play.google.com/store/books/details?id={volume_id}")

    if manifest.get("is_right_to_left"):
        book.set_direction("rtl")


def _add_cover(book, base_path, manifest, packager):
    """Add a cover, preferring a local file, then a manifest cover URL. Best-effort."""
    # create_page=False -> mark the image with the EPUB3 `cover-image` property instead
    # of generating a separate cover.xhtml page (which epubcheck flags as unreachable
    # non-linear content unless it's linked from the nav).
    for pattern in ("cover.*", "PP1.*"):
        for candidate in sorted(base_path.glob(pattern)):
            if candidate.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                book.set_cover("cover" + candidate.suffix.lower(), candidate.read_bytes(),
                               create_page=False)
                return True

    metadata = manifest.get("metadata", {})
    cover_url = (
        manifest.get("cover_url")
        or metadata.get("cover_url")
        or metadata.get("cover")
        or manifest.get("cover")
    )
    if cover_url:
        resource = packager._package(cover_url)  # reuse resolver/dedup
        if resource is not None:
            book.set_cover("cover" + _guess_extension(resource.media_type, cover_url),
                           resource.content, create_page=False)
            return True

    logger.warning("No cover found (no local cover/PP1 image and no cover URL in the manifest).")
    return False


def _build_toc(manifest, chapters):
    """Reconstruct a hierarchical TOC from `toc_entry`, falling back to flat chapters.

    Each `toc_entry` (`{label, depth, ...}`) is linked to a downloaded chapter by, in
    priority order: an explicit segment reference (`segment` / `segment_label`), then a
    label match against the segment title, then against the segment label. Entries that
    can't be resolved to a downloaded segment are dropped. If nothing resolves, we return
    a flat list of every chapter (the previous behaviour), which is always valid.
    """
    by_label = {label: chapter for label, chapter, _title in chapters}
    by_title = {}
    for label, chapter, title in chapters:
        by_title.setdefault(html.unescape(title or "").strip(), chapter)

    toc_entries = manifest.get("toc_entry") or []
    resolved = []
    for entry in toc_entries:
        chapter = None
        for key in ("segment", "segment_label"):
            if entry.get(key) in by_label:
                chapter = by_label[entry[key]]
                break
        label = html.unescape(entry.get("label", "")).strip()
        if chapter is None and label in by_title:
            chapter = by_title[label]
        if chapter is None and label in by_label:
            chapter = by_label[label]
        if chapter is None:
            continue
        resolved.append({"title": label or chapter.title, "href": chapter.file_name,
                         "depth": int(entry.get("depth") or 0)})  # tolerate missing/null depth

    if not resolved:
        if toc_entries:
            logger.warning("Couldn't map any toc_entry to a downloaded segment; using a flat TOC.")
        return [chapter for _label, chapter, _title in chapters]

    # Build a nested tree from the flat depth-ordered list.
    root = []
    stack = [(float("-inf"), root)]  # sentinel that no (even negative) depth can pop
    for item in resolved:
        node = {"title": item["title"], "href": item["href"], "children": []}
        while stack and stack[-1][0] >= item["depth"]:
            stack.pop()
        stack[-1][1].append(node)
        stack.append((item["depth"], node["children"]))

    counter = {"n": 0}

    def convert(node):
        counter["n"] += 1
        uid = f"toc_{counter['n']}"
        if node["children"]:
            return (epub.Section(node["title"], href=node["href"]),
                    [convert(child) for child in node["children"]])
        return epub.Link(node["href"], node["title"], uid=uid)

    return [convert(node) for node in root]


def build_epub(base_path, resolver=None):
    """Build `book.epub` inside `base_path` from the downloaded segments. Returns its path."""
    base_path = pathlib.Path(base_path)
    manifest = _load_manifest(base_path)
    resolver = resolver or default_resolver(base_path)
    packager = ResourcePackager(resolver)

    book = epub.EpubBook()
    _set_metadata(book, manifest)
    _add_cover(book, base_path, manifest, packager)

    chapters = []
    for segment in manifest["segment"]:
        label = segment["label"]
        title = segment.get("title", label)
        xhtml_path = base_path / f"{label}.xhtml"
        css_path = base_path / f"{label}.css"
        try:
            xhtml = xhtml_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.error("Couldn't find %s! Aborting...", xhtml_path)
            raise

        xhtml = packager.rewrite_html(xhtml)
        chapter = epub.EpubHtml(title=html.unescape(title), file_name=f"{label}.xhtml",
                                lang=book.language)
        chapter.add_meta(charset="utf-8")  # ebooklib emits <meta charset="utf-8"/>
        chapter.content = xhtml

        if css_path.exists():
            css = packager.rewrite_css(css_path.read_text(encoding="utf-8"))
            css_item = epub.EpubItem(file_name=f"{label}.css", media_type="text/css", content=css)
            book.add_item(css_item)
            chapter.add_item(css_item)

        book.add_item(chapter)
        chapters.append((label, chapter, title))

    # Package all resolved resources once.
    for resource in packager.resources:
        book.add_item(epub.EpubItem(file_name=resource.file_name,
                                    media_type=resource.media_type, content=resource.content))

    book.toc = _build_toc(manifest, chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    chapter_items = [chapter for _label, chapter, _title in chapters]
    book.spine = ["nav", *chapter_items]

    output = base_path / "book.epub"
    epub.write_epub(str(output), book, {})
    return output


@click.command()
@click.argument(
    "book-base-path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, readable=True,
                    path_type=pathlib.Path),
)
def epub_generate(book_base_path):
    """Rebuild an EPUB from the Google Play Books segments in BOOK-BASE-PATH.

    Example: play-book-epub-build "books/BwCMEAAAQBAJ"

    The book's segments must have already been downloaded with
    google-book-downloader-epub.py before running this command.
    """
    output = build_epub(book_base_path)
    click.echo(f'Done! EPUB saved to "{output}"')


if __name__ == "__main__":
    epub_generate()
