"""Optional X-Ray helpers that work with or without aws_xray_sdk installed."""

import contextlib
import os
from typing import Any, Dict, Iterator, Optional


def _xray_recorder() -> Any:
    try:
        from aws_xray_sdk.core import xray_recorder
    except ImportError:
        return None
    return xray_recorder


def set_trace_id(trace_id: Optional[str]) -> None:
    """Set the current trace header for downstream SDKs."""
    if trace_id:
        os.environ["_X_AMZN_TRACE_ID"] = trace_id


def current_trace_id() -> Optional[str]:
    """Return the active X-Ray trace header."""
    return os.getenv("_X_AMZN_TRACE_ID")


@contextlib.contextmanager
def subsegment(
    name: str,
    *,
    annotations: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    namespace: str = "lambda_runtime_pypy",
) -> Iterator[Any]:
    """Create an X-Ray subsegment when aws_xray_sdk is available."""
    recorder = _xray_recorder()
    if recorder is None:
        yield None
        return

    recorder.begin_subsegment(name)
    try:
        if annotations:
            for key, value in annotations.items():
                recorder.put_annotation(key, value)
        if metadata:
            for key, value in metadata.items():
                recorder.put_metadata(key, value, namespace)
        yield recorder.current_subsegment()
    except Exception:
        raise
    finally:
        recorder.end_subsegment()
