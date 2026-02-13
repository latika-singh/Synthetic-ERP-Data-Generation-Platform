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

Without this file, the Quality Service has no way to compute composite quality
scores, and the Generation Engine cannot validate generated datasets before
compliance checking and provisioning.

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

from quality_service.scoring.report_generator import (
    QualityReport,
    ReportGenerator,
)
from quality_service.validators import get_all_validators
from quality_service.validators.base import BaseValidator, ValidationResult
from shared.database.mongodb import get_mongo_db
from shared.database.redis_client import get_redis_client
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
    This model is the primary output of :meth:`QualityScorer.score` and is
    serialised for persistence in MongoDB and Redis caching.

    Attributes:
        job_id: Generation job identifier that was scored.
        tenant_id: Tenant namespace for multi-tenant isolation.
        composite_score: Final weighted quality score in [0.0, 1.0].
        passed: ``True`` when ``composite_score >= threshold``.
        threshold: Minimum acceptable composite score (default 0.95).
        validation_results: Individual :class:`ValidationResult` objects from
            each validator, preserving full detail for drill-down reporting.
        weights: Map of ``validator_name`` → ``weight`` used in scoring.
        scored_at: UTC timestamp of when scoring completed.
        execution_time_ms: Total wall-clock scoring pipeline time (ms).
        metadata: Additional context (record count, schema, generation method).

    Example::

        result = QualityScoringResult(
            job_id="gen-abc-123",
            tenant_id="tenant-42",
            composite_score=0.963,
            passed=True,
            threshold=0.95,
            validation_results=[...],
            weights={"statistical_fidelity": 0.4, "business_rules": 0.3,
                     "referential_integrity": 0.3},
            scored_at=datetime.now(timezone.utc),
            execution_time_ms=1523.7,
            metadata={"record_count": 100000, "generation_method": "ai_ml"},
        )
    """

    model_config = {"arbitrary_types_allowed": True}

    job_id: str = Field(
        ...,
        description="Generation job ID being scored.",
    )
    tenant_id: str = Field(
        ...,
        description="Tenant namespace for multi-tenant isolation.",
    )
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
        description="Minimum acceptable score (default 0.95).",
    )
    validation_results: list[ValidationResult] = Field(
        default_factory=list,
        description="Individual validator results preserving full detail.",
    )
    weights: dict[str, float] = Field(
        default_factory=dict,
        description="Map of validator_name -> weight used in scoring.",
    )
    scored_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of scoring completion (ISO 8601).",
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

    The scorer integrates with the Generation Engine for post-generation
    quality checks, gating datasets through a quality validation pipeline
    before they proceed to compliance checking and provisioning.

    Args:
        config: Optional dictionary of scoring configuration.  Recognised keys:

            - ``QUALITY_STATISTICAL_WEIGHT`` (float, default 0.4)
            - ``QUALITY_BUSINESS_RULES_WEIGHT`` (float, default 0.3)
            - ``QUALITY_REFERENTIAL_INTEGRITY_WEIGHT`` (float, default 0.3)
            - ``QUALITY_MIN_THRESHOLD`` (float, default 0.95)
            - ``SCORE_CACHE_TTL`` (int, default 3600) — Redis TTL in seconds.

    Raises:
        ValueError: If configured weights do not sum to 1.0 (within tolerance).

    Example::

        scorer = QualityScorer(config={
            "QUALITY_STATISTICAL_WEIGHT": 0.4,
            "QUALITY_BUSINESS_RULES_WEIGHT": 0.3,
            "QUALITY_REFERENTIAL_INTEGRITY_WEIGHT": 0.3,
            "QUALITY_MIN_THRESHOLD": 0.95,
        })
        result = scorer.score("job-1", "tenant-1", generated_df, profile)
    """

    # Default scoring weights matching the specification formula.
    _DEFAULT_WEIGHTS: dict[str, float] = {
        "QUALITY_STATISTICAL_WEIGHT": 0.4,
        "QUALITY_BUSINESS_RULES_WEIGHT": 0.3,
        "QUALITY_REFERENTIAL_INTEGRITY_WEIGHT": 0.3,
    }
    _WEIGHT_TOLERANCE: float = 1e-6
    _DEFAULT_THRESHOLD: float = 0.95
    _DEFAULT_CACHE_TTL: int = 3600
    _SCORING_RESULTS_COLLECTION: str = "scoring_results"

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        self.config: dict[str, Any] = config if config is not None else {}
        self.logger = get_logger(self.__class__.__name__)

        # ---- Load scoring weights from config or defaults ----
        self.statistical_weight: float = float(
            self.config.get(
                "QUALITY_STATISTICAL_WEIGHT",
                self._DEFAULT_WEIGHTS["QUALITY_STATISTICAL_WEIGHT"],
            )
        )
        self.business_rules_weight: float = float(
            self.config.get(
                "QUALITY_BUSINESS_RULES_WEIGHT",
                self._DEFAULT_WEIGHTS["QUALITY_BUSINESS_RULES_WEIGHT"],
            )
        )
        self.referential_integrity_weight: float = float(
            self.config.get(
                "QUALITY_REFERENTIAL_INTEGRITY_WEIGHT",
                self._DEFAULT_WEIGHTS["QUALITY_REFERENTIAL_INTEGRITY_WEIGHT"],
            )
        )
        self.minimum_threshold: float = float(
            self.config.get("QUALITY_MIN_THRESHOLD", self._DEFAULT_THRESHOLD)
        )
        self._cache_ttl: int = int(
            self.config.get("SCORE_CACHE_TTL", self._DEFAULT_CACHE_TTL)
        )

        # ---- Validate weight invariant ----
        self._validate_weights()

        # ---- Initialise validators via the Strategy pattern registry ----
        self._validators: list[BaseValidator] = []
        try:
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
                error_type=type(exc).__name__,
            )

        # ---- Initialise report generator ----
        self._report_generator: ReportGenerator = ReportGenerator(
            config=self.config,
        )

        # ---- Lazy-initialise external data stores ----
        # MongoDB and Redis connections are established lazily so the scorer
        # can be constructed in environments where they are temporarily
        # unavailable (unit tests, CLI tools, startup race conditions).
        self._db = None
        self._redis = None
        self._init_data_stores()

        self.logger.info(
            "quality_scorer_initialized",
            statistical_weight=self.statistical_weight,
            business_rules_weight=self.business_rules_weight,
            referential_integrity_weight=self.referential_integrity_weight,
            minimum_threshold=self.minimum_threshold,
            cache_ttl=self._cache_ttl,
            validators_count=len(self._validators),
            mongo_available=self._db is not None,
            redis_available=self._redis is not None,
        )

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _init_data_stores(self) -> None:
        """Lazily initialise MongoDB and Redis connections.

        Catches connection errors gracefully so that the scorer can still
        operate (without persistence/caching) when infrastructure services
        are temporarily unavailable.
        """
        try:
            self._db = get_mongo_db()
            self.logger.debug("quality_scorer_mongo_connected")
        except Exception as exc:
            self.logger.warning(
                "quality_scorer_mongo_unavailable",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        try:
            self._redis = get_redis_client()
            self.logger.debug("quality_scorer_redis_connected")
        except Exception as exc:
            self.logger.warning(
                "quality_scorer_redis_unavailable",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _validate_weights(self) -> None:
        """Verify that the configured scoring weights sum to 1.0.

        Checks that ``statistical_weight + business_rules_weight +
        referential_integrity_weight == 1.0`` within a floating-point
        tolerance of ±1e-6.  Logs a warning if the sum is exactly 1.0
        but close to the boundary of the tolerance window.

        Raises:
            ValueError: If weights do not sum to 1.0 within tolerance.
        """
        weight_sum: float = (
            self.statistical_weight
            + self.business_rules_weight
            + self.referential_integrity_weight
        )
        deviation: float = abs(weight_sum - 1.0)

        if deviation > self._WEIGHT_TOLERANCE:
            raise ValueError(
                f"Quality scoring weights must sum to 1.0, got {weight_sum:.6f}. "
                f"(statistical={self.statistical_weight}, "
                f"business_rules={self.business_rules_weight}, "
                f"referential_integrity={self.referential_integrity_weight})"
            )

        # Log a warning when the sum is technically within tolerance but
        # approaching the boundary — indicates potential configuration drift.
        boundary_warning_threshold: float = self._WEIGHT_TOLERANCE * 0.5
        if deviation > boundary_warning_threshold:
            self.logger.warning(
                "quality_scorer_weights_near_boundary",
                weight_sum=weight_sum,
                deviation=deviation,
                tolerance=self._WEIGHT_TOLERANCE,
                statistical=self.statistical_weight,
                business_rules=self.business_rules_weight,
                referential_integrity=self.referential_integrity_weight,
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
        metadata: Optional[dict[str, Any]] = None,
    ) -> QualityScoringResult:
        """Run all validators and compute the composite quality score.

        This is the primary orchestration method.  It iterates through every
        registered validator, collects their individual
        :class:`ValidationResult` objects, computes the weighted composite
        score using the formula
        ``Q = 0.4·S_stat + 0.3·S_biz + 0.3·S_ref``, and determines
        pass/fail against the configured threshold (default ≥ 95 %).

        Results are cached in Redis (with configurable TTL) and persisted to
        MongoDB for historical retrieval.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for multi-tenant isolation.
            generated_data: A single :class:`pd.DataFrame` for single-table
                scoring, or a ``dict[str, pd.DataFrame]`` mapping table names
                to DataFrames for multi-table scoring.
            profile: Statistical and schema profile metadata from the
                Profiling Service.
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

        start: float = time.perf_counter()
        all_results: list[ValidationResult] = []

        # ---- Execute each validator using the Strategy pattern ----
        for validator in self._validators:
            try:
                result: ValidationResult = validator.validate_with_timing(
                    generated_data, profile,
                )
                all_results.append(result)
                self.logger.debug(
                    "validator_result_collected",
                    job_id=job_id,
                    validator=result.validator_name,
                    score=round(result.score, 4),
                    weighted_score=round(result.weighted_score, 4),
                    passed=result.passed,
                )
            except Exception as exc:
                self.logger.error(
                    "validator_execution_error",
                    job_id=job_id,
                    validator=validator.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                # Create a zero-score result so scoring degrades gracefully
                # rather than aborting the entire pipeline.
                all_results.append(
                    validator.create_result(
                        score=0.0,
                        details={
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                        errors=[f"Validator failed: {exc}"],
                    )
                )

        # ---- Compute composite score ----
        composite_score: float = self._compute_composite_score(all_results)
        passed: bool = composite_score >= self.minimum_threshold

        elapsed_ms: float = (time.perf_counter() - start) * 1000.0

        # ---- Build the weights map ----
        weights_map: dict[str, float] = {
            r.validator_name: r.weight for r in all_results
        }

        # ---- Construct the scoring result ----
        scoring_result = QualityScoringResult(
            job_id=job_id,
            tenant_id=tenant_id,
            composite_score=composite_score,
            passed=passed,
            threshold=self.minimum_threshold,
            validation_results=all_results,
            weights=weights_map,
            scored_at=datetime.now(timezone.utc),
            execution_time_ms=round(elapsed_ms, 3),
            metadata=metadata if metadata is not None else {},
        )

        # ---- Persist and cache ----
        self._store_result(scoring_result)
        self._cache_result(scoring_result)

        self.logger.info(
            "quality_scoring_completed",
            job_id=job_id,
            tenant_id=tenant_id,
            composite_score=round(composite_score, 4),
            passed=passed,
            threshold=self.minimum_threshold,
            validators_executed=len(all_results),
            execution_time_ms=round(elapsed_ms, 3),
        )

        return scoring_result

    def score_and_report(
        self,
        job_id: str,
        tenant_id: str,
        generated_data: Union[pd.DataFrame, dict[str, pd.DataFrame]],
        profile: dict[str, Any],
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[QualityScoringResult, QualityReport]:
        """Score generated data and produce a detailed quality report.

        Convenience method that combines :meth:`score` and
        :meth:`ReportGenerator.generate_report` in a single call, then
        persists the report to MongoDB.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for multi-tenant isolation.
            generated_data: Single or multi-table generated data.
            profile: Statistical/schema profile metadata.
            metadata: Optional additional context.

        Returns:
            A tuple of ``(QualityScoringResult, QualityReport)``.
        """
        # ---- Step 1: Score ----
        scoring_result: QualityScoringResult = self.score(
            job_id=job_id,
            tenant_id=tenant_id,
            generated_data=generated_data,
            profile=profile,
            metadata=metadata,
        )

        # ---- Step 2: Generate report from the ValidationResult objects ----
        # The scoring_result.validation_results field contains the original
        # ValidationResult objects, so no reconstruction is needed.
        report: QualityReport = self._report_generator.generate_report(
            job_id=job_id,
            tenant_id=tenant_id,
            validation_results=scoring_result.validation_results,
            composite_score=scoring_result.composite_score,
            threshold=scoring_result.threshold,
            metadata=metadata,
        )

        # ---- Step 3: Persist the report ----
        try:
            self._report_generator.save_report(report)
            self.logger.info(
                "quality_report_persisted",
                report_id=report.report_id,
                job_id=report.job_id,
                overall_score=round(report.overall_score, 4),
                status=report.status,
            )
        except Exception as exc:
            self.logger.warning(
                "quality_report_save_failed",
                report_id=report.report_id,
                job_id=job_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        return scoring_result, report

    # ------------------------------------------------------------------
    # Composite score computation
    # ------------------------------------------------------------------

    def _compute_composite_score(
        self, validation_results: list[ValidationResult],
    ) -> float:
        """Calculate the weighted composite quality score.

        Implements the quality scoring formula::

            Q = Σ(result.score × result.weight)  for each result

        Validates that the sum of weights used equals 1.0 and clamps the
        final result to the [0.0, 1.0] range to guard against
        floating-point drift.

        Args:
            validation_results: Individual validator results carrying both
                ``score`` and ``weight`` fields.

        Returns:
            The composite quality score as a float in [0.0, 1.0].
        """
        if not validation_results:
            self.logger.warning("composite_score_no_results")
            return 0.0

        # Verify the weight invariant across the actual results used.
        weight_sum: float = sum(r.weight for r in validation_results)
        if abs(weight_sum - 1.0) > self._WEIGHT_TOLERANCE:
            self.logger.warning(
                "composite_score_weight_mismatch",
                expected=1.0,
                actual=weight_sum,
                validators=[r.validator_name for r in validation_results],
            )

        # Compute the weighted sum: Q = Σ(score_i × weight_i)
        composite: float = sum(
            r.score * r.weight for r in validation_results
        )

        # Clamp to valid range to protect against floating-point accumulation.
        return max(0.0, min(1.0, composite))

    # ------------------------------------------------------------------
    # Cache operations
    # ------------------------------------------------------------------

    def _cache_key(self, tenant_id: str, job_id: str) -> str:
        """Build the Redis cache key for a quality score.

        Key pattern: ``quality_score:{tenant_id}:{job_id}``

        Args:
            tenant_id: Tenant namespace for scoping.
            job_id: Generation job identifier.

        Returns:
            The formatted Redis key string.
        """
        return f"quality_score:{tenant_id}:{job_id}"

    def _cache_result(self, result: QualityScoringResult) -> None:
        """Cache a :class:`QualityScoringResult` in Redis with configurable TTL.

        Serialises the Pydantic model to a JSON string via
        :meth:`model_dump_json` and stores it under the standard key
        pattern ``quality_score:{tenant_id}:{job_id}`` with the configured
        TTL (default 3600 seconds / 1 hour).

        Args:
            result: The scoring result to cache.
        """
        if self._redis is None:
            return

        try:
            key: str = self._cache_key(result.tenant_id, result.job_id)
            # Serialize using Pydantic's JSON serializer which handles
            # datetime → ISO string and nested ValidationResult models.
            payload: str = result.model_dump_json()
            self._redis.set(key, payload, ex=self._cache_ttl)
            self.logger.debug(
                "quality_score_cached",
                job_id=result.job_id,
                tenant_id=result.tenant_id,
                ttl=self._cache_ttl,
            )
        except Exception as exc:
            self.logger.warning(
                "quality_score_cache_set_failed",
                job_id=result.job_id,
                tenant_id=result.tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def get_cached_score(
        self, job_id: str, tenant_id: str,
    ) -> Optional[QualityScoringResult]:
        """Retrieve a cached quality score from Redis.

        Looks up the quality score keyed by
        ``quality_score:{tenant_id}:{job_id}`` and deserialises the JSON
        payload back into a :class:`QualityScoringResult`.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for cache key scoping.

        Returns:
            The cached :class:`QualityScoringResult` or ``None`` on cache miss.
        """
        if self._redis is None:
            return None

        try:
            key: str = self._cache_key(tenant_id, job_id)
            raw: Optional[str] = self._redis.get(key)
            if raw is None:
                self.logger.debug(
                    "quality_score_cache_miss",
                    job_id=job_id,
                    tenant_id=tenant_id,
                )
                return None

            data: dict[str, Any] = json.loads(raw)
            result = QualityScoringResult.model_validate(data)
            self.logger.debug(
                "quality_score_cache_hit",
                job_id=job_id,
                tenant_id=tenant_id,
                composite_score=round(result.composite_score, 4),
            )
            return result
        except Exception as exc:
            self.logger.warning(
                "quality_score_cache_get_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    def invalidate_cache(self, job_id: str, tenant_id: str) -> bool:
        """Remove a cached quality score from Redis.

        Used when re-scoring a job to ensure stale cached results are
        cleared before the new score is computed.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace.

        Returns:
            ``True`` if the key was deleted, ``False`` if not found or Redis
            is unavailable.
        """
        if self._redis is None:
            return False

        try:
            key: str = self._cache_key(tenant_id, job_id)
            deleted: int = self._redis.delete(key)
            was_deleted: bool = bool(deleted)
            self.logger.info(
                "quality_score_cache_invalidated" if was_deleted
                else "quality_score_cache_invalidate_miss",
                job_id=job_id,
                tenant_id=tenant_id,
                key_deleted=was_deleted,
            )
            return was_deleted
        except Exception as exc:
            self.logger.warning(
                "quality_score_cache_invalidate_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

    # ------------------------------------------------------------------
    # MongoDB persistence
    # ------------------------------------------------------------------

    def _store_result(self, result: QualityScoringResult) -> None:
        """Persist a :class:`QualityScoringResult` to MongoDB.

        Serialises the Pydantic model to a plain dictionary via
        :meth:`model_dump` and inserts it into the ``scoring_results``
        collection, indexed by ``tenant_id`` for multi-tenant query
        isolation.

        Args:
            result: The scoring result to persist.
        """
        if self._db is None:
            return

        try:
            collection = self._db[self._SCORING_RESULTS_COLLECTION]
            doc: dict[str, Any] = result.model_dump()

            # Ensure datetime fields are ISO strings for consistent MongoDB
            # serialisation across different PyMongo configurations.
            if isinstance(doc.get("scored_at"), datetime):
                doc["scored_at"] = doc["scored_at"].isoformat()

            # Serialise nested ValidationResult models to plain dicts.
            # Pydantic's model_dump() handles this recursively, but we
            # guarantee dict form for any model instances that survived.
            serialised_results: list[dict[str, Any]] = []
            for vr in doc.get("validation_results", []):
                if isinstance(vr, dict):
                    serialised_results.append(vr)
                elif hasattr(vr, "model_dump"):
                    serialised_results.append(vr.model_dump())
                else:
                    serialised_results.append(dict(vr))
            doc["validation_results"] = serialised_results

            collection.insert_one(doc)
            self.logger.debug(
                "quality_score_stored",
                job_id=result.job_id,
                tenant_id=result.tenant_id,
            )
        except Exception as exc:
            self.logger.warning(
                "quality_score_store_failed",
                job_id=result.job_id,
                tenant_id=result.tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def get_stored_score(
        self, job_id: str, tenant_id: str,
    ) -> Optional[QualityScoringResult]:
        """Retrieve a stored quality score from MongoDB.

        Queries the ``scoring_results`` collection with both ``job_id`` and
        ``tenant_id`` to enforce multi-tenant isolation.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for multi-tenant isolation.

        Returns:
            The stored :class:`QualityScoringResult` or ``None`` if not found
            or MongoDB is unavailable.
        """
        if self._db is None:
            return None

        try:
            collection = self._db[self._SCORING_RESULTS_COLLECTION]
            doc: Optional[dict[str, Any]] = collection.find_one(
                {"job_id": job_id, "tenant_id": tenant_id},
            )
            if doc is None:
                return None

            # Remove the MongoDB internal _id field before model validation.
            doc.pop("_id", None)

            # Reconstruct nested ValidationResult objects from stored dicts.
            doc = self._reconstruct_validation_results(doc)

            return QualityScoringResult.model_validate(doc)
        except Exception as exc:
            self.logger.warning(
                "quality_score_retrieve_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    def get_score_history(
        self, tenant_id: str, limit: int = 50,
    ) -> list[QualityScoringResult]:
        """Retrieve historical quality scores for a tenant.

        Queries MongoDB for all scoring results associated with the given
        tenant, sorted by ``scored_at`` descending (most recent first),
        with a configurable limit for pagination.

        Args:
            tenant_id: Tenant namespace.
            limit: Maximum number of results to return (default 50).

        Returns:
            List of :class:`QualityScoringResult` instances sorted by
            ``scored_at`` descending (most recent first).  Returns an
            empty list if MongoDB is unavailable or an error occurs.
        """
        if self._db is None:
            return []

        try:
            collection = self._db[self._SCORING_RESULTS_COLLECTION]
            cursor = (
                collection.find({"tenant_id": tenant_id})
                .sort("scored_at", -1)
                .limit(limit)
            )

            results: list[QualityScoringResult] = []
            for doc in cursor:
                doc.pop("_id", None)
                try:
                    doc = self._reconstruct_validation_results(doc)
                    results.append(QualityScoringResult.model_validate(doc))
                except Exception as reconstruct_exc:
                    self.logger.warning(
                        "quality_score_history_reconstruction_failed",
                        tenant_id=tenant_id,
                        job_id=doc.get("job_id", "unknown"),
                        error=str(reconstruct_exc),
                        error_type=type(reconstruct_exc).__name__,
                    )
            return results
        except Exception as exc:
            self.logger.warning(
                "quality_score_history_failed",
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct_validation_results(
        doc: dict[str, Any],
    ) -> dict[str, Any]:
        """Reconstruct :class:`ValidationResult` objects from stored dicts.

        When reading back from MongoDB, ``validation_results`` entries are
        plain dictionaries.  This helper converts each entry into a
        :class:`ValidationResult` Pydantic model so that the parent
        :class:`QualityScoringResult` can be fully validated.

        Args:
            doc: The raw MongoDB document dictionary.

        Returns:
            The document with ``validation_results`` entries converted to
            :class:`ValidationResult` model instances.
        """
        raw_results: list[Any] = doc.get("validation_results", [])
        reconstructed: list[ValidationResult] = []
        for entry in raw_results:
            if isinstance(entry, ValidationResult):
                reconstructed.append(entry)
            elif isinstance(entry, dict):
                reconstructed.append(ValidationResult.model_validate(entry))
        doc["validation_results"] = reconstructed
        return doc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "QualityScorer",
    "QualityScoringResult",
]
