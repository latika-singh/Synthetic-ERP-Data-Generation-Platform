"""Global error handling middleware for the API Gateway.

Provides structured JSON error responses for all HTTP error codes and implements
the circuit breaker pattern for external service calls to prevent cascade failures
across the Synthetic ERP Data Generation Platform.

This module is central to the API Gateway's resilience strategy:

- **Structured Error Responses**: Every HTTP error (4xx, 5xx) is returned as a
  consistent JSON payload with error code, message, request ID, ISO 8601 UTC
  timestamp, and request path. In production mode, error details are sanitized
  to prevent information leakage (no stack traces or internal error messages)
  per SOC 2 Type II compliance requirements.

- **Circuit Breaker**: Pre-configured circuit breakers for each downstream
  microservice (Generation Engine, Profiling Service, Quality Service,
  Compliance Service, Provisioning Service) with configurable failure thresholds
  (3-5 failures trigger open state) and recovery timeouts (30-120 seconds).
  Prevents cascade failures when a downstream service becomes unhealthy.

- **Retry with Exponential Backoff**: A decorator for retrying transient errors
  (connection failures, timeouts, 502/503 responses) with a maximum of 3 retries
  and ``2^n`` second delay between attempts.

Usage::

    from api_gateway.middleware.error_handler import (
        register_error_handlers,
        ServiceCircuitBreaker,
        with_retry,
    )

    # In create_app():
    app = Flask(__name__)
    register_error_handlers(app)

    # For external service calls:
    circuit_breakers = ServiceCircuitBreaker(failure_threshold=5, recovery_timeout=30)
    result = circuit_breakers.call("generation_engine", requests.post, url, json=payload)

    # For retry logic:
    @with_retry(max_retries=3, base_delay=1.0)
    def call_external_api():
        return requests.get("http://service/api")
"""

from __future__ import annotations

import time
import traceback
import uuid
from datetime import UTC, datetime
from functools import wraps
from typing import TYPE_CHECKING, Any

from circuitbreaker import CircuitBreakerError, circuit
from flask import Flask, Response, current_app, g, jsonify, request


if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog
from werkzeug.exceptions import (
    BadGateway,
    BadRequest,
    Conflict,
    Forbidden,
    HTTPException,
    InternalServerError,
    MethodNotAllowed,
    NotFound,
    ServiceUnavailable,
    TooManyRequests,
    Unauthorized,
    UnprocessableEntity,
)

from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level structured logger instance.
# Initialised at import time; actual configuration (JSON vs. console output)
# is applied later by ``configure_logging`` in the Application Factory.
# ---------------------------------------------------------------------------
logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Error Code and Default Message Constants
# ---------------------------------------------------------------------------

_ERROR_CODE_MAP: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "UNPROCESSABLE_ENTITY",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_SERVER_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
}
"""Maps HTTP status codes to machine-readable error code strings."""

_DEFAULT_MESSAGES: dict[int, str] = {
    400: "The request was invalid or malformed.",
    401: "Authentication is required to access this resource.",
    403: "You do not have permission to access this resource.",
    404: "The requested resource was not found.",
    405: "The HTTP method is not allowed for this endpoint.",
    409: "The request conflicts with the current state of the resource.",
    422: "The request data could not be processed.",
    429: "Too many requests. Please try again later.",
    500: "An internal server error occurred.",
    502: "The upstream service returned an invalid response.",
    503: "The service is temporarily unavailable. Please try again later.",
}
"""Safe, production-ready messages that reveal no internal implementation details."""

# ---------------------------------------------------------------------------
# Circuit Breaker Defaults
# ---------------------------------------------------------------------------

_DEFAULT_FAILURE_THRESHOLD: int = 5
"""Number of consecutive failures before a circuit breaker transitions to open."""

_DEFAULT_RECOVERY_TIMEOUT: int = 30
"""Seconds a circuit breaker remains open before transitioning to half-open."""

# ---------------------------------------------------------------------------
# Retry Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RETRIES: int = 3
"""Maximum retry attempts after the initial call (4 total attempts)."""

_DEFAULT_BASE_DELAY: float = 1.0
"""Base delay in seconds for the first retry; subsequent retries double it."""

# ---------------------------------------------------------------------------
# Downstream Service Identifiers
# ---------------------------------------------------------------------------

