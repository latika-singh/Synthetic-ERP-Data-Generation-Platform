"""
Circuit Breaker Pattern Implementation for Synthetic ERP Data Generation Platform.

Provides resilience against cascade failures when calling external services
(Auth0, ERP systems, target databases, cloud storage, inter-service REST calls).
Implements the circuit breaker pattern with configurable thresholds, exponential
backoff retry, and centralized state monitoring.

Key Components:
    - ServiceCircuitBreaker: Extended CircuitBreaker with structured logging
      and automatic registry integration.
    - CircuitBreakerRegistry: Singleton registry for monitoring all active
      circuit breakers across a service instance.
    - circuit_breaker_decorator: Decorator factory combining circuit breaker
      with exponential backoff retry logic.
    - create_circuit_breaker: Factory function for creating configured breakers.
    - with_circuit_breaker: Functional API for one-off protected calls.
    - handle_circuit_breaker_error: Utility for standardized error responses.

State Machine:
    CLOSED ──(failures >= threshold)──► OPEN
    OPEN ──(recovery_timeout expires)──► HALF_OPEN
    HALF_OPEN ──(test call succeeds)──► CLOSED
    HALF_OPEN ──(test call fails)──────► OPEN

Configuration Bounds:
    - Failure threshold: 1-10 consecutive failures (default 5)
    - Recovery timeout: 1-120 seconds (default 30)
    - Max retries with backoff: 0-N attempts (default 3)
    - Backoff base: exponential delay = base ** attempt (default 2.0)

Usage:
    from shared.middleware.circuit_breaker import (
        circuit_breaker_decorator,
        create_circuit_breaker,
        CircuitBreakerRegistry,
        CircuitBreakerError,
    )

    # Decorator approach (recommended)
    @circuit_breaker_decorator(
        name="auth0_api",
        failure_threshold=3,
        recovery_timeout=60,
    )
    def call_auth0(token: str) -> dict:
        return requests.get(...).json()

    # Factory approach
    breaker = create_circuit_breaker(name="erp_connector", failure_threshold=5)
    result = breaker.call(fetch_erp_schema, connection_params)

    # Monitoring
    registry = CircuitBreakerRegistry.get_instance()
    status = registry.get_status()
"""

# Standard library imports
import functools
import threading
import time
from collections.abc import Callable
from typing import Any, Optional, TypeVar

# Third-party imports
from circuitbreaker import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    CircuitBreaker,
    CircuitBreakerError,
)

# Internal imports
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Type variable for preserving decorated function signatures
# ---------------------------------------------------------------------------
F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# Module-level structured logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Default configuration constants
# ---------------------------------------------------------------------------

DEFAULT_FAILURE_THRESHOLD: int = 5
"""Number of consecutive failures before opening the circuit."""

DEFAULT_RECOVERY_TIMEOUT: int = 30
"""Seconds before attempting a test call in half-open state."""

DEFAULT_EXPECTED_EXCEPTION: type = Exception
"""Default exception type to treat as a failure."""

MAX_FAILURE_THRESHOLD: int = 10
"""Upper bound for the configurable failure threshold."""

MAX_RECOVERY_TIMEOUT: int = 120
"""Upper bound for the configurable recovery timeout in seconds."""

DEFAULT_MAX_RETRIES: int = 3
"""Default maximum retry attempts with exponential backoff."""

DEFAULT_BACKOFF_BASE: float = 2.0
"""Base for exponential backoff delay calculation (delay = base ** attempt)."""

# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------
__all__ = [
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_EXPECTED_EXCEPTION",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RECOVERY_TIMEOUT",
    "MAX_FAILURE_THRESHOLD",
    "MAX_RECOVERY_TIMEOUT",
    "CircuitBreakerError",
    "CircuitBreakerRegistry",
    "ServiceCircuitBreaker",
    "circuit_breaker_decorator",
    "create_circuit_breaker",
    "handle_circuit_breaker_error",
    "with_circuit_breaker",
]


# =========================================================================
# CircuitBreakerRegistry — Singleton Registry
# =========================================================================


