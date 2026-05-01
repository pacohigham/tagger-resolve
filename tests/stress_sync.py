#!/usr/bin/env python3
# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Sync /process_request stress test (Phase 0.3 of production checklist).

Fires N concurrent requests at the Render server to confirm:
  1. 429 / 503 are returned (not collapsed to 502)
  2. Retry-After header is set on those responses
  3. Successful responses still return 200 with metadata

Usage:
  python tests/stress_sync.py --concurrency 30 --total 30

By default uses the demo license (DEMO-DEMO-DEMO-DEMO). Override with
--license-key / --hardware-id to test against a paid license.

Note: this hits the live Render server and consumes Anthropic credits
on successful calls (1 credit per request from the demo or paid pool).
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import sys
import time
from collections import Counter

import requests
from PIL import Image


def _make_test_grid() -> str:
    """Generate a small valid JPEG and return its base64 encoding.

    Using a tiny grayscale image so we don't waste bandwidth or Anthropic
    tokens. The model will still respond (probably with low-confidence
    metadata) which is fine -- we're testing the network/error path,
    not the AI quality.
    """
    img = Image.new("RGB", (640, 360), color=(40, 40, 40))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def fire_one(idx: int, url: str, payload: dict, timeout: int = 90) -> dict:
    """Send one request and return a structured result row."""
    t0 = time.time()
    try:
        r = requests.post(f"{url}/process_request", json=payload, timeout=timeout)
        elapsed = time.time() - t0
        retry_after = r.headers.get("Retry-After")
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:200]}
        return {
            "idx": idx,
            "status": r.status_code,
            "elapsed_s": round(elapsed, 2),
            "retry_after": retry_after,
            "detail": (body.get("detail") if isinstance(body, dict) else None) or "",
            "ok": r.status_code == 200,
        }
    except requests.RequestException as e:
        return {
            "idx": idx,
            "status": "exception",
            "elapsed_s": round(time.time() - t0, 2),
            "retry_after": None,
            "detail": str(e)[:200],
            "ok": False,
        }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://tagger-1t2g.onrender.com")
    p.add_argument("--concurrency", type=int, default=30)
    p.add_argument("--total", type=int, default=30)
    p.add_argument("--license-key", default="DEMO-DEMO-DEMO-DEMO")
    p.add_argument("--hardware-id", default="STRESS_TEST_RIG")
    args = p.parse_args()

    print(f"Stress test: {args.total} requests, {args.concurrency} concurrent -> {args.url}")
    print(f"License: {args.license_key}  Hardware: {args.hardware_id}\n")

    image_b64 = _make_test_grid()
    payload = {
        "license_key":        args.license_key,
        "hardware_id":        args.hardware_id,
        "image_b64":          image_b64,
        "description_length": "brief",
        "schema_version":     "v2",
    }

    t0 = time.time()
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(fire_one, i, args.url, payload) for i in range(args.total)]
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            results.append(r)
            tag = "OK" if r["ok"] else f"FAIL {r['status']}"
            ra  = f" RA={r['retry_after']}" if r["retry_after"] else ""
            print(f"  [{r['idx']:3d}] {tag:10s} {r['elapsed_s']}s{ra}  {r['detail'][:80]}")

    total_elapsed = time.time() - t0
    print(f"\nWall time: {total_elapsed:.1f}s\n")

    # Summary
    by_status = Counter(r["status"] for r in results)
    print("=== Status breakdown ===")
    for status, n in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {status}: {n}")

    print()

    # Verification checklist
    rate_limited = [r for r in results if r["status"] in (429, 503)]
    bad_gateway  = [r for r in results if r["status"] == 502]
    ok           = [r for r in results if r["status"] == 200]

    print("=== Phase 0.3 verification ===")
    print(f"  200 OK:           {len(ok)}")
    print(f"  429/503 throttle: {len(rate_limited)}")
    print(f"  502 generic:      {len(bad_gateway)}")
    print()

    if rate_limited:
        with_retry = [r for r in rate_limited if r["retry_after"]]
        without_retry = len(rate_limited) - len(with_retry)
        print(f"  Throttled responses with Retry-After header: {len(with_retry)}/{len(rate_limited)}")
        if without_retry:
            print(f"  WARN: {without_retry} throttle response(s) missing Retry-After header")

    if bad_gateway:
        print(f"  WARN: {len(bad_gateway)} 502 response(s) -- investigate")
        for r in bad_gateway[:3]:
            print(f"    detail: {r['detail'][:120]}")

    # Return non-zero if everything failed (pure outage)
    if len(ok) == 0 and len(rate_limited) == 0:
        print("\nALL REQUESTS FAILED -- network or auth issue, not a stress test result.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
