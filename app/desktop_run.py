"""Server entry point for the Windows app (desktop/src/server.ts): uvicorn plus a link to the app window.

The window keeps this process's stdin open and never writes to it except "quit" (stop cleanly). When stdin
closes, the window has gone, even if it crashed or was killed. The server stops then too, so a leftover
server never keeps running schedules next to a new one.

    python -m app.desktop_run --port 8000
"""
import argparse
import os
import sys
import threading

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    port = ap.parse_args().port
    server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=port))

    def watch_window() -> None:
        for line in sys.stdin:
            if line.strip() == "quit":
                break
        server.should_exit = True
        threading.Timer(15, lambda: os._exit(0)).start()  # a request or job that won't finish can't block exit

    threading.Thread(target=watch_window, daemon=True).start()
    server.run()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # don't wait on background job threads; jobs.recover() picks them up next start


if __name__ == "__main__":
    main()
