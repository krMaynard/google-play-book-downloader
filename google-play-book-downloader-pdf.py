#!/usr/bin/env python3
"""Command-line Google Play Books page downloader.

This is a thin wrapper around :mod:`play_books.downloader`, which holds the
actual download logic (shared with the web GUI, see ``gui.py``).
"""

import logging

from play_books import downloader


def main():
    book_id = input("Type your book ID and press enter: ").strip()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

    try:
        with open("curl.txt", "r") as f:
            curl_command = f.read().strip()
    except FileNotFoundError:
        print("""\nYou will need to provide your cookies in order to download books. Here is how:
1) Go to https://play.google.com/books and log in.
2) Open dev console, network tab.
3) Click on the book you want to read (the link should have the format https://play.google.com/books/reader?id=xxxxxxxxxx)
4) In the network tab of the dev console type "segment" (without the quotes) in the filter box.
5) Right-click on the first request (it should appear as segment?authuser=0&xxxxxxxxx) in the dev console and then click on "Copy as cURL", or "Copy as cURL (bash)", or "Copy as cURL (POSIX)" whichever appears (the name of this option depends on your browser and OS).
6) Create the file curl.txt and paste it inside\n""")
        raise FileNotFoundError("curl.txt file not found. Please create it and put the curl command from the browser.")
    except IOError as e:
        raise IOError(f"Error reading curl.txt: {e}")

    try:
        url, cookies, headers = downloader.parse_curl_command(curl_command)
    except ValueError as e:
        raise ValueError(f"Failed to parse curl command: {e}")

    if url.netloc != "play.google.com":
        raise ValueError(
            f"Invalid curl command in curl.txt. The domain name should be 'play.google.com' "
            f"but in the command it is: {url.netloc}"
        )

    logging.info(f"Script started for book id: {book_id}")

    def log_progress(progress: downloader.DownloadProgress):
        if progress.status == "downloading" and progress.total_pages:
            logging.info(
                f"[{progress.current_page}/{progress.total_pages}] {progress.message}"
            )

    result = downloader.download_book(
        book_id=book_id,
        cookies=cookies,
        headers=headers,
        output_dir="books",
        progress_callback=log_progress,
    )

    logging.info(
        f'Finished. Downloaded {result.downloaded_pages}/{result.total_pages} pages '
        f'to "{result.book_dir}".'
    )


if __name__ == "__main__":
    main()
