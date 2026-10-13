import http.client
import json
import sys
import time
import urllib.parse
from pathlib import Path

url = sys.argv[1]
parsed_url = urllib.parse.urlsplit(url)
if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
    raise SystemExit("recovery probe URL must use http or https and include a host")
try:
    port = parsed_url.port
except ValueError as exc:
    raise SystemExit("recovery probe URL contains an invalid port") from exc
connection_class = (
    http.client.HTTPSConnection
    if parsed_url.scheme == "https"
    else http.client.HTTPConnection
)
request_target = parsed_url.path or "/"
if parsed_url.query:
    request_target = f"{request_target}?{parsed_url.query}"
limit = float(sys.argv[2])
output = Path(sys.argv[3])
started = time.perf_counter()
attempts = 0
recovered = False
last_error = None

while time.perf_counter() - started < limit:
    attempts += 1
    connection = connection_class(parsed_url.hostname, port=port, timeout=1)
    try:
        connection.request(
            "GET", request_target, headers={"X-Forwarded-Proto": "https"}
        )
        response = connection.getresponse()
        recovered = response.status == 200
        response.read()
        if recovered:
            break
    except Exception as exc:
        last_error = type(exc).__name__
    finally:
        connection.close()
    time.sleep(0.5)

result = {
    "recovered": recovered,
    "recovery_seconds": time.perf_counter() - started,
    "attempts": attempts,
    "last_error": last_error,
}
output.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
