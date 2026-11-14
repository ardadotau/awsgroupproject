#!/usr/bin/env python3
"""Log low-rate public /health requests for a short availability test.

Standard library only. TLS validation is on unless --insecure is explicitly used
for a testing-only self-signed certificate. No login or secrets are required.
"""

import argparse
import collections
import csv
import datetime as dt
import json
import pathlib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Application base URL, or its /health URL")
    parser.add_argument("--duration", type=float, default=180, help="Seconds, 1 to 600 (default: 180)")
    parser.add_argument("--interval", type=float, default=1, help="Seconds between request starts, at least 0.25")
    parser.add_argument("--timeout", type=float, default=5, help="Request timeout, 0.5 to 15 seconds")
    parser.add_argument("--csv", type=pathlib.Path, help="New CSV path; an existing file will not be overwritten")
    parser.add_argument("--insecure", action="store_true", help="TESTING ONLY: bypass validation of a lab self-signed certificate")
    args = parser.parse_args(argv)
    if not 1 <= args.duration <= 600:
        parser.error("--duration must be between 1 and 600 seconds")
    if args.interval < 0.25:
        parser.error("--interval must be at least 0.25 seconds")
    if not 0.5 <= args.timeout <= 15:
        parser.error("--timeout must be between 0.5 and 15 seconds")
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        parser.error("Supply an http:// or https:// application URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("The probe URL must not contain credentials, query parameters or a fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/health"):
        path += "/health"
    args.url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    if args.csv is None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.csv = pathlib.Path(f"gateway-probe-{stamp}.csv")
    return args


def sample(url, context, timeout):
    started = time.monotonic()
    status, server, error, body = 0, "", "", b""
    request = urllib.request.Request(url, headers={"User-Agent": "INFS3701-Gateway-Evidence-Probe/1.0"})
    try:
        with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
            status, body = response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(65536)
        error = f"HTTP {status}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Store only the reason, not a URL which might accidentally contain data.
        reason = getattr(exc, "reason", exc)
        error = str(reason).replace("\r", " ").replace("\n", " ")[:200]
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                server = str(data.get("server", data.get("server_name", "")))[:100]
        except (ValueError, UnicodeError):
            if 200 <= status < 300:
                error = "Health response was not JSON"
    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    return status, elapsed_ms, server, error


def main(argv=None):
    args = parse_args(argv)
    if args.insecure:
        print("TESTING ONLY: TLS certificate validation is disabled for this probe.", file=sys.stderr)
        context = ssl._create_unverified_context()
    else:
        context = ssl.create_default_context()
    counts, servers = collections.Counter(), collections.Counter()
    started = time.monotonic()
    deadline = started + args.duration
    next_start = started
    interrupted = False
    try:
        with args.csv.open("x", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["timestamp_utc", "status", "latency_ms", "server", "error"])
            try:
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if now < next_start:
                        time.sleep(min(next_start - now, max(0, deadline - now)))
                    if time.monotonic() >= deadline:
                        break
                    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    status, latency, server, error = sample(args.url, context, min(args.timeout, max(0.1, deadline - time.monotonic())))
                    writer.writerow([stamp, status, latency, server, error])
                    stream.flush()
                    counts["requests"] += 1
                    if 200 <= status < 300 and not error:
                        counts["success"] += 1
                    else:
                        counts["failed"] += 1
                    if server:
                        servers[server] += 1
                    next_start += args.interval
                    if next_start < time.monotonic():
                        # Do not issue a burst to catch up after a slow request.
                        next_start = time.monotonic() + args.interval
            except KeyboardInterrupt:
                interrupted = True
    except OSError as exc:
        print(f"Cannot create CSV: {exc}", file=sys.stderr)
        return 2
    result = {
        "csv": str(args.csv.resolve()),
        "requests": counts["requests"],
        "successful_requests": counts["success"],
        "failed_requests": counts["failed"],
        "responses_by_server": dict(servers),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "tls_validation": not args.insecure,
        "interrupted": interrupted,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
