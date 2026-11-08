from __future__ import annotations

import argparse
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

from flexistudy.api import FlexiHandler
from flexistudy.store import Store


def main():
    parser = argparse.ArgumentParser(description="FlexiStudy Local")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data", help="Path to state.json")
    args = parser.parse_args()
    FlexiHandler.store = Store(args.data)
    FlexiHandler.static_dir = Path(__file__).parent / "static"
    server = ThreadingHTTPServer((args.host, args.port), FlexiHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"FlexiStudy Local running at {url}")
    print(f"Data: {FlexiHandler.store.path}")
    if not args.no_browser:
        threading.Timer(.6, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopping FlexiStudy Local")
    finally: server.server_close()


if __name__ == "__main__":
    main()
