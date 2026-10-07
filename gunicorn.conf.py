"""Gunicorn configuration (production process manager, Linux/macOS).

    gunicorn -c gunicorn.conf.py app.main:app

Every value can be overridden with an environment variable.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

bind = os.getenv("BIND", f"0.0.0.0:{os.getenv('PORT', '8000')}")
# Each worker loads its own ResNet-50 (~300 MB RSS with PyTorch); size accordingly.
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
worker_class = "uvicorn_worker.UvicornWorker"
# Vision inference + AI calls can take a while on small CPUs.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "0"))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "0"))
# Trust X-Forwarded-* only from the reverse proxy.
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
# Fork *before* loading PyTorch: each worker initialises its own model safely.
preload_app = False
# The app writes its own structured access log.
accesslog = None
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
worker_tmp_dir = "/dev/shm" if Path("/dev/shm").is_dir() else None  # noqa: S108
# Gunicorn's control socket defaults to the working directory, which is
# read-only in the container; keep it on the /tmp tmpfs instead.
control_socket = os.getenv("GUNICORN_CONTROL_SOCKET", "/tmp/gunicorn.ctl")  # noqa: S108


def on_starting(server: Any) -> None:
    """Reset the Prometheus multiprocess directory on (re)start."""
    directory = os.getenv("PROMETHEUS_MULTIPROC_DIR")
    if directory:
        shutil.rmtree(directory, ignore_errors=True)
        Path(directory).mkdir(parents=True, exist_ok=True)


def child_exit(server: Any, worker: Any) -> None:
    """Drop metrics of dead workers so gauges stay correct."""
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)  # type: ignore[no-untyped-call]
