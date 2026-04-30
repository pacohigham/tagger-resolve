# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Tagger proxy client.

Sends a stitched JPEG grid to the Tagger Render server, which validates
the license, deducts a credit, runs Claude, and returns metadata.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Dict

import requests

logger = logging.getLogger(__name__)


class CreditsExhaustedError(Exception):
    """Raised when the proxy returns HTTP 402 -- no credits remaining."""


class MissingCredentialsError(Exception):
    """Raised when license_key or hardware_id are not configured."""


class ClaudeAnalyzer:
    def __init__(
        self,
        proxy_url: str,
        license_key: str,
        hardware_id: str,
        max_retries: int = 3,
        description_length: str = "standard",
        schema_version: str = "v2",
        timeout: int = 60,
    ):
        self.proxy_url = proxy_url.rstrip("/")
        self.license_key = license_key
        self.hardware_id = hardware_id
        self.max_retries = max_retries
        self.description_length = description_length
        self.schema_version = schema_version
        self.timeout = timeout

    @staticmethod
    def encode_image(image_path: str) -> str:
        with open(image_path, "rb") as f:
            return base64.standard_b64encode(f.read()).decode("utf-8")

    def analyze_grid(self, stitched_path: str) -> Dict[str, str]:
        if not stitched_path:
            return {}
        if not self.proxy_url:
            logger.error("proxy_url not configured")
            return {}
        if not self.hardware_id:
            raise MissingCredentialsError(
                "Hardware ID is not set. Open Settings to configure it."
            )

        effective_key = self.license_key or "DEMO-DEMO-DEMO-DEMO"

        try:
            image_b64 = self.encode_image(stitched_path)
        except Exception as e:
            logger.error(f"Could not encode image: {e}")
            return {}

        payload = {
            "license_key": effective_key,
            "hardware_id": self.hardware_id,
            "image_b64": image_b64,
            "description_length": self.description_length,
            "schema_version": self.schema_version,
        }

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    f"{self.proxy_url}/process_request",
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    metadata = body.get("metadata", body)
                    return self._normalize(metadata)
                if resp.status_code == 402:
                    detail = resp.json().get("detail", "No credits remaining.")
                    raise CreditsExhaustedError(detail)
                if resp.status_code == 403:
                    logger.error(f"License error: {resp.text}")
                    return {}
                raise requests.RequestException(
                    f"HTTP {resp.status_code}: {resp.text}"
                )
            except requests.RequestException as e:
                if attempt < self.max_retries - 1:
                    delay = 2 ** attempt
                    logger.warning(
                        f"Proxy attempt {attempt + 1}/{self.max_retries} failed: {e}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"Proxy error after {self.max_retries} attempts: {e}")
                    return {}
        return {}

    @staticmethod
    def _normalize(metadata: dict) -> Dict[str, object]:
        """Coerce server response into the shape we hand to Resolve.

        Lists are kept as lists so the writer can comma-join them per
        target field (Keyword expects comma-separated, others get the
        raw text). None values become empty strings.
        """
        result: Dict[str, object] = {}
        for key, value in metadata.items():
            if value is None:
                result[key] = ""
            elif isinstance(value, list):
                result[key] = [str(v) for v in value]
            else:
                result[key] = str(value)
        return result


def test_proxy_connection(proxy_url: str) -> bool:
    try:
        r = requests.get(f"{proxy_url.rstrip('/')}/health", timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"Proxy connection failed: {e}")
        return False
