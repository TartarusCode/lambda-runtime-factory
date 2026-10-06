#!/opt/pypy/bin/pypy

import decimal
import http.client
import importlib
import json
import logging
import os
import signal
import socket
import sys
import time
import traceback
from typing import Any, Callable, Dict, Optional, Tuple, Union

# Configure logging before importing helper modules.
logging.basicConfig(
    level=os.getenv("PYPY_RUNTIME_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

RUNTIME_API_ENDPOINT = "/2018-06-01/runtime"
HANDLER = os.getenv("_HANDLER")
RUNTIME_API = os.getenv("AWS_LAMBDA_RUNTIME_API")
RUNTIME_CLIENT_TIMEOUT = float(os.getenv("PYPY_RUNTIME_API_TIMEOUT", "310"))
POLL_RETRY_BACKOFF_SECONDS = float(os.getenv("PYPY_RUNTIME_POLL_BACKOFF", "0.25"))

SHUTDOWN_REQUESTED = False
_COLD_START = True


def _prepend_path(path: str) -> None:
    if path and path not in sys.path:
        sys.path.insert(0, path)


for path in ("/opt/pypy", "/opt/pypy/site-packages", os.getenv("LAMBDA_TASK_ROOT")):
    if path:
        _prepend_path(path)

try:
    from lambda_runtime_pypy.init import run_configured_init_hooks
    from lambda_runtime_pypy.logging import clear_context, set_context
    from lambda_runtime_pypy.tracing import set_trace_id
except ImportError:  # pragma: no cover - bootstrap must stay operable without helpers.
    def run_configured_init_hooks(handler: Callable[..., Any]) -> None:
        return None

    def clear_context() -> None:
        return None

    def set_context(**_: Any) -> None:
        return None

    def set_trace_id(trace_id: Optional[str]) -> None:
        if trace_id:
            os.environ["_X_AMZN_TRACE_ID"] = trace_id


class LambdaContext:
    """AWS Lambda execution context object."""

    def __init__(
        self,
        request_id: str,
        invoked_function_arn: Optional[str],
        deadline_ms: Optional[str],
        trace_id: Optional[str],
    ) -> None:
        self.aws_request_id = request_id
        self.deadline_ms = int(deadline_ms) if deadline_ms is not None else None
        self.function_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        self.function_version = os.getenv("AWS_LAMBDA_FUNCTION_VERSION")
        self.invoked_function_arn = invoked_function_arn
        self.log_group_name = os.getenv("AWS_LAMBDA_LOG_GROUP_NAME")
        self.log_stream_name = os.getenv("AWS_LAMBDA_LOG_STREAM_NAME")
        self.memory_limit_in_mb = os.getenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
        self.trace_id = trace_id
        set_trace_id(trace_id)

    def get_remaining_time_in_millis(self) -> Optional[int]:
        """Return the remaining execution time in milliseconds."""
        if self.deadline_ms is None:
            return None
        return max(0, self.deadline_ms - int(time.time() * 1000))


class RuntimeApiClient:
    """Small persistent client for the Lambda Runtime API."""

    def __init__(self, runtime_api: str, timeout_seconds: float) -> None:
        self.runtime_api = runtime_api
        self.timeout_seconds = timeout_seconds
        self._connection: Optional[http.client.HTTPConnection] = None

    def _connect(self) -> http.client.HTTPConnection:
        if self._connection is None:
            self._connection = http.client.HTTPConnection(
                self.runtime_api,
                timeout=self.timeout_seconds,
            )
        return self._connection

    def _reset(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[http.client.HTTPResponse, bytes]:
        payload_headers = {"Connection": "keep-alive"}
        if headers:
            payload_headers.update(headers)

        try:
            connection = self._connect()
            connection.request(method, path, body=body, headers=payload_headers)
            response = connection.getresponse()
            payload = response.read()
        except (OSError, http.client.HTTPException, socket.timeout):
            self._reset()
            raise

        if response.status >= 400:
            self._reset()
            raise RuntimeError(
                "Runtime API request failed "
                f"({method} {path} -> {response.status} {response.reason}): "
                f"{payload.decode('utf-8', errors='replace')}"
            )

        return response, payload

    def next_invocation(self) -> Tuple[str, Dict[str, Any], LambdaContext]:
        response, payload = self._request("GET", f"{RUNTIME_API_ENDPOINT}/invocation/next")
        request_id = response.getheader("Lambda-Runtime-Aws-Request-Id")
        if not request_id:
            raise RuntimeError("Runtime API did not include Lambda-Runtime-Aws-Request-Id")

        context = LambdaContext(
            request_id=request_id,
            invoked_function_arn=response.getheader("Lambda-Runtime-Invoked-Function-Arn"),
            deadline_ms=response.getheader("Lambda-Runtime-Deadline-Ms"),
            trace_id=response.getheader("Lambda-Runtime-Trace-Id"),
        )
        event = json.loads(payload.decode("utf-8"))
        return request_id, event, context

    def post_init_error(self, error: Exception) -> None:
        self._post_json(f"{RUNTIME_API_ENDPOINT}/init/error", error_payload(error))

    def post_invocation_response(
        self,
        request_id: str,
        handler_response: Union[bytes, str, Dict[str, Any]],
    ) -> None:
        if not isinstance(handler_response, (bytes, str)):
            handler_response = json.dumps(handler_response, default=decimal_serializer)
        if not isinstance(handler_response, bytes):
            handler_response = handler_response.encode("utf-8")
        self._request(
            "POST",
            f"{RUNTIME_API_ENDPOINT}/invocation/{request_id}/response",
            body=handler_response,
            headers={"Content-Type": "application/json"},
        )

    def post_invocation_error(self, request_id: str, error: Exception) -> None:
        self._post_json(
            f"{RUNTIME_API_ENDPOINT}/invocation/{request_id}/error",
            error_payload(error),
        )

    def _post_json(self, path: str, payload: Dict[str, Any]) -> None:
        self._request(
            "POST",
            path,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )


def handle_shutdown(signum: int, _frame: Any) -> None:
    """Request a graceful shutdown after the current lifecycle step."""
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    logger.info("Received signal %s; runtime will exit gracefully", signum)


for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
    signal.signal(shutdown_signal, handle_shutdown)


def decimal_serializer(obj: Any) -> Union[float, Any]:
    """Serialize Decimal objects for JSON encoding."""
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    raise TypeError(f"{repr(obj)} is not JSON serializable")


def error_payload(error: Exception) -> Dict[str, Any]:
    """Build the Lambda Runtime API error payload."""
    return {
        "errorMessage": str(error),
        "errorType": type(error).__name__,
        "stackTrace": traceback.format_exception(type(error), error, error.__traceback__),
    }


def validate_environment() -> None:
    """Validate required environment variables."""
    required_vars = ["AWS_LAMBDA_RUNTIME_API", "_HANDLER"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing_vars)}"
        )


def parse_handler(handler: str) -> Tuple[str, str]:
    """Parse handler string into module path and handler name."""
    module_path, handler_name = handler.rsplit(".", 1)
    if not module_path or not handler_name:
        raise ValueError("Handler must contain both module and function name")
    return module_path.replace("/", "."), handler_name


def load_handler(handler: str) -> Callable[[Dict[str, Any], LambdaContext], Any]:
    """Import and return the configured handler function."""
    module_path, handler_name = parse_handler(handler)
    module = importlib.import_module(module_path)
    function = getattr(module, handler_name)
    if not callable(function):
        raise TypeError(f"Configured handler '{module_path}.{handler_name}' is not callable")
    logger.info("Handler loaded: %s.%s", module_path, handler_name)
    return function


def main() -> None:
    """Main function to handle the Lambda runtime lifecycle."""
    global _COLD_START

    logger.info("Starting Lambda runtime")
    client: Optional[RuntimeApiClient] = None

    try:
        validate_environment()
        client = RuntimeApiClient(RUNTIME_API, RUNTIME_CLIENT_TIMEOUT)
        handler = load_handler(HANDLER)
        run_configured_init_hooks(handler)
    except Exception as error:
        logger.exception("Runtime initialization failed")
        if client is None and RUNTIME_API:
            client = RuntimeApiClient(RUNTIME_API, RUNTIME_CLIENT_TIMEOUT)
        if client is not None:
            try:
                client.post_init_error(error)
            except Exception:
                logger.exception("Failed to report init error to Lambda")
        raise SystemExit(1)

    while not SHUTDOWN_REQUESTED:
        request_id: Optional[str] = None

        try:
            request_id, event, context = client.next_invocation()
        except Exception:
            if SHUTDOWN_REQUESTED:
                break
            logger.exception("Failed to get next invocation")
            time.sleep(POLL_RETRY_BACKOFF_SECONDS)
            continue

        set_context(
            aws_request_id=request_id,
            cold_start=_COLD_START,
            function_name=context.function_name,
            function_version=context.function_version,
            trace_id=context.trace_id,
        )

        try:
            handler_response = handler(event, context)
        except Exception as error:
            logger.exception("Handler execution failed for request %s", request_id)
            try:
                client.post_invocation_error(request_id, error)
            except Exception:
                logger.exception("Failed to send invocation error for request %s", request_id)
        else:
            try:
                client.post_invocation_response(request_id, handler_response)
            except Exception:
                logger.exception(
                    "Failed to send invocation response for request %s",
                    request_id,
                )
        finally:
            _COLD_START = False
            clear_context()

    logger.info("Lambda runtime shutdown complete")


if __name__ == "__main__":
    main()
