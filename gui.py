#!/usr/bin/env python3
"""Launch the Google Play Books downloader web GUI.

Usage:
    python gui.py                 # http://localhost:8000
    python gui.py --port 9000     # custom port
    python gui.py --no-browser    # don't open a browser automatically
"""

import argparse
import threading
import webbrowser

from web.server import run_server


def main():
    parser = argparse.ArgumentParser(description="Google Play Books downloader GUI")
    parser.add_argument("--host", default="localhost", help="Host to bind (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser window automatically"
    )
    args = parser.parse_args()

    if not args.no_browser:
        url = f"http://{args.host}:{args.port}"
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
