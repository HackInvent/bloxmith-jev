"""Private bounded HTTPS worker. No framework dependency, credentials only on stdin."""

import json
import os
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_BYTES = 512 * 1024


class NoRedirect(HTTPRedirectHandler):
    """Never forward Authorization to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(packet):
    """Retry only explicit throttling/overload, never ambiguous transport failures."""
    opener = build_opener(NoRedirect())
    body = json.dumps(packet["body"], ensure_ascii=False, allow_nan=False).encode("utf-8")
    for attempt in range(packet["retries"] + 1):
        try:
            query = Request(ENDPOINT, data=body, method="POST", headers={"Authorization": "Bearer " + packet["key"],
                            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "BloxSmith-Jev/0.1.0"})
            with opener.open(query, timeout=packet["timeout"]) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    return {"error": "large"}
                return {"body": raw.decode("utf-8")}
        except HTTPError as error:
            status = error.code
            delay = error.headers.get("Retry-After", "")
            error.close()
            if status in (429, 529) and attempt < packet["retries"]:
                try:
                    delay = float(delay) if delay else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                # The parent enforces the total wall-clock budget even during backoff.
                if not 0 <= delay <= packet["timeout"]:
                    return {"error": "busy"}
                time.sleep(delay)
                continue
            return {"error": "auth" if status in (401, 403) else "request" if status in (400, 422)
                    else "busy" if status in (429, 529) else "redirect" if 300 <= status < 400 else "http"}
        except Exception:
            return {"error": "network"}
    return {"error": "busy"}


def main():
    """Watch ownership and keep all raw failures away from graph results and stderr."""
    if len(sys.argv) > 1:
        descriptor = int(sys.argv[1])

        def watch_owner():
            try:
                while os.read(descriptor, 1):
                    pass
            finally:
                os._exit(0)

        threading.Thread(target=watch_owner, daemon=True).start()
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            result = {"error": "large"}
        else:
            result = request(json.loads(raw))
    except Exception:
        result = {"error": "network"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