_DOWNSTREAM_SERVICES: tuple[str, ...] = (
    "generation_engine",
    "profiling_service",
    "quality_service",
    "compliance_service",
    "provisioning_service",
)
"""Canonical names for the five downstream microservices."""


# =========================================================================
# Private Helper Functions
# =========================================================================


def _get_request_id() -> str:
    """Retrieve the current request ID or generate a new UUID4.

    Looks for an existing ``request_id`` on Flask's ``g`` object (typically
    set by the logging middleware earlier in the request pipeline).  Falls
    back to generating a fresh UUID4 when no request context is available.

    Returns:
        A UUID string uniquely identifying the current request.
    """
    try:
        existing_id: str | None = getattr(g, "request_id", None)
        if existing_id is not None:
            return str(existing_id)
    except RuntimeError:
        # Raised when accessed outside of a Flask request context (e.g.
        # during application startup or in background workers).
        pass
    return str(uuid.uuid4())


def _get_request_path() -> str:
    """Retrieve the URL path of the current request.

    Returns:
        The request path string, or ``"unknown"`` when no Flask request
        context is active.
    """
    try:
        return request.path
    except RuntimeError:
        return "unknown"


def _get_request_method() -> str:
    """Retrieve the HTTP method of the current request.

    Returns:
        The HTTP method string (e.g. ``"GET"``), or ``"unknown"`` when no
        Flask request context is active.
    """
    try:
        return request.method
    except RuntimeError:
        return "unknown"


def _is_debug_mode() -> bool:
    """Check whether the Flask application is running in debug mode.

    In debug mode, error responses include full stack traces and internal
    error messages.  In production mode (``DEBUG=False``), all details are
    suppressed to prevent information leakage.

    Returns:
        ``True`` when the application is running with ``DEBUG=True``.
    """
    try:
        return bool(current_app.config.get("DEBUG", False))
    except RuntimeError:
        return False


