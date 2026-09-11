from collections.abc import Callable, Iterable
from typing import Any, Dict, List

import utils.ssl_compat  # noqa: F401  — trust OS CAs for requests HTTPS


class BaseConnector:
    """Base interface for all job source connectors."""

    # Set by the fetch pipeline so a grabbed job can be stored before fetch_jobs returns.
    _on_raw_job: Callable[[Dict[str, Any]], None] | None = None

    def fetch_jobs(self) -> List[Dict[str, Any]]:
        """Fetch raw jobs from source.

        Call ``_emit`` as soon as each job is complete so the pipeline can
        persist it while this method is still running.
        """
        raise NotImplementedError

    def normalize(self, raw_job: Dict[str, Any]) -> Dict[str, Any]:
        """Convert to unified schema"""
        raise NotImplementedError

    def get_source_name(self) -> str:
        """Return source identifier"""
        raise NotImplementedError

    def _emit(
        self,
        raw_job: Dict[str, Any],
        bucket: List[Dict[str, Any]] | None = None,
    ) -> None:
        """Record a grabbed job and hand it to the pipeline immediately."""
        if bucket is not None:
            bucket.append(raw_job)
        sink = getattr(self, "_on_raw_job", None)
        if callable(sink):
            sink(raw_job)

    def _emit_many(
        self,
        jobs: Iterable[Dict[str, Any]],
        bucket: List[Dict[str, Any]] | None = None,
    ) -> None:
        for job in jobs:
            self._emit(job, bucket)
