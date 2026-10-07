"""Prometheus metrics.

Each application instance owns its registry (no global state, test friendly).
When ``PROMETHEUS_MULTIPROC_DIR`` is set (Gunicorn with several workers), the
values are written to shared files and aggregated at scrape time.
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


class AppMetrics:
    """All application metrics, bound to one registry."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        r = self.registry
        self.http_requests = Counter(
            "mab_http_requests_total",
            "HTTP requests by method, route template and status code.",
            ["method", "route", "status"],
            registry=r,
        )
        self.http_latency = Histogram(
            "mab_http_request_duration_seconds",
            "HTTP request latency by method and route template.",
            ["method", "route"],
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.uploads = Counter(
            "mab_uploads_total", "Uploaded files by outcome.", ["outcome"], registry=r
        )
        self.vision_latency = Histogram(
            "mab_vision_inference_seconds",
            "Time spent tagging one image.",
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.vision_failures = Counter(
            "mab_vision_failures_total", "Images the vision model failed to tag.", registry=r
        )
        self.ai_generations = Counter(
            "mab_ai_generations_total", "Caption generations by outcome.", ["outcome"], registry=r
        )
        self.ai_latency = Histogram(
            "mab_ai_generation_seconds",
            "End-to-end caption generation latency (including retries).",
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.publish_requests = Counter(
            "mab_publish_requests_total",
            "Posts sent to the publishing provider by mode and outcome.",
            ["provider", "mode", "outcome"],
            registry=r,
        )
        self.oauth_events = Counter(
            "mab_oauth_events_total",
            "OAuth connection events by outcome.",
            ["provider", "outcome"],
            registry=r,
        )
        self.rate_limited = Counter(
            "mab_rate_limited_total",
            "Requests rejected by the rate limiter.",
            ["scope"],
            registry=r,
        )
        self.vision_ready = Gauge(
            "mab_vision_model_ready",
            "1 when the vision model is loaded.",
            registry=r,
            multiprocess_mode="max",
        )

    def render(self) -> tuple[bytes, str]:
        """Exposition-format payload and its content type."""
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
            return generate_latest(registry), CONTENT_TYPE_LATEST
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