def _create_error_response(
    error_code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Build a structured JSON error response.

    Every error returned by the API Gateway follows a consistent format
    to enable reliable programmatic consumption and audit trail correlation.

    The response body structure::

        {
            "error": {
                "code": "BAD_REQUEST",
                "message": "The request was invalid or malformed.",
                "request_id": "550e8400-e29b-41d4-a716-446655440000",
                "timestamp": "2025-01-15T12:34:56.789012+00:00",
                "path": "/api/v1/generation/jobs",
                "details": {}
            }
        }

    Args:
        error_code: Machine-readable error code (e.g. ``"BAD_REQUEST"``).
        message: Human-readable error description.  In production, this
            should be a generic message that does not reveal internals.
        status_code: The HTTP status code for the response.
        details: Optional dictionary of additional context.  In production,
            this should be ``None`` or empty to prevent information leakage.

    Returns:
        A tuple of ``(Response, status_code)`` suitable for returning
        directly from a Flask error handler or route function.
    """
    request_id = _get_request_id()
    response_body: dict[str, Any] = {
        "error": {
            "code": error_code,
            "message": message,
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "path": _get_request_path(),
            "details": details if details is not None else {},
        }
    }
    response: Response = jsonify(response_body)
    response.status_code = status_code
    return response, status_code


# =========================================================================
# HTTP Error Handlers (one per supported status code)
# =========================================================================


def _handle_400(error: BadRequest) -> tuple[Response, int]:
    """Handle 400 Bad Request errors.

    Returned when the client sends a request with invalid syntax, missing
    required parameters, or malformed body content.

    Args:
        error: The ``BadRequest`` exception raised by Flask or application
            code.

    Returns:
        Structured JSON error response with HTTP 400.
    """
    description = str(error.description) if error.description else ""
    message = description if _is_debug_mode() else _DEFAULT_MESSAGES[400]
    logger.warning(
        "http_error",
        error_code="BAD_REQUEST",
        status_code=400,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        detail=description,
    )
    return _create_error_response("BAD_REQUEST", message, 400)


def _handle_401(error: Unauthorized) -> tuple[Response, int]:
    """Handle 401 Unauthorized errors.

    Returned when the request lacks valid authentication credentials
    (missing or expired JWT token, invalid Auth0 session).

    Args:
        error: The ``Unauthorized`` exception raised by the JWT
            authentication middleware.

    Returns:
        Structured JSON error response with HTTP 401.
    """
    description = str(error.description) if error.description else ""
    message = description if _is_debug_mode() else _DEFAULT_MESSAGES[401]
    logger.warning(
        "http_error",
        error_code="UNAUTHORIZED",
        status_code=401,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        detail=description,
    )
    return _create_error_response("UNAUTHORIZED", message, 401)


def _handle_403(error: Forbidden) -> tuple[Response, int]:
    """Handle 403 Forbidden errors.

    Returned when the authenticated user does not have the required role
    or permission to access the requested resource (RBAC enforcement).

    Args:
        error: The ``Forbidden`` exception raised by RBAC/OPA checks.

    Returns:
        Structured JSON error response with HTTP 403.
    """
    description = str(error.description) if error.description else ""
    message = description if _is_debug_mode() else _DEFAULT_MESSAGES[403]
    logger.warning(
        "http_error",
        error_code="FORBIDDEN",
        status_code=403,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        detail=description,
    )
    return _create_error_response("FORBIDDEN", message, 403)


def _handle_404(error: NotFound) -> tuple[Response, int]:
    """Handle 404 Not Found errors.

    Returned when the requested route does not exist or the specified
    resource (generation job, profile, schema) cannot be found.

    Args:
        error: The ``NotFound`` exception raised by Flask routing or
            application code.

    Returns:
        Structured JSON error response with HTTP 404.
    """
    description = str(error.description) if error.description else ""
    message = description if _is_debug_mode() else _DEFAULT_MESSAGES[404]
    # 404 is a routine occurrence; log at INFO level to reduce noise.
    logger.info(
        "http_error",
        error_code="NOT_FOUND",
        status_code=404,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
    )
    return _create_error_response("NOT_FOUND", message, 404)


def _handle_405(error: MethodNotAllowed) -> tuple[Response, int]:
    """Handle 405 Method Not Allowed errors.

    Returned when the HTTP method used is not supported for the requested
    endpoint (e.g. ``DELETE`` on a read-only resource).

    Args:
        error: The ``MethodNotAllowed`` exception raised by Flask routing.

    Returns:
        Structured JSON error response with HTTP 405 and a list of
        allowed methods in the ``details`` field.
    """
    description = str(error.description) if error.description else ""
    message = description if _is_debug_mode() else _DEFAULT_MESSAGES[405]
    valid_methods = list(error.valid_methods) if error.valid_methods else []
    details: dict[str, Any] = {}
    if valid_methods:
        details["allowed_methods"] = valid_methods
    logger.warning(
        "http_error",
        error_code="METHOD_NOT_ALLOWED",
        status_code=405,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        allowed_methods=valid_methods,
    )
    return _create_error_response("METHOD_NOT_ALLOWED", message, 405, details)


def _handle_409(error: Conflict) -> tuple[Response, int]:
    """Handle 409 Conflict errors.

    Returned when the request cannot be completed because it conflicts
    with the current state of the resource (e.g. duplicate job submission,
    concurrent modification).

    Args:
        error: The ``Conflict`` exception raised by application code.

    Returns:
        Structured JSON error response with HTTP 409.
    """
    description = str(error.description) if error.description else ""
    message = description if _is_debug_mode() else _DEFAULT_MESSAGES[409]
    logger.warning(
        "http_error",
        error_code="CONFLICT",
        status_code=409,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        detail=description,
    )
    return _create_error_response("CONFLICT", message, 409)


def _handle_422(error: UnprocessableEntity) -> tuple[Response, int]:
    """Handle 422 Unprocessable Entity errors.

    Returned when the request body is syntactically valid but semantically
    incorrect (e.g. Pydantic validation failure, invalid generation
    parameters).

    Args:
        error: The ``UnprocessableEntity`` exception raised by validation
            logic.

    Returns:
        Structured JSON error response with HTTP 422.
    """
    description = str(error.description) if error.description else ""
    message = description if _is_debug_mode() else _DEFAULT_MESSAGES[422]
    logger.warning(
        "http_error",
        error_code="UNPROCESSABLE_ENTITY",
        status_code=422,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        detail=description,
    )
    return _create_error_response("UNPROCESSABLE_ENTITY", message, 422)


def _handle_429(error: TooManyRequests) -> tuple[Response, int]:
    """Handle 429 Too Many Requests errors from rate limiting.

    Returned when a client exceeds the configured rate limit tier
    (60/300/1000 requests per minute depending on role).

    Args:
        error: The ``TooManyRequests`` exception raised by the rate
            limiter middleware.

    Returns:
        Structured JSON error response with HTTP 429 and optional
        ``retry_after_seconds`` hint.
    """
    retry_after: int | None = getattr(error, "retry_after", None)
    details: dict[str, Any] = {}
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    logger.warning(
        "rate_limit_exceeded",
        error_code="TOO_MANY_REQUESTS",
        status_code=429,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        retry_after=retry_after,
    )
    return _create_error_response(
        "TOO_MANY_REQUESTS", _DEFAULT_MESSAGES[429], 429, details
    )


def _handle_500(error: InternalServerError) -> tuple[Response, int]:
    """Handle 500 Internal Server Error.

    In development mode (``DEBUG=True``), the response includes the full
    stack trace and the original exception message to aid debugging.
    In production mode, all internal details are suppressed to prevent
    information leakage per SOC 2 Type II requirements.

    Args:
        error: The ``InternalServerError`` exception.  May contain an
            ``original_exception`` attribute set by Flask when it wraps
            an unhandled exception.

    Returns:
        Structured JSON error response with HTTP 500.
    """
    details: dict[str, Any] | None = None
    if _is_debug_mode():
        original = getattr(error, "original_exception", None)
        tb = traceback.format_exc()
        # Avoid including an empty/useless traceback string.
        tb_content = tb if tb and "NoneType: None" not in tb else None
        details = {
            "traceback": tb_content,
            "original_error": str(original) if original else str(
                error.description
            ),
        }
    logger.error(
        "internal_server_error",
        error_code="INTERNAL_SERVER_ERROR",
        status_code=500,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        exc_info=True,
    )
    return _create_error_response(
        "INTERNAL_SERVER_ERROR", _DEFAULT_MESSAGES[500], 500, details
    )


def _handle_502(error: BadGateway) -> tuple[Response, int]:
    """Handle 502 Bad Gateway errors from upstream services.

    Returned when an upstream microservice (Generation Engine, Profiling
    Service, etc.) returns an invalid or unreadable response.

    Args:
        error: The ``BadGateway`` exception.

    Returns:
        Structured JSON error response with HTTP 502.
    """
    description = str(error.description) if error.description else ""
    details: dict[str, Any] | None = None
    if _is_debug_mode():
        details = {"upstream_error": description}
    logger.error(
        "bad_gateway",
        error_code="BAD_GATEWAY",
        status_code=502,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        detail=description,
    )
    return _create_error_response("BAD_GATEWAY", _DEFAULT_MESSAGES[502], 502, details)


def _handle_503(error: ServiceUnavailable) -> tuple[Response, int]:
    """Handle 503 Service Unavailable errors.

    Returned when a downstream service is unreachable, a circuit breaker
    is open, or the platform is undergoing maintenance.

    Args:
        error: The ``ServiceUnavailable`` exception.

    Returns:
        Structured JSON error response with HTTP 503 and optional
        ``retry_after_seconds`` hint.
    """
    retry_after: int | None = getattr(error, "retry_after", None)
    details: dict[str, Any] = {}
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    logger.error(
        "service_unavailable",
        error_code="SERVICE_UNAVAILABLE",
        status_code=503,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        retry_after=retry_after,
    )
    return _create_error_response(
        "SERVICE_UNAVAILABLE", _DEFAULT_MESSAGES[503], 503, details
    )


# =========================================================================
# Generic / Catch-All Exception Handlers
# =========================================================================


def _handle_generic_exception(error: Exception) -> tuple[Response, int]:
    """Handle any unhandled exception not covered by specific handlers.

    Catches both ``HTTPException`` subclasses (for status codes without
    dedicated handlers) and arbitrary Python exceptions.  In production
    mode, error details are fully sanitized to prevent information leakage.

    Args:
        error: The unhandled exception instance.

    Returns:
        Structured JSON error response.  Uses the exception's HTTP status
        code if available; defaults to 500 for non-HTTP exceptions.
    """
    if isinstance(error, HTTPException):
        status_code: int = error.code if error.code is not None else 500
        error_code = _ERROR_CODE_MAP.get(status_code, "UNKNOWN_ERROR")
        default_message = _DEFAULT_MESSAGES.get(
            status_code, "An unexpected error occurred."
        )
        description = str(error.description) if error.description else ""
        message = description if _is_debug_mode() else default_message
        details: dict[str, Any] | None = None
    else:
        status_code = 500
        error_code = "INTERNAL_SERVER_ERROR"
        message = _DEFAULT_MESSAGES[500]
        details = None
        if _is_debug_mode():
            tb = traceback.format_exc()
            details = {
                "exception_type": type(error).__name__,
                "exception_message": str(error),
                "traceback": (
                    tb if tb and "NoneType: None" not in tb else None
                ),
            }

    logger.error(
        "unhandled_exception",
        error_code=error_code,
        status_code=status_code,
        exception_type=type(error).__name__,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
        exc_info=not isinstance(error, HTTPException),
    )
    return _create_error_response(error_code, message, status_code, details)


def _handle_circuit_breaker_error(
    error: CircuitBreakerError,
) -> tuple[Response, int]:
    """Handle ``CircuitBreakerError`` when a circuit is in open state.

    When a downstream service's circuit breaker is open (too many
    consecutive failures exceeded the threshold), this handler returns a
    503 Service Unavailable response identifying the affected service.

    Args:
        error: The ``CircuitBreakerError`` raised by the circuitbreaker
            library when a call is attempted on an open circuit.

    Returns:
        Structured JSON error response with HTTP 503 and circuit breaker
        context in the ``details`` field.
    """
    try:
        breaker_name = str(error) if str(error) else "unknown_service"
    except Exception:
        # CircuitBreakerError.__str__ accesses internal circuit breaker
        # attributes that may not always be available.
        breaker_name = "unknown_service"
    details: dict[str, Any] = {
        "circuit_breaker": breaker_name,
        "reason": "Circuit breaker is open due to repeated downstream failures.",
    }
    logger.error(
        "circuit_breaker_open",
        error_code="SERVICE_UNAVAILABLE",
        status_code=503,
        circuit_breaker=breaker_name,
        path=_get_request_path(),
        method=_get_request_method(),
        request_id=_get_request_id(),
    )
    return _create_error_response(
        "SERVICE_UNAVAILABLE",
        _DEFAULT_MESSAGES[503],
        503,
        details,
    )


# =========================================================================
# ServiceCircuitBreaker Class
# =========================================================================


class ServiceCircuitBreaker:
    """Manages circuit breakers for all downstream microservice calls.

    Provides pre-configured circuit breaker instances for each of the five
    downstream services (Generation Engine, Profiling Service, Quality
    Service, Compliance Service, Provisioning Service).  Each circuit
    breaker independently tracks failure counts and transitions between
    **closed** → **open** → **half-open** states.

    When a circuit is open, calls to that service immediately raise
    :class:`CircuitBreakerError` instead of attempting the request,
    preventing cascade failures across the platform.

    Attributes:
        generation_engine_cb: Circuit breaker proxy for Generation Engine.
        profiling_service_cb: Circuit breaker proxy for Profiling Service.
        quality_service_cb: Circuit breaker proxy for Quality Service.
        compliance_service_cb: Circuit breaker proxy for Compliance Service.
        provisioning_service_cb: Circuit breaker proxy for Provisioning
            Service.

    Example::

        cb = ServiceCircuitBreaker(failure_threshold=5, recovery_timeout=30)

        # Route through the circuit breaker via call():
        result = cb.call(
            "generation_engine", requests.post, url, json=payload
        )

        # Or use a specific circuit breaker proxy directly:
        result = cb.generation_engine_cb(requests.post, url, json=payload)
    """

    def __init__(
        self,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: int = _DEFAULT_RECOVERY_TIMEOUT,
        expected_exception: type[Exception] = Exception,
    ) -> None:
        """Initialise circuit breakers for all downstream services.

        Creates an independent circuit breaker for each downstream
        microservice.  All breakers share the same threshold and timeout
        configuration but maintain separate failure counts and states.

        Args:
            failure_threshold: Number of consecutive failures before a
                circuit opens.  Recommended range: 3-5. Defaults to 5.
            recovery_timeout: Seconds to wait in the open state before
                transitioning to half-open and allowing a probe request.
                Recommended range: 30-120. Defaults to 30.
            expected_exception: The base exception type that counts as a
                failure.  Defaults to ``Exception`` (any exception).
        """
        self._failure_threshold: int = failure_threshold
        self._recovery_timeout: int = recovery_timeout
        self._expected_exception: type[Exception] = expected_exception

        # Build per-service circuit-breaker-protected proxy functions.
        # Each proxy wraps a provided callable with its own independent
        # circuit breaker state.
        self._service_proxies: dict[str, Callable[..., Any]] = {}

        for service_name in _DOWNSTREAM_SERVICES:
            proxy = self._create_service_proxy(service_name)
            self._service_proxies[service_name] = proxy
            # Expose as public attribute (e.g. self.generation_engine_cb).
            setattr(self, f"{service_name}_cb", proxy)

        logger.info(
            "circuit_breakers_initialized",
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            services=list(_DOWNSTREAM_SERVICES),
        )

    # -- Explicit attribute declarations for static analysis / IDE support --
    generation_engine_cb: Callable[..., Any]
    profiling_service_cb: Callable[..., Any]
    quality_service_cb: Callable[..., Any]
    compliance_service_cb: Callable[..., Any]
    provisioning_service_cb: Callable[..., Any]

    def _create_service_proxy(
        self, service_name: str
    ) -> Callable[..., Any]:
        """Create a circuit-breaker-protected proxy for a single service.

        The returned function accepts a callable and its arguments, and
        delegates the actual invocation through the circuit breaker.
        Failures (exceptions matching ``expected_exception``) are tracked
        by the circuit breaker; once the failure threshold is reached the
        circuit opens and subsequent calls raise ``CircuitBreakerError``.

        Args:
            service_name: Identifier for the downstream service
                (e.g. ``"generation_engine"``).

        Returns:
            A callable with signature
            ``(func, *args, **kwargs) -> Any`` that routes through the
            circuit breaker.
        """

        @circuit(
            failure_threshold=self._failure_threshold,
            recovery_timeout=self._recovery_timeout,
            expected_exception=self._expected_exception,
            name=service_name,
        )
        def _proxy(
            func: Callable[..., Any], *args: Any, **kwargs: Any
        ) -> Any:
            """Proxy function protected by a circuit breaker.

            Delegates to the provided *func* while the circuit breaker
            monitors for failures.
            """
            return func(*args, **kwargs)

        return _proxy

    def call(
        self,
        service_name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute a function through the named service's circuit breaker.

        Routes the call through the appropriate circuit breaker based on
        ``service_name``.  If the circuit is open,
        :class:`CircuitBreakerError` is raised immediately without
        attempting the underlying call.

        Args:
            service_name: The downstream service identifier.  Must be one
                of ``"generation_engine"``, ``"profiling_service"``,
                ``"quality_service"``, ``"compliance_service"``, or
                ``"provisioning_service"``.
            func: The callable to execute (e.g. ``requests.post``).
            *args: Positional arguments forwarded to *func*.
            **kwargs: Keyword arguments forwarded to *func*.

        Returns:
            The return value of *func*.

        Raises:
            CircuitBreakerError: If the service's circuit breaker is in
                the open state.
            ValueError: If *service_name* is not a recognised service.
        """
        proxy = self._service_proxies.get(service_name)
        if proxy is None:
            available = ", ".join(sorted(self._service_proxies.keys()))
            raise ValueError(
                f"Unknown service: '{service_name}'. "
                f"Available services: {available}"
            )

        try:
            result = proxy(func, *args, **kwargs)
        except CircuitBreakerError:
            logger.error(
                "circuit_breaker_rejected",
                service=service_name,
                path=_get_request_path(),
                method=_get_request_method(),
                request_id=_get_request_id(),
            )
            raise
        except self._expected_exception as exc:
            logger.warning(
                "service_call_failed",
                service=service_name,
                exception_type=type(exc).__name__,
                error=str(exc),
                path=_get_request_path(),
                method=_get_request_method(),
                request_id=_get_request_id(),
            )
            raise
        return result


