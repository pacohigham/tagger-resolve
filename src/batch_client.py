# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Tagger Batch API client.

Wraps /batch/submit, /batch/{id}/status, /batch/{id}/results on the
Render server. The Batch API path is the production path for any
multi-file processing -- it uses Anthropic's separate batch quota,
costs 50% less per token, and is immune to per-minute rate limits
that synchronous calls hit under load.

Trade-off: results are not real-time. Anthropic's SLA is 24 hours
but typical turnaround for our request size is minutes. The poller
honours Retry-After headers and backs off with jitter on 429/5xx.
"""

from __future__ import annotations

import base64
import logging
import random
import time
from dataclasses import dataclass
from typing import Iterable

import requests

logger = logging.getLogger(__name__)


class BatchSubmitError(Exception):
    """Raised when the server cannot submit a batch upstream."""


class BatchNotReady(Exception):
    """Raised when /results is called before the batch has ended."""


class CreditsExhaustedError(Exception):
    """Insufficient credits for the requested batch size."""


@dataclass
class BatchSubmitResult:
    batch_id: str
    items_submitted: int
    credits_pre_deducted: int
    credits_remaining: int
    processing_status: str


@dataclass
class BatchStatus:
    batch_id: str
    processing_status: str        # in_progress | ended | canceled | expired
    items_count: int
    counts: dict                   # processing/succeeded/errored/canceled/expired
    results_retrieved: bool


@dataclass
class BatchResultRow:
    custom_id: str
    file_name: str | None
    status: str                    # succeeded | errored | canceled | expired
    metadata: dict | None
    error: str | None


@dataclass
class BatchResults:
    batch_id: str
    items_count: int
    succeeded: int
    failed: int
    credits_refunded: int
    credits_remaining: int
    results: list[BatchResultRow]


class BatchClient:
    def __init__(
        self,
        proxy_url: str,
        license_key: str,
        hardware_id: str,
        description_length: str = "standard",
        timeout: int = 60,
    ):
        self.proxy_url = proxy_url.rstrip("/")
        self.license_key = license_key
        self.hardware_id = hardware_id
        self.description_length = description_length
        self.timeout = timeout

    @staticmethod
    def encode_image(image_path: str) -> str:
        with open(image_path, "rb") as f:
            return base64.standard_b64encode(f.read()).decode("utf-8")

    def submit(self, items: Iterable[tuple[str, str, str | None]]) -> BatchSubmitResult:
        """Submit a batch.

        items is an iterable of (custom_id, image_b64, file_name) triples.
        custom_id must be unique within the batch and match what the caller
        will use to look up the result later (we use the queue row id).
        """
        payload_items = []
        for custom_id, b64, name in items:
            payload_items.append({
                "custom_id": custom_id,
                "image_b64": b64,
                "file_name": name,
            })
        if not payload_items:
            raise ValueError("Cannot submit an empty batch")

        body = {
            "license_key":        self.license_key or "DEMO-DEMO-DEMO-DEMO",
            "hardware_id":        self.hardware_id,
            "description_length": self.description_length,
            "items":              payload_items,
        }
        resp = self._post("/batch/submit", json=body)
        if resp.status_code == 402:
            raise CreditsExhaustedError(resp.json().get("detail", "No credits remaining."))
        if resp.status_code != 200:
            raise BatchSubmitError(f"HTTP {resp.status_code}: {resp.text}")
        d = resp.json()
        return BatchSubmitResult(
            batch_id=d["batch_id"],
            items_submitted=d["items_submitted"],
            credits_pre_deducted=d["credits_pre_deducted"],
            credits_remaining=d["credits_remaining"],
            processing_status=d["processing_status"],
        )

    def status(self, batch_id: str) -> BatchStatus:
        resp = self._get(f"/batch/{batch_id}/status", params={
            "license_key": self.license_key or "DEMO-DEMO-DEMO-DEMO",
            "hardware_id": self.hardware_id,
        })
        if resp.status_code != 200:
            raise BatchSubmitError(f"HTTP {resp.status_code}: {resp.text}")
        d = resp.json()
        return BatchStatus(
            batch_id=d["batch_id"],
            processing_status=d["processing_status"],
            items_count=d["items_count"],
            counts=d.get("request_counts") or {},
            results_retrieved=d.get("results_retrieved", False),
        )

    def results(self, batch_id: str) -> BatchResults:
        resp = self._get(f"/batch/{batch_id}/results", params={
            "license_key": self.license_key or "DEMO-DEMO-DEMO-DEMO",
            "hardware_id": self.hardware_id,
        })
        if resp.status_code == 409:
            raise BatchNotReady(resp.json().get("detail", "Batch not ready."))
        if resp.status_code != 200:
            raise BatchSubmitError(f"HTTP {resp.status_code}: {resp.text}")
        d = resp.json()
        rows = [BatchResultRow(
            custom_id=r["custom_id"],
            file_name=r.get("file_name"),
            status=r["status"],
            metadata=r.get("metadata"),
            error=r.get("error"),
        ) for r in d["results"]]
        return BatchResults(
            batch_id=d["batch_id"],
            items_count=d["items_count"],
            succeeded=d["succeeded"],
            failed=d["failed"],
            credits_refunded=d["credits_refunded"],
            credits_remaining=d["credits_remaining"],
            results=rows,
        )

    def wait_for(
        self,
        batch_id: str,
        poll_interval: float = 10.0,
        max_wait: float = 24 * 3600,
        progress_cb=None,
    ) -> BatchResults:
        """Poll until the batch ends, then return results.

        progress_cb(BatchStatus) is called once per poll if provided.
        """
        deadline = time.time() + max_wait
        while True:
            st = self.status(batch_id)
            if progress_cb:
                try:
                    progress_cb(st)
                except Exception:
                    pass
            if st.processing_status == "ended":
                return self.results(batch_id)
            if st.processing_status in ("canceled", "expired"):
                # results endpoint will still tell us refund + per-item details
                return self.results(batch_id)
            if time.time() > deadline:
                raise TimeoutError(f"Batch {batch_id} did not complete within {max_wait}s")
            sleep_for = poll_interval + random.uniform(0, poll_interval / 4)
            time.sleep(sleep_for)

    # ----- HTTP plumbing -----------------------------------------------------

    def _post(self, path: str, json: dict) -> requests.Response:
        return self._request("POST", path, json=json)

    def _get(self, path: str, params: dict) -> requests.Response:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.proxy_url}{path}"
        max_retries = 4
        for attempt in range(max_retries):
            try:
                resp = requests.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    raise
                delay = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"{method} {path} attempt {attempt+1}: {e}. Retry in {delay:.1f}s")
                time.sleep(delay)
                continue

            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = int(float(retry_after)) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                delay += random.uniform(0, 1)
                if attempt == max_retries - 1:
                    return resp
                logger.warning(
                    f"{method} {path} -> {resp.status_code}; "
                    f"backing off {delay:.1f}s (attempt {attempt+1}/{max_retries})"
                )
                time.sleep(delay)
                continue

            return resp
        return resp
