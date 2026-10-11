"""Open the dashboard once the server has had time to bind its port.

Called in the background by start.bat so the browser does not land on a
connection error while uvicorn is still starting.
"""

from __future__ import annotations

import socket
import sys
import time
import webbrowser
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 20.0


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.25)
    return False


def main(argv: list[str]) -> int:
    url = argv[0] if argv else DEFAULT_URL
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80

    if wait_for_port(host, port, TIMEOUT_SECONDS):
        webbrowser.open(url)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