# =========================================================================
# Retry Decorator with Exponential Backoff
# =========================================================================


def with_retry(
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    retryable_exceptions: tuple[type[Exception], ...] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory implementing exponential backoff retry logic.

    Wraps a function so that transient failures are automatically retried
    with increasing delay between attempts.  The delay follows the formula
    ``base_delay * 2^attempt`` where *attempt* starts at 0 for the first
    retry:

    - Retry 1 delay: ``base_delay * 2^0`` = 1.0 s (default)
    - Retry 2 delay: ``base_delay * 2^1`` = 2.0 s
    - Retry 3 delay: ``base_delay * 2^2`` = 4.0 s

    Default retryable exceptions cover network-related transient failures:
    ``ConnectionError``, ``TimeoutError``, and ``OSError``.

    Args:
        max_retries: Maximum number of retry attempts after the initial
            call.  Defaults to 3 (4 total attempts).
        base_delay: Base delay in seconds before the first retry.
            Subsequent retries double the delay.  Defaults to 1.0.
        retryable_exceptions: Tuple of exception types that trigger a
            retry.  Defaults to
            ``(ConnectionError, TimeoutError, OSError)``.

    Returns:
        A decorator that wraps the target function with retry logic.

    Example::

        @with_retry(max_retries=3, base_delay=1.0)
        def fetch_remote_data(url: str) -> dict:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()

        @with_retry(
            max_retries=2,
            base_delay=0.5,
            retryable_exceptions=(ConnectionError, TimeoutError),
        )
        def post_to_service(payload: dict) -> dict:
            return requests.post(SERVICE_URL, json=payload).json()
    """
    if retryable_exceptions is None:
        retryable_exceptions = (ConnectionError, TimeoutError, OSError)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:  # type: ignore[misc]
                    last_exception = exc

                    if attempt < max_retries:
                        # Exponential backoff: base_delay * 2^attempt
                        delay: float = base_delay * (2 ** attempt)
                        logger.warning(
                            "retry_attempt",
                            function=func.__name__,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            delay_seconds=delay,
                            exception_type=type(exc).__name__,
                            error=str(exc),
                            request_id=_get_request_id(),
                        )
                        time.sleep(delay)
                    else:
                        # All retries exhausted
                        logger.error(
                            "retry_exhausted",
                            function=func.__name__,
                            total_attempts=max_retries + 1,
                            exception_type=type(exc).__name__,
                            error=str(exc),
                            request_id=_get_request_id(),
                        )

            # Re-raise the last captured exception after exhausting retries.
            if last_exception is not None:
                raise last_exception

            # Defensive guard: should be unreachable.
            raise RuntimeError(
                f"Retry logic error in {func.__name__}: "
                "no result and no exception captured."
            )

        return wrapper

    return decorator


# =========================================================================
# Error Handler Registration
# =========================================================================


def register_error_handlers(app: Flask) -> None:
    """Register all error handlers on the Flask application instance.

    Attaches structured JSON error handlers for every supported HTTP error
    code (400, 401, 403, 404, 405, 409, 422, 429, 500, 502, 503), a
    generic fallback handler for all other ``Exception`` types, and a
    dedicated handler for :class:`CircuitBreakerError`.

    This function should be called **once** during application
    initialisation, typically inside the ``create_app()`` Application
    Factory, after all Blueprints have been registered.

    Args:
        app: The Flask application instance to register error handlers on.

    Example::

        def create_app() -> Flask:
            app = Flask(__name__)
            # ... register blueprints, extensions, etc.
            register_error_handlers(app)
            return app
    """
    # -- Specific HTTP status code handlers --
    app.register_error_handler(400, _handle_400)
    app.register_error_handler(401, _handle_401)
    app.register_error_handler(403, _handle_403)
    app.register_error_handler(404, _handle_404)
    app.register_error_handler(405, _handle_405)
    app.register_error_handler(409, _handle_409)
    app.register_error_handler(422, _handle_422)
    app.register_error_handler(429, _handle_429)
    app.register_error_handler(500, _handle_500)
    app.register_error_handler(502, _handle_502)
    app.register_error_handler(503, _handle_503)

    # -- Circuit breaker error handler (matched before generic Exception) --
    app.register_error_handler(
        CircuitBreakerError, _handle_circuit_breaker_error
    )

    # -- Generic catch-all for unhandled exceptions --
    app.register_error_handler(Exception, _handle_generic_exception)

    logger.info(
        "error_handlers_registered",
        http_codes=[400, 401, 403, 404, 405, 409, 422, 429, 500, 502, 503],
        additional_handlers=["CircuitBreakerError", "Exception"],
        total_handlers=13,
    )