class CircuitBreakerRegistry:
    """Singleton registry tracking all active circuit breakers in the service.

    Provides a centralised view of every circuit breaker's state for
    monitoring, health-check endpoints, and manual resets.  All public
    methods are thread-safe, guarded by an internal reentrant lock so
    that concurrent Flask request threads can safely register, query,
    and reset breakers.

    Attributes:
        _instance: Singleton instance (class-level).
        _lock: Class-level lock for thread-safe singleton initialisation.

    Usage::

        registry = CircuitBreakerRegistry.get_instance()
        status   = registry.get_status()
        registry.reset("auth0_api")
    """

    _instance: Optional["CircuitBreakerRegistry"] = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        """Initialise with an empty breaker map and its own access lock."""
        self._breakers: dict[str, CircuitBreaker] = {}
        self._breakers_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Singleton accessor
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "CircuitBreakerRegistry":
        """Return the singleton *CircuitBreakerRegistry* instance.

        Uses double-checked locking for thread-safe lazy initialisation.

        Returns:
            The singleton registry instance.
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Registration & retrieval
    # ------------------------------------------------------------------

    def register(self, name: str, breaker: CircuitBreaker) -> None:
        """Register a circuit breaker under *name*.

        If a breaker with the same name already exists it is silently
        replaced, allowing reconfiguration at runtime.

        Args:
            name: Unique identifier for the circuit breaker.
            breaker: The ``CircuitBreaker`` instance to register.
        """
        with self._breakers_lock:
            self._breakers[name] = breaker
        logger.debug("circuit_breaker_registered", name=name)

    def get(self, name: str) -> CircuitBreaker | None:
        """Return the circuit breaker registered under *name*, or ``None``.

        Args:
            name: Lookup key.

        Returns:
            The ``CircuitBreaker`` instance if found, ``None`` otherwise.
        """
        with self._breakers_lock:
            return self._breakers.get(name)

    def get_all(self) -> dict[str, CircuitBreaker]:
        """Return a shallow copy of all registered circuit breakers.

        Returns:
            Mapping of breaker names to ``CircuitBreaker`` instances.
        """
        with self._breakers_lock:
            return dict(self._breakers)

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, dict]:
        """Return an aggregated status snapshot of every registered breaker.

        Returns:
            A dictionary keyed by breaker name, each value being a dict
            with keys ``state``, ``failure_count``, and ``last_failure``.

        Example::

            {
                "auth0_api": {
                    "state": "closed",
                    "failure_count": 0,
                    "last_failure": None,
                },
                "erp_connector": {
                    "state": "open",
                    "failure_count": 5,
                    "last_failure": "ConnectionError: timed out",
                },
            }
        """
        with self._breakers_lock:
            status: dict[str, dict] = {}
            for name, breaker in self._breakers.items():
                last_failure = breaker.last_failure
                status[name] = {
                    "state": breaker.state,
                    "failure_count": breaker.failure_count,
                    "last_failure": (
                        str(last_failure) if last_failure is not None else None
                    ),
                }
            return status

    # ------------------------------------------------------------------
    # Reset operations
    # ------------------------------------------------------------------

    def reset(self, name: str) -> bool:
        """Reset a specific circuit breaker to the **CLOSED** state.

        Args:
            name: Name of the circuit breaker to reset.

        Returns:
            ``True`` if the breaker was found and reset, ``False`` if
            no breaker with that name is registered.
        """
        with self._breakers_lock:
            breaker = self._breakers.get(name)
            if breaker is None:
                logger.warning(
                    "circuit_breaker_reset_not_found",
                    name=name,
                    reason="not_found",
                )
                return False
            breaker.reset()
        logger.info("circuit_breaker_manually_reset", name=name)
        return True

    def reset_all(self) -> None:
        """Reset **all** registered circuit breakers to the CLOSED state.

        Iterates through every breaker and calls ``reset()``, restoring
        normal operation for all protected service calls.
        """
        with self._breakers_lock:
            count = len(self._breakers)
            for breaker in self._breakers.values():
                breaker.reset()
        logger.info("all_circuit_breakers_reset", count=count)


# =========================================================================
# ServiceCircuitBreaker — Extended CircuitBreaker
# =========================================================================


class ServiceCircuitBreaker(CircuitBreaker):
    """Extended circuit breaker with structured logging and registry integration.

    Builds on the ``circuitbreaker.CircuitBreaker`` base class to add:

    * **Automatic registration** in :class:`CircuitBreakerRegistry`.
    * **Structured logging** on every state transition
      (CLOSED → OPEN → HALF_OPEN → CLOSED).
    * **Parameter validation** clamping thresholds within allowed bounds.
    * A human-readable :attr:`state_name` property.

    Args:
        name: Unique identifier for this circuit breaker.
        failure_threshold: Consecutive failures before opening (1-10).
        recovery_timeout: Seconds before attempting recovery (1-120).
        expected_exception: Exception type(s) that count as failures.

    Example::

        breaker = ServiceCircuitBreaker(
            name="auth0_api",
            failure_threshold=3,
            recovery_timeout=60,
        )
        result = breaker.call(make_http_request, url, headers=headers)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: int = DEFAULT_RECOVERY_TIMEOUT,
        expected_exception: type = DEFAULT_EXPECTED_EXCEPTION,
    ) -> None:
        """Initialise with validation, base-class init, and registry registration."""
        # Validate and clamp thresholds within allowed bounds
        validated_threshold = max(1, min(int(failure_threshold), MAX_FAILURE_THRESHOLD))
        validated_timeout = max(1, min(int(recovery_timeout), MAX_RECOVERY_TIMEOUT))

        if validated_threshold != failure_threshold:
            logger.warning(
                "circuit_breaker_threshold_clamped",
                name=name,
                requested=failure_threshold,
                applied=validated_threshold,
                bounds=f"1-{MAX_FAILURE_THRESHOLD}",
            )

        if validated_timeout != recovery_timeout:
            logger.warning(
                "circuit_breaker_timeout_clamped",
                name=name,
                requested=recovery_timeout,
                applied=validated_timeout,
                bounds=f"1-{MAX_RECOVERY_TIMEOUT}",
            )

        # Initialise the base CircuitBreaker
        super().__init__(
            failure_threshold=validated_threshold,
            recovery_timeout=validated_timeout,
            expected_exception=expected_exception,
            name=name,
        )

        # Keep a reference for structured log context
        self._service_name: str = name

        # Auto-register in the centralized registry
        CircuitBreakerRegistry.get_instance().register(name, self)

    # ------------------------------------------------------------------
    # Context manager override for state-transition logging
    # ------------------------------------------------------------------

    def __exit__(
        self,
        exc_type: type | None,
        exc_value: BaseException | None,
        _traceback: Any,
    ) -> bool:
        """Intercept context-manager exit to log state transitions.

        Captures the effective state before the base class processes the
        call outcome (success / failure) and compares it with the state
        afterwards.  Any difference triggers a structured log event at
        the appropriate severity level.
        """
        state_before = self.state
        result: bool = super().__exit__(exc_type, exc_value, _traceback)
        state_after = self.state
        if state_before != state_after:
            self._log_state_transition(state_before, state_after)
        return result

    # ------------------------------------------------------------------
    # Call override with open-state guard
    # ------------------------------------------------------------------

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute *func* through the circuit breaker with an open-state guard.

        The base ``CircuitBreaker.call()`` delegates to the context manager
        without checking the open state.  This override ensures that calls
        made directly (not via the decorator wrapper) are properly rejected
        when the circuit is OPEN.

        Args:
            func: The callable to protect.
            *args: Positional arguments forwarded to *func*.
            **kwargs: Keyword arguments forwarded to *func*.

        Returns:
            The return value of ``func(*args, **kwargs)``.

        Raises:
            CircuitBreakerError: If the circuit is currently OPEN.
        """
        if self.opened:
            logger.warning(
                "circuit_breaker_call_rejected",
                name=self._service_name,
                state=self.state,
                failure_count=self.failure_count,
            )
            raise CircuitBreakerError(self)
        return super().call(func, *args, **kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_state_transition(self, old_state: str, new_state: str) -> None:
        """Emit a structured log event for a circuit breaker state change.

        Uses WARNING level when the circuit opens (service degradation)
        and INFO level for recovery-related transitions.

        Args:
            old_state: The state before the transition.
            new_state: The state after the transition.
        """
        if new_state == STATE_OPEN:
            logger.warning(
                "Circuit breaker opened",
                name=self._service_name,
                failures=self.failure_count,
                old_state=old_state,
                new_state=new_state,
            )
        elif new_state == STATE_HALF_OPEN:
            logger.info(
                "Circuit breaker half-open, testing",
                name=self._service_name,
                old_state=old_state,
                new_state=new_state,
            )
        elif new_state == STATE_CLOSED:
            logger.info(
                "Circuit breaker closed, recovered",
                name=self._service_name,
                old_state=old_state,
                new_state=new_state,
            )

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def state_name(self) -> str:
        """Return a human-readable label for the current breaker state.

        Returns:
            One of ``"closed"``, ``"open"``, or ``"half_open"``.
        """
        state = self.state
        state_names: dict[str, str] = {
            STATE_CLOSED: "closed",
            STATE_OPEN: "open",
            STATE_HALF_OPEN: "half_open",
        }
        return state_names.get(state, str(state))


# =========================================================================
# Factory Function
# =========================================================================


def create_circuit_breaker(
    name: str,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    recovery_timeout: int = DEFAULT_RECOVERY_TIMEOUT,
    expected_exception: type = DEFAULT_EXPECTED_EXCEPTION,
) -> ServiceCircuitBreaker:
    """Create and return a fully configured :class:`ServiceCircuitBreaker`.

    This is the recommended entry-point for creating circuit breakers
    programmatically.  The returned instance is automatically registered
    in the :class:`CircuitBreakerRegistry`.

    Args:
        name: Unique identifier for the protected service or dependency.
        failure_threshold: Consecutive failures before opening the circuit.
            Clamped to ``[1, MAX_FAILURE_THRESHOLD]``.
        recovery_timeout: Seconds before attempting a test call in
            half-open state.  Clamped to ``[1, MAX_RECOVERY_TIMEOUT]``.
        expected_exception: Exception type(s) that count as failures.

    Returns:
        A configured :class:`ServiceCircuitBreaker` instance.

    Raises:
        ValueError: If *name* is empty or parameters are not numeric.

    Example::

        breaker = create_circuit_breaker(
            name="auth0_api",
            failure_threshold=3,
            recovery_timeout=60,
            expected_exception=ConnectionError,
        )
    """
    if not name or not isinstance(name, str):
        raise ValueError("Circuit breaker name must be a non-empty string")

    if not isinstance(failure_threshold, (int, float)):
        raise ValueError(
            f"failure_threshold must be numeric, got {type(failure_threshold).__name__}"
        )
    if not isinstance(recovery_timeout, (int, float)):
        raise ValueError(
            f"recovery_timeout must be numeric, got {type(recovery_timeout).__name__}"
        )

    breaker = ServiceCircuitBreaker(
        name=name,
        failure_threshold=int(failure_threshold),
        recovery_timeout=int(recovery_timeout),
        expected_exception=expected_exception,
    )

    logger.debug(
        "circuit_breaker_created",
        name=name,
        failure_threshold=breaker._failure_threshold,
        recovery_timeout=breaker._recovery_timeout,
    )

    return breaker


# =========================================================================
# Decorator Factory
# =========================================================================


def circuit_breaker_decorator(
    name: str,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    recovery_timeout: int = DEFAULT_RECOVERY_TIMEOUT,
    expected_exception: type = DEFAULT_EXPECTED_EXCEPTION,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
) -> Callable[[F], F]:
    """Decorator factory combining circuit breaker with exponential backoff retry.

    Produces a decorator that protects a function with both the circuit
    breaker pattern **and** retry logic:

    1. If the circuit is **OPEN**, raises :class:`CircuitBreakerError`
       immediately — no retries are attempted.
    2. If the function raises *expected_exception*, it is retried up to
       *max_retries* times with exponential backoff
       (``delay = backoff_base ** attempt``).
    3. On the final retry failure the exception propagates naturally;
       the circuit breaker tracks it as a failure.

    Args:
        name: Unique name for the circuit breaker.
        failure_threshold: Failures before opening (1-10).
        recovery_timeout: Seconds before half-open attempt (1-120).
        expected_exception: Exception type(s) that count as failures.
        max_retries: Maximum additional retry attempts (0 = no retries,
            i.e. a single attempt).
        backoff_base: Base for exponential delay (default 2.0).
            Delays: 2⁰ = 1 s, 2¹ = 2 s, 2² = 4 s, …

    Returns:
        A decorator that wraps the target function.

    Example::

        @circuit_breaker_decorator(
            name="auth0_api",
            failure_threshold=3,
            recovery_timeout=60,
        )
        def call_auth0(token: str) -> dict:
            resp = requests.get(
                "https://auth0.example.com/userinfo",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()
    """

    def decorator(func: F) -> F:
        # Retrieve an existing breaker or create a new one
        registry = CircuitBreakerRegistry.get_instance()
        breaker = registry.get(name)
        if breaker is None:
            breaker = create_circuit_breaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                expected_exception=expected_exception,
            )

        effective_max_retries = max(0, int(max_retries))

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(effective_max_retries + 1):
                try:
                    return breaker.call(func, *args, **kwargs)

                except CircuitBreakerError:
                    # Circuit is OPEN — do NOT retry; propagate immediately
                    logger.warning(
                        "circuit_breaker_call_blocked",
                        name=name,
                        attempt=attempt + 1,
                        reason="circuit_open",
                    )
                    raise

                except expected_exception as exc:
                    last_exception = exc

                    if attempt < effective_max_retries:
                        # Exponential backoff: delay = base ** attempt
                        delay: float = backoff_base ** attempt
                        logger.info(
                            "circuit_breaker_retry",
                            name=name,
                            attempt=attempt + 1,
                            max_retries=effective_max_retries,
                            delay_seconds=delay,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                        time.sleep(delay)
                    else:
                        # All retries exhausted — let the exception propagate
                        logger.warning(
                            "circuit_breaker_retries_exhausted",
                            name=name,
                            total_attempts=attempt + 1,
                            max_retries=effective_max_retries,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                        raise

            # Safety net (should not be reached in normal flow)
            if last_exception is not None:  # pragma: no cover
                raise last_exception

        return wrapper  # type: ignore[return-value]

    return decorator


# =========================================================================
# Functional (non-decorator) API
# =========================================================================


def with_circuit_breaker(
    name: str,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute *func* through a named circuit breaker (functional API).

    Retrieves (or lazily creates) the circuit breaker registered under
    *name* and routes the call through it.  Useful for one-off or
    dynamically determined calls where a decorator is not practical.

    Args:
        name: Name of the circuit breaker to use.
        func: The callable to protect.
        *args: Positional arguments forwarded to *func*.
        **kwargs: Keyword arguments forwarded to *func*.

    Returns:
        The return value of ``func(*args, **kwargs)``.

    Raises:
        CircuitBreakerError: If the circuit is currently OPEN.
        Exception: Any exception raised by *func* matching the breaker's
            configured expected exception.

    Example::

        result = with_circuit_breaker(
            "database_query",
            execute_query,
            query="SELECT * FROM users",
            timeout=30,
        )
    """
    registry = CircuitBreakerRegistry.get_instance()
    breaker = registry.get(name)
    if breaker is None:
        breaker = create_circuit_breaker(name=name)
    return breaker.call(func, *args, **kwargs)


# =========================================================================
# Error Response Utility
# =========================================================================


def handle_circuit_breaker_error(
    error: CircuitBreakerError,
    service_name: str,
) -> dict[str, Any]:
    """Build a standardised error response when a circuit breaker is open.

    Creates a dictionary suitable for returning as an HTTP **503 Service
    Unavailable** response.  Also emits a structured error-level log
    event with full context.

    Args:
        error: The :class:`CircuitBreakerError` that was raised.
        service_name: Human-readable name of the unavailable service.

    Returns:
        A dictionary containing:

        - ``error_type`` — ``"CircuitBreakerError"``
        - ``message`` — Human-readable description
        - ``service_name`` — The affected service
        - ``retry_after`` — Seconds until the circuit may attempt recovery
        - ``status_code`` — ``503``

    Example::

        try:
            result = call_external_service()
        except CircuitBreakerError as e:
            resp = handle_circuit_breaker_error(e, "auth0")
            return jsonify(resp), resp["status_code"]
    """
    # Extract recovery timeout from the breaker attached to the error
    recovery_timeout: int = DEFAULT_RECOVERY_TIMEOUT
    circuit_breaker_ref = getattr(error, "_circuit_breaker", None)
    if circuit_breaker_ref is not None:
        recovery_timeout = int(
            getattr(circuit_breaker_ref, "_recovery_timeout", DEFAULT_RECOVERY_TIMEOUT)
        )

    # Safely convert the error to string — the circuitbreaker library's
    # __str__ accesses _circuit_breaker.name which may be None if the
    # error was constructed manually (e.g., during testing).
    try:
        error_message = str(error)
    except (AttributeError, TypeError):
        error_message = repr(error)

    logger.error(
        "circuit_breaker_service_unavailable",
        service_name=service_name,
        error=error_message,
        recovery_timeout=recovery_timeout,
    )

    return {
        "error_type": "CircuitBreakerError",
        "message": (
            f"Service '{service_name}' is temporarily unavailable. "
            f"The circuit breaker has been activated due to repeated failures. "
            f"Please retry after {recovery_timeout} seconds."
        ),
        "service_name": service_name,
        "retry_after": recovery_timeout,
        "status_code": 503,
    }
