"""Weighted composite quality scorer for the Synthetic ERP Data Generation Platform.

Orchestrates all three validation strategies (statistical fidelity 40 %, business
rules 30 %, referential integrity 30 %) and computes the overall quality score
using the formula::

    Q = 0.4 * S_statistical + 0.3 * S_business_rules + 0.3 * S_referential_integrity

Acts as the central orchestration point for the quality validation pipeline:

1. Receives generated data and source profile metadata.
2. Runs all registered validators (Strategy pattern).
3. Aggregates individual scores into a composite quality score.
4. Determines pass/fail against the ≥ 95 % threshold.
5. Optionally generates a :class:`QualityReport` via :class:`ReportGenerator`.
6. Caches results in Redis and persists them to MongoDB.

Usage::

    from quality_service.scoring.quality_scorer import QualityScorer

    scorer = QualityScorer()
    result = scorer.score(
        job_id="abc-123",
        tenant_id="tenant-1",
        generated_data=df,
        profile=profile_dict,
    )
    print(result.composite_score, result.passed)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional, Union

import pandas as pd
from pydantic import BaseModel, Field

from quality_service.validators.base import BaseValidator, ValidationResult
from quality_service.scoring.report_generator import (
    QualityReport,
    ReportGenerator,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic result model
# ---------------------------------------------------------------------------


class QualityScoringResult(BaseModel):
    """Result of a composite quality scoring operation.

    Encapsulates the weighted composite score, pass/fail status, individual
    validator results, timing metadata, and the weights used for aggregation.

    Attributes:
        job_id: Generation job identifier that was scored.
        tenant_id: Tenant namespace for multi-tenant isolation.
        composite_score: Final weighted quality score in [0.0, 1.0].
        passed: ``True`` when ``composite_score >= threshold``.
        threshold: Minimum acceptable composite score (default 0.95).
        validation_results: Individual validator result dictionaries.
        weights: Map of ``validator_name`` → ``weight`` used in scoring.
        scored_at: ISO 8601 timestamp of when scoring completed.
        execution_time_ms: Total wall-clock scoring pipeline time (ms).
        metadata: Additional context (record count, schema, generation method).
    """

    model_config = {"arbitrary_types_allowed": True}

    job_id: str = Field(..., description="Generation job ID being scored.")
    tenant_id: str = Field(..., description="Tenant namespace.")
    composite_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Final weighted quality score in [0.0, 1.0].",
    )
    passed: bool = Field(
        default=False,
        description="Whether composite_score >= threshold.",
    )
    threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Minimum acceptable score.",
    )
    validation_results: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Individual validator results (serialised).",
    )
    weights: dict[str, float] = Field(
        default_factory=dict,
        description="Map of validator_name -> weight.",
    )
    scored_at: str = Field(
        default="",
        description="ISO 8601 timestamp of scoring completion.",
    )
    execution_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Total scoring pipeline time in milliseconds.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context (record count, schema, method).",
    )


# ---------------------------------------------------------------------------
# QualityScorer — central orchestration class
# ---------------------------------------------------------------------------


class QualityScorer:
    """Central quality scoring orchestrator for the Quality Service.

    Manages the complete scoring lifecycle:

    1. Initialises all registered validators via the Strategy pattern.
    2. Executes each validator against generated data.
    3. Computes the weighted composite quality score.
    4. Determines pass/fail against the configurable threshold.
    5. Caches results in Redis and persists them to MongoDB.

    Args:
        config: Optional dictionary of scoring configuration.  Recognised keys:

            - ``QUALITY_STATISTICAL_WEIGHT`` (float, default 0.4)
            - ``QUALITY_BUSINESS_RULES_WEIGHT`` (float, default 0.3)
            - ``QUALITY_REFERENTIAL_INTEGRITY_WEIGHT`` (float, default 0.3)
            - ``QUALITY_MIN_THRESHOLD`` (float, default 0.95)
            - ``SCORE_CACHE_TTL`` (int, default 3600) — Redis TTL in seconds.

    Raises:
        ValueError: If configured weights do not sum to 1.0 (within tolerance).
    """

    # Default scoring weights matching the specification formula.
    _DEFAULT_WEIGHTS = {
        "QUALITY_STATISTICAL_WEIGHT": 0.4,
        "QUALITY_BUSINESS_RULES_WEIGHT": 0.3,
        "QUALITY_REFERENTIAL_INTEGRITY_WEIGHT": 0.3,
    }
    _WEIGHT_TOLERANCE = 1e-6

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config if config is not None else {}
        self.logger = get_logger(self.__class__.__name__)

        # Load scoring weights.
        self.statistical_weight: float = float(
            self.config.get("QUALITY_STATISTICAL_WEIGHT", 0.4)
        )
        self.business_rules_weight: float = float(
            self.config.get("QUALITY_BUSINESS_RULES_WEIGHT", 0.3)
        )
        self.referential_integrity_weight: float = float(
            self.config.get("QUALITY_REFERENTIAL_INTEGRITY_WEIGHT", 0.3)
        )
        self.minimum_threshold: float = float(
            self.config.get("QUALITY_MIN_THRESHOLD", 0.95)
        )
        self._cache_ttl: int = int(
            self.config.get("SCORE_CACHE_TTL", 3600)
        )

        # Validate that weights sum to 1.0.
        self._validate_weights()

        # Initialise validators via the registry.
        self._validators: list[BaseValidator] = []
        try:
            from quality_service.validators import get_all_validators

            self._validators = get_all_validators(self.config or None)
            self.logger.info(
                "quality_scorer_validators_loaded",
                count=len(self._validators),
                validators=[v.get_name() for v in self._validators],
            )
        except Exception as exc:
            self.logger.warning(
                "quality_scorer_validators_load_failed",
                error=str(exc),
            )

        # Initialise report generator.
        self._report_generator = ReportGenerator(config=self.config)

        # Lazy-initialise external dependencies (MongoDB, Redis) so the
        # scorer can be constructed in environments where they are absent.
        self._db = None
        self._redis = None
        self._init_data_stores()

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _init_data_stores(self) -> None:
        """Lazily initialise MongoDB and Redis connections."""
        try:
            from shared.database.mongodb import get_mongo_db

            self._db = get_mongo_db()
        except Exception as exc:
            self.logger.warning(
                "quality_scorer_mongo_unavailable", error=str(exc)
            )

        try:
            from shared.database.redis_client import get_redis_client

            self._redis = get_redis_client()
        except Exception as exc:
            self.logger.warning(
                "quality_scorer_redis_unavailable", error=str(exc)
            )

    def _validate_weights(self) -> None:
        """Verify that the configured scoring weights sum to 1.0.

        Raises:
            ValueError: If weights do not sum to 1.0 within tolerance.
        """
        weight_sum = (
            self.statistical_weight
            + self.business_rules_weight
            + self.referential_integrity_weight
        )
        if abs(weight_sum - 1.0) > self._WEIGHT_TOLERANCE:
            raise ValueError(
                f"Quality scoring weights must sum to 1.0, got {weight_sum:.6f}. "
                f"(statistical={self.statistical_weight}, "
                f"business_rules={self.business_rules_weight}, "
                f"referential_integrity={self.referential_integrity_weight})"
            )

    # ------------------------------------------------------------------
    # Core scoring methods
    # ------------------------------------------------------------------

    def score(
        self,
        job_id: str,
        tenant_id: str,
        generated_data: Union[pd.DataFrame, dict[str, pd.DataFrame]],
        profile: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> QualityScoringResult:
        """Run all validators and compute the composite quality score.

        This is the primary orchestration method.  It iterates through all
        registered validators, collects their results, computes the weighted
        composite score, and persists the outcome.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for multi-tenant isolation.
            generated_data: A single ``pd.DataFrame`` or a mapping of
                ``table_name`` → ``pd.DataFrame`` for multi-table scoring.
            profile: Statistical/schema profile metadata from the Profiling
                Service.
            metadata: Optional additional context (generation method, schema
                version, record count, etc.).

        Returns:
            A :class:`QualityScoringResult` with the composite score,
            pass/fail status, and per-validator breakdowns.
        """
        self.logger.info(
            "quality_scoring_started",
            job_id=job_id,
            tenant_id=tenant_id,
            validators_count=len(self._validators),
        )

        start = time.perf_counter()
        all_results: list[ValidationResult] = []

        # Execute each validator.
        for validator in self._validators:
            try:
                result = validator.validate_with_timing(generated_data, profile)
                all_results.append(result)
            except Exception as exc:
                self.logger.error(
                    "validator_execution_error",
                    validator=validator.get_name(),
                    error=str(exc),
                    exc_info=True,
                )
                # Create a zero-score result so scoring degrades gracefully.
                all_results.append(
                    validator.create_result(
                        score=0.0,
                        details={"error": str(exc)},
                        errors=[f"Validator failed: {exc}"],
                    )
                )

        # Compute composite score.
        composite_score = self._compute_composite_score(all_results)
        passed = composite_score >= self.minimum_threshold

        elapsed_ms = (time.perf_counter() - start) * 1000.0

        # Build the weights map.
        weights_map = {r.validator_name: r.weight for r in all_results}

        # Serialise individual results.
        serialised_results = []
        for r in all_results:
            try:
                serialised_results.append(r.model_dump())
            except Exception:
                serialised_results.append(
                    {"validator_name": r.validator_name, "score": r.score}
                )

        scoring_result = QualityScoringResult(
            job_id=job_id,
            tenant_id=tenant_id,
            composite_score=composite_score,
            passed=passed,
            threshold=self.minimum_threshold,
            validation_results=serialised_results,
            weights=weights_map,
            scored_at=datetime.now(timezone.utc).isoformat(),
            execution_time_ms=round(elapsed_ms, 3),
            metadata=metadata if metadata is not None else {},
        )

        # Persist and cache.
        self._store_result(scoring_result)
        self._cache_result(scoring_result)

        self.logger.info(
            "quality_scoring_completed",
            job_id=job_id,
            tenant_id=tenant_id,
            composite_score=round(composite_score, 4),
            passed=passed,
            execution_time_ms=round(elapsed_ms, 3),
        )

        return scoring_result

    def score_and_report(
        self,
        job_id: str,
        tenant_id: str,
        generated_data: Union[pd.DataFrame, dict[str, pd.DataFrame]],
        profile: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> tuple[QualityScoringResult, QualityReport]:
        """Score generated data and produce a detailed quality report.

        Convenience method combining :meth:`score` and
        :meth:`ReportGenerator.generate_report` in a single call.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for multi-tenant isolation.
            generated_data: Single or multi-table generated data.
            profile: Statistical/schema profile metadata.
            metadata: Optional additional context.

        Returns:
            A tuple of ``(QualityScoringResult, QualityReport)``.
        """
        scoring_result = self.score(
            job_id=job_id,
            tenant_id=tenant_id,
            generated_data=generated_data,
            profile=profile,
            metadata=metadata,
        )

        # Reconstruct ValidationResult objects for the report generator.
        validation_results: list[ValidationResult] = []
        for raw in scoring_result.validation_results:
            try:
                validation_results.append(ValidationResult.model_validate(raw))
            except Exception:
                self.logger.warning(
                    "validation_result_reconstruction_failed",
                    raw_keys=list(raw.keys()) if isinstance(raw, dict) else "unknown",
                )

        report = self._report_generator.generate_report(
            job_id=job_id,
            tenant_id=tenant_id,
            validation_results=validation_results,
            composite_score=scoring_result.composite_score,
            threshold=scoring_result.threshold,
            metadata=metadata,
        )

        # Persist the report.
        try:
            self._report_generator.save_report(report)
        except Exception as exc:
            self.logger.warning(
                "quality_report_save_failed",
                report_id=report.report_id,
                error=str(exc),
            )

        return scoring_result, report

    # ------------------------------------------------------------------
    # Composite score computation
    # ------------------------------------------------------------------

    def _compute_composite_score(
        self, validation_results: list[ValidationResult]
    ) -> float:
        """Calculate the weighted composite quality score.

        Formula::

            Q = Σ(result.score * result.weight) for each result

        The result is clamped to [0.0, 1.0].

        Args:
            validation_results: Individual validator results.

        Returns:
            The composite quality score as a float in [0.0, 1.0].
        """
        if not validation_results:
            return 0.0

        composite = sum(r.score * r.weight for r in validation_results)

        # Clamp to valid range to guard against floating-point drift.
        return max(0.0, min(1.0, composite))

    # ------------------------------------------------------------------
    # Cache operations
    # ------------------------------------------------------------------

    def _cache_key(self, tenant_id: str, job_id: str) -> str:
        """Build the Redis cache key for a quality score."""
        return f"quality_score:{tenant_id}:{job_id}"

    def _cache_result(self, result: QualityScoringResult) -> None:
        """Cache a :class:`QualityScoringResult` in Redis with TTL.

        Args:
            result: The scoring result to cache.
        """
        if self._redis is None:
            return

        try:
            key = self._cache_key(result.tenant_id, result.job_id)
            payload = result.model_dump_json()
            self._redis.set(key, payload, ex=self._cache_ttl)
        except Exception as exc:
            self.logger.warning(
                "quality_score_cache_set_failed",
                job_id=result.job_id,
                error=str(exc),
            )

    def get_cached_score(
        self, job_id: str, tenant_id: str
    ) -> QualityScoringResult | None:
        """Retrieve a cached quality score from Redis.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for cache key scoping.

        Returns:
            The cached :class:`QualityScoringResult` or ``None`` on cache miss.
        """
        if self._redis is None:
            return None

        try:
            key = self._cache_key(tenant_id, job_id)
            raw = self._redis.get(key)
            if raw is None:
                return None
            data = json.loads(raw)
            return QualityScoringResult.model_validate(data)
        except Exception as exc:
            self.logger.warning(
                "quality_score_cache_get_failed",
                job_id=job_id,
                error=str(exc),
            )
            return None

    def invalidate_cache(self, job_id: str, tenant_id: str) -> bool:
        """Remove a cached quality score from Redis.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace.

        Returns:
            ``True`` if the key was deleted, ``False`` if not found.
        """
        if self._redis is None:
            return False

        try:
            key = self._cache_key(tenant_id, job_id)
            deleted = self._redis.delete(key)
            return bool(deleted)
        except Exception as exc:
            self.logger.warning(
                "quality_score_cache_invalidate_failed",
                job_id=job_id,
                error=str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # MongoDB persistence
    # ------------------------------------------------------------------

    def _store_result(self, result: QualityScoringResult) -> None:
        """Persist a :class:`QualityScoringResult` to MongoDB.

        Args:
            result: The scoring result to store.
        """
        if self._db is None:
            return

        try:
            collection = self._db["scoring_results"]
            doc = result.model_dump()
            collection.insert_one(doc)
        except Exception as exc:
            self.logger.warning(
                "quality_score_store_failed",
                job_id=result.job_id,
                error=str(exc),
            )

    def get_stored_score(
        self, job_id: str, tenant_id: str
    ) -> QualityScoringResult | None:
        """Retrieve a stored quality score from MongoDB.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for multi-tenant isolation.

        Returns:
            The stored :class:`QualityScoringResult` or ``None`` if not found.
        """
        if self._db is None:
            return None

        try:
            collection = self._db["scoring_results"]
            doc = collection.find_one(
                {"job_id": job_id, "tenant_id": tenant_id}
            )
            if doc is None:
                return None
            doc.pop("_id", None)
            return QualityScoringResult.model_validate(doc)
        except Exception as exc:
            self.logger.warning(
                "quality_score_retrieve_failed",
                job_id=job_id,
                error=str(exc),
            )
            return None

    def get_score_history(
        self, tenant_id: str, limit: int = 50
    ) -> list[QualityScoringResult]:
        """Retrieve historical quality scores for a tenant.

        Args:
            tenant_id: Tenant namespace.
            limit: Maximum number of results (default 50).

        Returns:
            List of :class:`QualityScoringResult` sorted by ``scored_at``
            descending (most recent first).
        """
        if self._db is None:
            return []

        try:
            collection = self._db["scoring_results"]
            cursor = (
                collection.find({"tenant_id": tenant_id})
                .sort("scored_at", -1)
                .limit(limit)
            )
            results: list[QualityScoringResult] = []
            for doc in cursor:
                doc.pop("_id", None)
                try:
                    results.append(QualityScoringResult.model_validate(doc))
                except Exception:
                    pass
            return results
        except Exception as exc:
            self.logger.warning(
                "quality_score_history_failed",
                tenant_id=tenant_id,
                error=str(exc),
            )
            return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "QualityScorer",
    "QualityScoringResult",
]
