"""Referential integrity validator for the Quality Service weighted scoring model.

This module implements the :class:`ReferentialIntegrityValidator` class, one of
three pluggable validation strategies in the Quality Service's weighted scoring
system.  It carries a **30 % weight** in the composite quality score::

    Q = 0.4 * S_statistical + 0.3 * S_business_rules + **0.3 * S_referential_integrity**

The validator ensures that generated synthetic ERP data maintains proper foreign
key relationships across tables and ERP modules, covering:

- **FK reference validity** — every child FK value resolves to a valid parent PK
- **Cardinality compliance** — relationship cardinality matches profiled patterns
  (one-to-one, one-to-many, many-to-many)
- **Orphan record prevention** — no child records reference non-existent parents
  (zero-tolerance by default, configurable via ``orphan_tolerance``)
- **Cascade chain completeness** — multi-level dependency chains
  (grandparent → parent → child) remain intact
- **Cross-module reference integrity** — FK references spanning ERP modules
  (e.g., GL entries → HR cost centres, sales orders → MM materials) are valid
- **Uniqueness constraint enforcement** — PKs and unique indexes contain no
  duplicate values

Without valid referential integrity, generated data would contain broken
references (e.g., invoices referencing non-existent customers), rendering it
unsuitable for realistic ERP testing scenarios.

Extends :class:`BaseValidator` to conform to the common Strategy interface
consumed by the :class:`QualityScorer`.

Usage::

    from quality_service.validators.referential_integrity_validator import (
        ReferentialIntegrityValidator,
    )

    validator = ReferentialIntegrityValidator(config={"orphan_tolerance": 0.0})
    result = validator.validate(
        generated_data={"orders": orders_df, "customers": customers_df},
        profile={
            "relationships": [
                {
                    "parent_table": "customers",
                    "child_table": "orders",
                    "parent_column": "customer_id",
                    "child_column": "customer_id",
                    "cardinality": "one_to_many",
                }
            ],
            "uniqueness_constraints": {"customers": ["customer_id"]},
        },
    )
    assert result.score >= 0.95
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from quality_service.validators.base import BaseValidator, ValidationResult
from shared.logging.structured_logger import get_logger


# Module-level structured logger for FK validation events, orphan detection,
# cascade chain analysis, cross-module reference checks, and validation
# error details with structured context.
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# ReferentialIntegrityValidator
# ---------------------------------------------------------------------------


class ReferentialIntegrityValidator(BaseValidator):
    """Validates referential integrity of generated synthetic ERP data.

    Implements the **30 % weighted** component of the quality scoring model.
    Extends :class:`BaseValidator` following the Strategy pattern so that the
    :class:`QualityScorer` can invoke it polymorphically alongside the
    statistical and business-rules validators.

    The overall referential integrity score is itself a weighted combination
    of six sub-component scores:

    ==============================  ======  =================================
    Sub-component                   Weight  Description
    ==============================  ======  =================================
    FK validity                     0.40    All FK values resolve to parents
    Cardinality compliance          0.15    1:1 / 1:N / M:N patterns match
    Orphan-free                     0.15    Zero unresolvable child records
    Cascade chain integrity         0.15    Multi-level chains hold
    Cross-module references         0.10    Cross-ERP-module FKs are valid
    Uniqueness constraints          0.05    PKs / unique indexes no dups
    ==============================  ======  =================================

    Args:
        config: Optional configuration overrides.  Recognised keys:

            - ``orphan_tolerance`` (float): Maximum tolerable orphan ratio
              in [0.0, 1.0].  Default ``0.0`` (zero orphans allowed).
            - ``fk_validity_weight`` (float): Override FK validity sub-weight.
            - ``cardinality_weight`` (float): Override cardinality sub-weight.
            - ``orphan_free_weight`` (float): Override orphan sub-weight.
            - ``cascade_chains_weight`` (float): Override cascade sub-weight.
            - ``cross_module_weight`` (float): Override cross-module weight.
            - ``uniqueness_weight`` (float): Override uniqueness sub-weight.
            - ``minimum_threshold`` (float): Minimum passing score
              (default ``0.95``).

    Attributes:
        weight: Constant ``0.3`` — this validator's share in the composite
            quality score.
        orphan_tolerance: Maximum acceptable orphan ratio in [0.0, 1.0].
    """

    # Class-level constants
    WEIGHT: float = 0.3
    VALIDATOR_NAME: str = "referential_integrity"

    # Default sub-component weights (sum to 1.0)
    _DEFAULT_COMPONENT_WEIGHTS: dict[str, float] = {
        "fk_validity": 0.40,
        "cardinality": 0.15,
        "orphan_free": 0.15,
        "cascade_chains": 0.15,
        "cross_module": 0.10,
        "uniqueness": 0.05,
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.weight: float = self.WEIGHT

        # Per-run tracking state — reset at the start of each validate() call
        self._relationship_graph: dict[str, Any] = {}
        self._validated_pairs: set[tuple[str, str, str, str]] = set()

        # Orphan tolerance threshold (default: zero orphans permitted)
        self.orphan_tolerance: float = float(
            self.config.get("orphan_tolerance", 0.0)
        )

        # Build effective component weights (allow config overrides)
        self._component_weights: dict[str, float] = {}
        for key, default_w in self._DEFAULT_COMPONENT_WEIGHTS.items():
            cfg_key = f"{key}_weight"
            self._component_weights[key] = float(
                self.config.get(cfg_key, default_w)
            )

        self.logger.info(
            "referential_integrity_validator_initialized",
            orphan_tolerance=self.orphan_tolerance,
            minimum_threshold=self.minimum_threshold,
            component_weights=self._component_weights,
        )

    # ------------------------------------------------------------------
    # Abstract interface implementations (BaseValidator contract)
    # ------------------------------------------------------------------

    def get_weight(self) -> float:
        """Return this validator's weight in the composite quality score.

        Returns:
            ``0.3`` — the referential integrity component weight.
        """
        return self.WEIGHT

    def get_name(self) -> str:
        """Return the validator's human-readable identifier.

        Returns:
            ``"referential_integrity"``
        """
        return self.VALIDATOR_NAME

    # ------------------------------------------------------------------
    # Core validation entry point
    # ------------------------------------------------------------------

    def validate(
        self,
        generated_data: dict[str, pd.DataFrame] | pd.DataFrame,
        profile: dict[str, Any],
    ) -> ValidationResult:
        """Run all referential integrity checks against generated data.

        This is the primary entry point conforming to the
        :class:`BaseValidator` interface.  It orchestrates six categories
        of integrity checks and aggregates their scores into a single
        normalised score in [0.0, 1.0].

        Args:
            generated_data: A ``dict[str, pd.DataFrame]`` mapping table names
                to their generated DataFrames for multi-table validation.
                A single :class:`pd.DataFrame` is accepted but produces a
                warning since FK validation is inherently multi-table.
            profile: Schema profile dictionary containing:

                - ``relationships`` (list[dict]): FK relationship definitions.
                  Each dict should include ``parent_table``, ``child_table``,
                  ``parent_column``, ``child_column``, ``cardinality``, and
                  optionally ``module``, ``nullable``, ``min_children``,
                  ``max_children``, ``profiled_avg_children``,
                  ``profiled_std_children``, ``is_cross_module``.
                - ``uniqueness_constraints`` (dict[str, list[str]]): Mapping
                  of table name → list of columns that must be unique.
                - ``tables`` (dict[str, dict]): Table metadata with per-table
                  ``module`` and ``pk_columns`` information.

        Returns:
            A fully populated :class:`ValidationResult` with the referential
            integrity score, per-relationship breakdown, orphan report, and
            cascade / cross-module analysis details.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Reset per-run tracking state
        self._validated_pairs = set()
        self._relationship_graph = {}

        # --- Input normalisation -------------------------------------------
        if isinstance(generated_data, pd.DataFrame):
            warnings.append(
                "Single DataFrame provided; FK validation requires multiple "
                "tables.  Wrapping as {'_single_table': DataFrame}."
            )
            generated_data = {"_single_table": generated_data}

        if not generated_data:
            self.logger.warning(
                "validation_skipped",
                reason="empty_generated_data",
            )
            return self.create_result(
                score=0.0,
                details={"reason": "No generated data provided"},
                errors=["No generated data tables provided for validation"],
                records_validated=0,
                records_passed=0,
            )

        relationships: list[dict[str, Any]] = profile.get("relationships", [])
        if not relationships:
            self.logger.warning(
                "validation_skipped",
                reason="no_relationships_defined",
            )
            total_recs = self._count_total_records(generated_data)
            return self.create_result(
                score=1.0,
                details={"reason": "No relationships defined in profile"},
                warnings=[
                    "No foreign key relationships defined in profile; "
                    "integrity assumed valid",
                ],
                records_validated=total_recs,
                records_passed=total_recs,
            )

        # --- Step 1: Build relationship graph ------------------------------
        self._relationship_graph = self._build_relationship_graph(profile)
        self.logger.info(
            "relationship_graph_built",
            num_tables=len(self._relationship_graph.get("nodes", {})),
            num_relationships=len(relationships),
            root_tables=self._relationship_graph.get("root_tables", []),
            leaf_tables=self._relationship_graph.get("leaf_tables", []),
            has_circular_refs=self._relationship_graph.get(
                "has_circular_refs", False
            ),
        )

        # --- Step 2: Validate each foreign key relationship ----------------
        fk_result = self._run_fk_validations(
            generated_data, relationships, errors, warnings,
        )

        # --- Step 3: Validate cardinality ----------------------------------
        cardinality_scores = self._run_cardinality_validations(
            generated_data, relationships, errors,
        )

        # --- Step 4: Detect orphan records ---------------------------------
        orphan_report = self._detect_orphan_records(
            generated_data, relationships,
        )
        total_orphans = int(
            np.sum([
                entry.get("orphan_count", 0)
                for entry in orphan_report.values()
            ])
        )
        total_records = self._count_total_records(generated_data)
        orphan_score = self._compute_orphan_score(
            total_orphans, total_records,
        )

        # --- Step 5: Validate cascade chains -------------------------------
        cascade_score = self._validate_cascade_chains(
            generated_data, relationships,
        )

        # --- Step 6: Validate cross-module references ----------------------
        cross_module_score = self._validate_cross_module_references(
            generated_data, relationships,
        )

        # --- Step 7: Validate uniqueness constraints -----------------------
        uniqueness_scores = self._run_uniqueness_validations(
            generated_data, profile, errors,
        )

        # --- Aggregate all sub-scores and build the final result -----------
        return self._compose_integrity_result(
            fk_result=fk_result,
            cardinality_scores=cardinality_scores,
            orphan_report=orphan_report,
            orphan_score=orphan_score,
            cascade_score=cascade_score,
            cross_module_score=cross_module_score,
            uniqueness_scores=uniqueness_scores,
            generated_data=generated_data,
            relationships=relationships,
            total_records=total_records,
            total_orphans=total_orphans,
            errors=errors,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Validate-phase helpers (extracted to reduce ``validate`` complexity)
    # ------------------------------------------------------------------

    def _run_fk_validations(
        self,
        tables: dict[str, pd.DataFrame],
        relationships: list[dict[str, Any]],
        errors: list[str],
        warnings: list[str],
    ) -> dict[str, Any]:
        """Execute FK validation for every relationship.

        Returns a result dict with ``fk_scores``, ``total_fk_refs_checked``,
        and ``total_valid_fk_refs`` for downstream aggregation.
        """
        fk_scores: dict[str, float] = {}
        total_fk_refs_checked: int = 0
        total_valid_fk_refs: int = 0

        for rel in relationships:
            rel_result = self._validate_single_fk_relationship(
                rel, tables, errors, warnings,
            )
            if rel_result is None:
                continue

            rel_key, score, checked, valid = rel_result
            fk_scores[rel_key] = score
            total_fk_refs_checked += checked
            total_valid_fk_refs += valid

        return {
            "fk_scores": fk_scores,
            "total_fk_refs_checked": total_fk_refs_checked,
            "total_valid_fk_refs": total_valid_fk_refs,
        }

    def _validate_single_fk_relationship(
        self,
        rel: dict[str, Any],
        tables: dict[str, pd.DataFrame],
        errors: list[str],
        warnings: list[str],
    ) -> tuple[str, float, int, int] | None:
        """Validate one FK relationship and return scoring tuple.

        Returns ``(rel_key, fk_score, refs_checked, valid_refs)`` on
        success, or ``None`` when the relationship should be skipped.
        """
        child_name: str = rel.get("child_table", "")
        parent_name: str = rel.get("parent_table", "")
        fk_col: str = rel.get("child_column", "")
        pk_col: str = rel.get("parent_column", "")

        if not child_name or not parent_name or not fk_col or not pk_col:
            errors.append(f"Incomplete relationship definition: {rel}")
            return None
        if child_name not in tables:
            warnings.append(
                f"Child table '{child_name}' not in generated data",
            )
            return None
        if parent_name not in tables:
            warnings.append(
                f"Parent table '{parent_name}' not in generated data",
            )
            return None

        rel_key = f"{child_name}.{fk_col}->{parent_name}.{pk_col}"
        child_df = tables[child_name]
        parent_df = tables[parent_name]

        if fk_col not in child_df.columns:
            errors.append(
                f"FK column '{fk_col}' not found in table '{child_name}'",
            )
            return rel_key, 0.0, 0, 0
        if pk_col not in parent_df.columns:
            errors.append(
                f"PK column '{pk_col}' not found in table '{parent_name}'",
            )
            return rel_key, 0.0, 0, 0

        try:
            fk_score = self._validate_foreign_key(
                child_table=child_df,
                parent_table=parent_df,
                fk_column=fk_col,
                pk_column=pk_col,
            )
            non_null_count = int(child_df[fk_col].notna().sum())
            valid_count = int(
                child_df[fk_col].dropna().isin(parent_df[pk_col]).sum(),
            )
            self._validated_pairs.add(
                (child_name, fk_col, parent_name, pk_col),
            )
            if fk_score < 1.0:
                self.logger.info(
                    "fk_validation_partial",
                    relationship=rel_key,
                    score=round(fk_score, 4),
                    total_refs=non_null_count,
                    valid_refs=valid_count,
                )
            return rel_key, fk_score, non_null_count, valid_count
        except Exception as exc:
            errors.append(f"FK validation error for {rel_key}: {exc}")
            self.logger.error(
                "fk_validation_error",
                relationship=rel_key,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return rel_key, 0.0, 0, 0

    def _run_cardinality_validations(
        self,
        tables: dict[str, pd.DataFrame],
        relationships: list[dict[str, Any]],
        errors: list[str],
    ) -> dict[str, float]:
        """Execute cardinality validation for every relationship."""
        cardinality_scores: dict[str, float] = {}

        for rel in relationships:
            child_name: str = rel.get("child_table", "")
            parent_name: str = rel.get("parent_table", "")
            if child_name not in tables or parent_name not in tables:
                continue

            rel_key = f"{child_name}->{parent_name}:cardinality"
            try:
                card_score = self._validate_cardinality(
                    child_table=tables[child_name],
                    parent_table=tables[parent_name],
                    relationship=rel,
                )
                cardinality_scores[rel_key] = card_score
            except Exception as exc:
                errors.append(
                    f"Cardinality validation error for {rel_key}: {exc}",
                )
                cardinality_scores[rel_key] = 0.0
                self.logger.error(
                    "cardinality_validation_error",
                    relationship=rel_key,
                    error=str(exc),
                )

        return cardinality_scores

    def _run_uniqueness_validations(
        self,
        tables: dict[str, pd.DataFrame],
        profile: dict[str, Any],
        errors: list[str],
    ) -> dict[str, float]:
        """Execute uniqueness validation for each constrained table."""
        uniqueness_constraints: dict[str, list[str]] = profile.get(
            "uniqueness_constraints", {},
        )
        uniqueness_scores: dict[str, float] = {}

        for table_name, unique_cols in uniqueness_constraints.items():
            if table_name not in tables or not unique_cols:
                continue
            try:
                u_score = self._validate_uniqueness_constraints(
                    tables[table_name], unique_cols,
                )
                uniqueness_scores[table_name] = u_score
            except Exception as exc:
                errors.append(
                    f"Uniqueness validation error for '{table_name}': {exc}",
                )
                uniqueness_scores[table_name] = 0.0
                self.logger.error(
                    "uniqueness_validation_error",
                    table=table_name,
                    error=str(exc),
                )

        return uniqueness_scores

    def _compose_integrity_result(
        self,
        *,
        fk_result: dict[str, Any],
        cardinality_scores: dict[str, float],
        orphan_report: dict[str, Any],
        orphan_score: float,
        cascade_score: float,
        cross_module_score: float,
        uniqueness_scores: dict[str, float],
        generated_data: dict[str, pd.DataFrame],
        relationships: list[dict[str, Any]],
        total_records: int,
        total_orphans: int,
        errors: list[str],
        warnings: list[str],
    ) -> ValidationResult:
        """Aggregate sub-scores and build the final ValidationResult."""
        fk_scores: dict[str, float] = fk_result["fk_scores"]
        total_fk_refs_checked: int = fk_result["total_fk_refs_checked"]
        total_valid_fk_refs: int = fk_result["total_valid_fk_refs"]

        fk_aggregate = (
            self._aggregate_relationship_scores(fk_scores)
            if fk_scores else 1.0
        )
        card_aggregate = (
            self._aggregate_relationship_scores(cardinality_scores)
            if cardinality_scores else 1.0
        )
        uniqueness_aggregate = (
            float(np.mean(list(uniqueness_scores.values())))
            if uniqueness_scores else 1.0
        )

        component_scores: dict[str, float] = {
            "fk_validity": fk_aggregate,
            "cardinality": card_aggregate,
            "orphan_free": orphan_score,
            "cascade_chains": cascade_score,
            "cross_module": cross_module_score,
            "uniqueness": uniqueness_aggregate,
        }

        scores_arr = [component_scores[k] for k in self._component_weights]
        weights_arr = list(self._component_weights.values())
        overall_score = float(np.average(scores_arr, weights=weights_arr))

        details = self._build_result_details(
            component_scores=component_scores,
            fk_scores=fk_scores,
            cardinality_scores=cardinality_scores,
            uniqueness_scores=uniqueness_scores,
            orphan_report=orphan_report,
            total_orphans=total_orphans,
            cascade_score=cascade_score,
            cross_module_score=cross_module_score,
            overall_score=overall_score,
            generated_data=generated_data,
            relationships=relationships,
        )

        records_passed = min(total_valid_fk_refs, total_fk_refs_checked)
        if total_fk_refs_checked == 0:
            records_passed = total_records

        self.logger.info(
            "referential_integrity_validation_complete",
            overall_score=round(overall_score, 4),
            fk_score=round(fk_aggregate, 4),
            cardinality_score=round(card_aggregate, 4),
            orphan_score=round(orphan_score, 4),
            cascade_score=round(cascade_score, 4),
            cross_module_score=round(cross_module_score, 4),
            uniqueness_score=round(uniqueness_aggregate, 4),
            total_records=total_records,
            total_fk_refs_checked=total_fk_refs_checked,
            total_valid_fk_refs=total_valid_fk_refs,
            total_orphans=total_orphans,
            passed=overall_score >= self.minimum_threshold,
        )

        return self.create_result(
            score=overall_score,
            details=details,
            errors=errors if errors else None,
            warnings=warnings if warnings else None,
            records_validated=total_records,
            records_passed=records_passed,
        )

    def _build_result_details(
        self,
        *,
        component_scores: dict[str, float],
        fk_scores: dict[str, float],
        cardinality_scores: dict[str, float],
        uniqueness_scores: dict[str, float],
        orphan_report: dict[str, Any],
        total_orphans: int,
        cascade_score: float,
        cross_module_score: float,
        overall_score: float,
        generated_data: dict[str, pd.DataFrame],
        relationships: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the detailed result dictionary for the ValidationResult."""
        return {
            "component_scores": {
                k: {
                    "score": round(component_scores[k], 6),
                    "weight": self._component_weights[k],
                    "weighted_contribution": round(
                        component_scores[k] * self._component_weights[k], 6,
                    ),
                }
                for k in self._component_weights
            },
            "fk_validation": fk_scores,
            "cardinality_validation": cardinality_scores,
            "uniqueness_validation": uniqueness_scores,
            "orphan_report": {
                "total_orphans": total_orphans,
                "orphan_tolerance": self.orphan_tolerance,
                "per_table": orphan_report,
            },
            "cascade_chain_score": cascade_score,
            "cross_module_score": cross_module_score,
            "overall_score": round(overall_score, 6),
            "tables_validated": list(generated_data.keys()),
            "relationships_checked": len(fk_scores),
            "relationship_graph_summary": {
                "num_relationships": len(relationships),
                "root_tables": self._relationship_graph.get(
                    "root_tables", [],
                ),
                "leaf_tables": self._relationship_graph.get(
                    "leaf_tables", [],
                ),
                "has_circular_refs": self._relationship_graph.get(
                    "has_circular_refs", False,
                ),
            },
        }

    # ------------------------------------------------------------------
    # FK validation
    # ------------------------------------------------------------------

    def _validate_foreign_key(
        self,
        child_table: pd.DataFrame,
        parent_table: pd.DataFrame,
        fk_column: str,
        pk_column: str,
    ) -> float:
        """Check that every FK value in the child table exists in the parent.

        Handles nullable FK columns by excluding ``NaN`` / ``None`` values
        from the validity check — a null FK is considered valid when the
        column is nullable.

        Args:
            child_table: Child table DataFrame containing the FK column.
            parent_table: Parent table DataFrame containing the PK column.
            fk_column: Name of the column in *child_table* holding FK values.
            pk_column: Name of the column in *parent_table* holding PK values.

        Returns:
            FK validity score in [0.0, 1.0] — the fraction of non-null FK
            values that resolve to a valid parent PK.
        """
        if child_table.empty:
            return 1.0

        fk_series = child_table[fk_column]

        # Identify null FK values — nulls are excluded from validity checks
        null_mask = fk_series.isna()
        non_null_fks = fk_series[~null_mask]

        if non_null_fks.empty:
            # All FK values are null — valid if column is nullable
            return 1.0

        # Build the set of valid parent PKs
        parent_pks = parent_table[pk_column].dropna().unique()

        # Check each FK value exists in the parent PK set
        valid_mask = non_null_fks.isin(parent_pks)
        valid_count = int(valid_mask.sum())
        total_count = len(non_null_fks)

        score = valid_count / total_count if total_count > 0 else 1.0

        # Log orphan FK values for diagnostic purposes
        if valid_count < total_count:
            orphan_fks = non_null_fks[~valid_mask]
            sample_orphans = orphan_fks.head(10).tolist()
            self.logger.debug(
                "orphan_fk_values_detected",
                fk_column=fk_column,
                pk_column=pk_column,
                orphan_count=total_count - valid_count,
                total_checked=total_count,
                sample_orphans=sample_orphans,
            )

        return float(np.float64(score))

    # ------------------------------------------------------------------
    # Cardinality validation
    # ------------------------------------------------------------------

    def _validate_cardinality(
        self,
        child_table: pd.DataFrame,
        parent_table: pd.DataFrame,
        relationship: dict[str, Any],
    ) -> float:
        """Validate that the relationship cardinality matches expected patterns.

        Checks one-to-one, one-to-many, and many-to-many cardinality
        constraints.  When profiled cardinality statistics are available
        (``profiled_avg_children``, ``profiled_std_children``), the method
        compares the generated distribution against the profiled one.

        Args:
            child_table: Child table DataFrame.
            parent_table: Parent table DataFrame.
            relationship: Relationship metadata dictionary containing:

                - ``child_column`` (str): FK column in the child table.
                - ``parent_column`` (str): PK column in the parent table.
                - ``cardinality`` (str): One of ``"one_to_one"``,
                  ``"one_to_many"``, or ``"many_to_many"``.
                - ``min_children`` (int, optional): Minimum child count
                  per parent.
                - ``max_children`` (int, optional): Maximum child count
                  per parent.
                - ``profiled_avg_children`` (float, optional): Profiled
                  average children per parent.
                - ``profiled_std_children`` (float, optional): Profiled
                  standard deviation of children per parent.

        Returns:
            Cardinality compliance score in [0.0, 1.0].
        """
        fk_col: str = relationship.get("child_column", "")
        pk_col: str = relationship.get("parent_column", "")
        cardinality_type: str = relationship.get("cardinality", "one_to_many")

        if child_table.empty or parent_table.empty:
            return 1.0
        if fk_col not in child_table.columns or pk_col not in parent_table.columns:
            return 0.0

        fk_values = child_table[fk_col].dropna()
        if fk_values.empty:
            return 1.0

        # Build cardinality statistics used by sub-checks.
        cardinality_groups = child_table.groupby(fk_col).size()
        children_per_parent = fk_values.value_counts()

        # Dispatch structural check by cardinality type.
        scores = self._structural_cardinality_scores(
            cardinality_type, cardinality_groups, children_per_parent,
            parent_table, pk_col, relationship,
        )

        # Append profiled distribution comparison score (if available).
        dist_score = self._compare_cardinality_distribution(
            children_per_parent, child_table, fk_col, relationship,
        )
        if dist_score is not None:
            scores.append(dist_score)

        return float(np.mean(scores)) if scores else 1.0

    # -- Cardinality sub-checks -----------------------------------------

    def _structural_cardinality_scores(
        self,
        cardinality_type: str,
        cardinality_groups: pd.Series,
        children_per_parent: pd.Series,
        parent_table: pd.DataFrame,
        pk_col: str,
        relationship: dict[str, Any],
    ) -> list[float]:
        """Return structural cardinality scores based on the type.

        Dispatches to a dedicated helper for each cardinality kind and
        returns the partial score list that will later be combined with
        the optional profiled-distribution score.
        """
        dispatch: dict[str, Any] = {
            "one_to_one": lambda: self._cardinality_one_to_one(
                cardinality_groups,
            ),
            "one_to_many": lambda: self._cardinality_one_to_many(
                cardinality_groups, children_per_parent,
                parent_table, pk_col, relationship,
            ),
            "many_to_many": lambda: [1.0],
        }
        handler = dispatch.get(cardinality_type)
        if handler is not None:
            result: list[float] = handler()
            return result

        self.logger.warning(
            "unknown_cardinality_type",
            cardinality=cardinality_type,
        )
        return [1.0]

    @staticmethod
    def _cardinality_one_to_one(
        cardinality_groups: pd.Series,
    ) -> list[float]:
        """Score one-to-one: each parent maps to at most one child."""
        one_to_one_violations = int((cardinality_groups > 1).sum())
        total_groups = len(cardinality_groups)
        if total_groups > 0:
            compliance = 1.0 - (one_to_one_violations / total_groups)
            return [max(0.0, compliance)]
        return [1.0]

    @staticmethod
    def _cardinality_one_to_many(
        cardinality_groups: pd.Series,
        children_per_parent: pd.Series,
        parent_table: pd.DataFrame,
        pk_col: str,
        relationship: dict[str, Any],
    ) -> list[float]:
        """Score one-to-many: validate min/max child bounds if specified."""
        scores: list[float] = []
        min_children: int = relationship.get("min_children", 0)
        max_children_bound = relationship.get("max_children")

        if min_children > 0:
            parent_pks = parent_table[pk_col].dropna().unique()
            parents_without_enough = sum(
                1 for pk_val in parent_pks
                if (
                    children_per_parent.get(pk_val, 0)
                    if pk_val in children_per_parent.index
                    else 0
                ) < min_children
            )
            total_parents = len(parent_pks)
            score = (
                1.0 - (parents_without_enough / total_parents)
                if total_parents > 0
                else 1.0
            )
            scores.append(score)

        if max_children_bound is not None:
            over_max = int((cardinality_groups > max_children_bound).sum())
            total_grouped = len(cardinality_groups)
            score = (
                1.0 - (over_max / total_grouped)
                if total_grouped > 0
                else 1.0
            )
            scores.append(score)

        # No bounds specified -- structurally valid.
        if not scores:
            scores.append(1.0)
        return scores

    def _compare_cardinality_distribution(
        self,
        children_per_parent: pd.Series,
        child_table: pd.DataFrame,
        fk_col: str,
        relationship: dict[str, Any],
    ) -> float | None:
        """Compare generated cardinality distribution to profiled stats.

        Returns a score in [0.0, 1.0] if profiled statistics are available,
        or ``None`` when no comparison can be made.
        """
        profiled_avg = relationship.get("profiled_avg_children")
        profiled_std = relationship.get("profiled_std_children")

        if profiled_avg is None or children_per_parent.empty:
            return None

        actual_avg = float(children_per_parent.mean())
        actual_nunique = int(child_table[fk_col].nunique())

        result: float | None = None
        if profiled_std is not None and profiled_std > 0:
            z_score = abs(actual_avg - profiled_avg) / profiled_std
            result = max(0.0, 1.0 - (z_score / 4.0))
        elif profiled_avg > 0:
            result = min(actual_avg, profiled_avg) / max(
                actual_avg, profiled_avg
            )

        self.logger.debug(
            "cardinality_distribution_check",
            profiled_avg=profiled_avg,
            profiled_std=profiled_std,
            actual_avg=round(actual_avg, 4),
            actual_unique_fks=actual_nunique,
        )
        return result

    # ------------------------------------------------------------------
    # Orphan record detection
    # ------------------------------------------------------------------

    def _detect_orphan_records(
        self,
        tables: dict[str, pd.DataFrame],
        relationships: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Scan all tables for orphan records.

        An *orphan child* is a row whose FK value does not appear in the
        parent table's PK column.  When ``min_children > 0``, parent rows
        that have no children are also flagged as anomalous.

        Args:
            tables: Mapping of table name → DataFrame.
            relationships: List of FK relationship definitions.

        Returns:
            Dictionary keyed by ``"child_table.fk_column"`` containing:

            - ``orphan_count`` (int): Number of orphan child records.
            - ``orphan_ratio`` (float): Orphans / non-null FK count.
            - ``sample_orphan_keys`` (list): Up to 10 sample orphan FK
              values for debugging.
            - ``childless_parents`` (int): Parents missing required children
              (only counted when ``min_children > 0``).
            - ``total_child_rows`` (int): Total rows in the child table.
            - ``non_null_fk_count`` (int): Non-null FK values in child.
        """
        orphan_report: dict[str, Any] = {}

        for rel in relationships:
            child_name: str = rel.get("child_table", "")
            parent_name: str = rel.get("parent_table", "")
            fk_col: str = rel.get("child_column", "")
            pk_col: str = rel.get("parent_column", "")

            if (
                child_name not in tables
                or parent_name not in tables
                or not fk_col
                or not pk_col
            ):
                continue

            child_df = tables[child_name]
            parent_df = tables[parent_name]

            if fk_col not in child_df.columns or pk_col not in parent_df.columns:
                continue

            report_key = f"{child_name}.{fk_col}"

            # --- Orphan children -------------------------------------------
            fk_series = child_df[fk_col]
            non_null_fks = fk_series.dropna()
            parent_pk_set = set(parent_df[pk_col].dropna().unique())

            if not non_null_fks.empty:
                orphan_mask = ~non_null_fks.isin(parent_pk_set)
                orphan_values = non_null_fks[orphan_mask]
                orphan_count = len(orphan_values)
                orphan_ratio = (
                    orphan_count / len(non_null_fks)
                    if len(non_null_fks) > 0
                    else 0.0
                )
                sample_orphan_keys = orphan_values.head(10).tolist()
            else:
                orphan_count = 0
                orphan_ratio = 0.0
                sample_orphan_keys = []

            # --- Childless parents (when min_children > 0) -----------------
            min_children = rel.get("min_children", 0)
            childless_parents = 0

            if min_children > 0 and not parent_df.empty:
                child_fk_set = (
                    set(non_null_fks.unique())
                    if not non_null_fks.empty
                    else set()
                )
                childless_parents = int(
                    sum(
                        1
                        for pk in parent_df[pk_col].dropna()
                        if pk not in child_fk_set
                    )
                )

            orphan_report[report_key] = {
                "orphan_count": orphan_count,
                "orphan_ratio": round(orphan_ratio, 6),
                "sample_orphan_keys": sample_orphan_keys,
                "childless_parents": childless_parents,
                "total_child_rows": len(child_df),
                "non_null_fk_count": len(non_null_fks),
            }

            if orphan_count > 0:
                self.logger.info(
                    "orphan_records_detected",
                    table=child_name,
                    fk_column=fk_col,
                    orphan_count=orphan_count,
                    orphan_ratio=round(orphan_ratio, 4),
                )

        return orphan_report

    # ------------------------------------------------------------------
    # Cascade chain validation
    # ------------------------------------------------------------------

    def _validate_cascade_chains(
        self,
        tables: dict[str, pd.DataFrame],
        relationships: list[dict[str, Any]],
    ) -> float:
        """Validate multi-level cascading relationships.

        Ensures transitive integrity holds across the full dependency chain.
        For example, in a ``Sales Order → Order Line Items → Delivery Items``
        chain, every Delivery Item must ultimately trace back to a valid
        Sales Order through its parent Order Line Item.

        Uses :meth:`pd.DataFrame.merge` to join tables along the cascade
        chain for efficient transitive-reference checking.

        Args:
            tables: Mapping of table name → DataFrame.
            relationships: List of FK relationship definitions.

        Returns:
            Cascade chain integrity score in [0.0, 1.0].
        """
        if not relationships or len(relationships) < 2:
            return 1.0

        # Build adjacency list: parent → [(child, fk_col, pk_col)]
        children_of: dict[str, list[dict[str, str]]] = defaultdict(list)
        for rel in relationships:
            parent = rel.get("parent_table", "")
            child = rel.get("child_table", "")
            if parent and child:
                children_of[parent].append({
                    "child_table": child,
                    "child_column": rel.get("child_column", ""),
                    "parent_column": rel.get("parent_column", ""),
                })

        # Discover and validate all chains of depth ≥ 2
        chain_scores: list[float] = []

        for grandparent, parent_rels in children_of.items():
            for parent_rel in parent_rels:
                parent_table_name = parent_rel["child_table"]
                if parent_table_name not in children_of:
                    continue

                for child_rel in children_of[parent_table_name]:
                    child_table_name = child_rel["child_table"]

                    chain_score = self._validate_single_chain(
                        tables=tables,
                        grandparent_table=grandparent,
                        grandparent_pk=parent_rel["parent_column"],
                        parent_table=parent_table_name,
                        parent_fk=parent_rel["child_column"],
                        parent_pk=child_rel["parent_column"],
                        child_table=child_table_name,
                        child_fk=child_rel["child_column"],
                    )
                    chain_scores.append(chain_score)

                    self.logger.debug(
                        "cascade_chain_validated",
                        chain=(
                            f"{grandparent}->{parent_table_name}"
                            f"->{child_table_name}"
                        ),
                        score=round(chain_score, 4),
                    )

        if not chain_scores:
            return 1.0

        return float(np.mean(chain_scores))

    def _validate_single_chain(
        self,
        tables: dict[str, pd.DataFrame],
        grandparent_table: str,
        grandparent_pk: str,
        parent_table: str,
        parent_fk: str,
        parent_pk: str,
        child_table: str,
        child_fk: str,
    ) -> float:
        """Validate a single three-level cascade chain using merge.

        Traces child → parent → grandparent and computes the fraction
        of child records whose transitive FK chain is fully intact.

        Args:
            tables: Mapping of table name → DataFrame.
            grandparent_table: Name of the root ancestor table.
            grandparent_pk: PK column in the grandparent table.
            parent_table: Name of the intermediate table.
            parent_fk: FK column in the parent table referencing the
                grandparent.
            parent_pk: PK column in the parent table.
            child_table: Name of the leaf table.
            child_fk: FK column in the child table referencing the parent.

        Returns:
            Chain integrity score in [0.0, 1.0].
        """
        if (
            grandparent_table not in tables
            or parent_table not in tables
            or child_table not in tables
        ):
            return 1.0

        gp_df = tables[grandparent_table]
        p_df = tables[parent_table]
        c_df = tables[child_table]

        # Verify required columns exist in all three tables
        required = [
            (gp_df, grandparent_pk, grandparent_table),
            (p_df, parent_fk, parent_table),
            (p_df, parent_pk, parent_table),
            (c_df, child_fk, child_table),
        ]
        for df, col, tbl in required:
            if col not in df.columns:
                self.logger.warning(
                    "cascade_chain_column_missing",
                    table=tbl,
                    column=col,
                )
                return 1.0

        if c_df.empty:
            return 1.0

        # Merge child → parent on the FK/PK join to trace the chain.
        # This is more efficient than nested loops for large DataFrames.
        child_parent_merged = c_df[[child_fk]].merge(
            p_df[[parent_pk, parent_fk]],
            left_on=child_fk,
            right_on=parent_pk,
            how="left",
        )

        # Check that each resolved parent row has a valid grandparent FK
        gp_pks = set(gp_df[grandparent_pk].dropna().unique())
        resolved_parent_fks = child_parent_merged[parent_fk].dropna()

        if resolved_parent_fks.empty:
            return 1.0

        chain_valid_mask = resolved_parent_fks.isin(gp_pks)
        valid_count = int(chain_valid_mask.sum())
        total_count = len(resolved_parent_fks)

        return valid_count / total_count if total_count > 0 else 1.0

    # ------------------------------------------------------------------
    # Cross-module reference validation
    # ------------------------------------------------------------------

    def _validate_cross_module_references(
        self,
        tables: dict[str, pd.DataFrame],
        relationships: list[dict[str, Any]],
    ) -> float:
        """Validate FK references that span ERP modules.

        Cross-module references occur when, for example:

        - GL entries (Financial Accounting) reference cost centres (HR)
        - Sales orders (Sales & Distribution) reference materials (MM)
        - Purchase orders (Material Management) reference vendors (FI)

        Args:
            tables: Mapping of table name → DataFrame.
            relationships: List of FK relationship definitions.  Each
                entry may include ``is_cross_module`` (bool) and/or
                ``module`` (str) metadata.

        Returns:
            Cross-module referential integrity score in [0.0, 1.0].
            Returns ``1.0`` when no cross-module relationships exist.
        """
        table_modules: dict[str, str] = self._relationship_graph.get(
            "table_modules", {}
        )
        cross_module_scores: list[float] = []

        for rel in relationships:
            child_name: str = rel.get("child_table", "")
            parent_name: str = rel.get("parent_table", "")
            fk_col: str = rel.get("child_column", "")
            pk_col: str = rel.get("parent_column", "")

            # Determine whether this is a cross-module relationship
            is_cross_module: bool = rel.get("is_cross_module", False)
            if not is_cross_module:
                child_module = table_modules.get(child_name, "")
                parent_module = table_modules.get(parent_name, "")
                if (
                    child_module
                    and parent_module
                    and child_module != parent_module
                ):
                    is_cross_module = True

            if not is_cross_module:
                continue

            if (
                child_name not in tables
                or parent_name not in tables
                or not fk_col
                or not pk_col
            ):
                continue

            child_df = tables[child_name]
            parent_df = tables[parent_name]

            if (
                fk_col not in child_df.columns
                or pk_col not in parent_df.columns
            ):
                cross_module_scores.append(0.0)
                continue

            try:
                score = self._validate_foreign_key(
                    child_table=child_df,
                    parent_table=parent_df,
                    fk_column=fk_col,
                    pk_column=pk_col,
                )
                cross_module_scores.append(score)

                self.logger.info(
                    "cross_module_reference_validated",
                    child_table=child_name,
                    parent_table=parent_name,
                    child_module=table_modules.get(child_name, "unknown"),
                    parent_module=table_modules.get(parent_name, "unknown"),
                    score=round(score, 4),
                )
            except Exception as exc:
                cross_module_scores.append(0.0)
                self.logger.error(
                    "cross_module_validation_error",
                    child_table=child_name,
                    parent_table=parent_name,
                    error=str(exc),
                )

        if not cross_module_scores:
            return 1.0

        return float(np.mean(cross_module_scores))

    # ------------------------------------------------------------------
    # Relationship graph construction
    # ------------------------------------------------------------------

    def _build_relationship_graph(
        self,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Parse the schema profile and build a directed dependency graph.

        Constructs a directed graph of table dependencies from FK
        relationship definitions, identifies root tables (no inbound FK
        dependencies) and leaf tables (only outbound FKs, no dependents),
        and detects circular references.

        Args:
            profile: Schema profile containing:

                - ``relationships`` (list[dict]): FK relationship defs.
                - ``tables`` (dict[str, dict], optional): Per-table metadata
                  including ``module`` classification.

        Returns:
            A dictionary with:

            - ``nodes`` (dict): Table name → ``{in_edges, out_edges}``.
            - ``edges`` (list): All FK edges as 4-tuples
              ``(child, fk_col, parent, pk_col)``.
            - ``root_tables`` (list[str]): Tables with no FK dependencies
              (i.e., no outbound FK edges pointing to another table).
            - ``leaf_tables`` (list[str]): Tables that no other table
              depends on (i.e., no inbound FK edges).
            - ``has_circular_refs`` (bool): ``True`` if at least one cycle
              exists.
            - ``table_modules`` (dict[str, str]): Table name → ERP module.
        """
        relationships = profile.get("relationships", [])
        table_metadata: dict[str, Any] = profile.get("tables", {})

        # Nodes with directed edge tracking
        nodes: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: {"in_edges": [], "out_edges": []}
        )
        edges: list[tuple[str, str, str, str]] = []

        for rel in relationships:
            parent: str = rel.get("parent_table", "")
            child: str = rel.get("child_table", "")
            pk_col: str = rel.get("parent_column", "")
            fk_col: str = rel.get("child_column", "")

            if not parent or not child:
                continue

            # Ensure both nodes exist in the graph
            _ = nodes[parent]
            _ = nodes[child]

            # Child depends on parent (child has an FK pointing to parent)
            nodes[child]["out_edges"].append(parent)
            nodes[parent]["in_edges"].append(child)
            edges.append((child, fk_col, parent, pk_col))

        # Root tables: no outbound FK edges (no FK dependencies)
        root_tables = [
            name for name, info in nodes.items() if not info["out_edges"]
        ]

        # Leaf tables: no inbound edges (no other table depends on them)
        leaf_tables = [
            name for name, info in nodes.items() if not info["in_edges"]
        ]

        # Cycle detection via DFS
        has_circular = self._detect_circular_references(nodes)
        if has_circular:
            self.logger.warning(
                "circular_references_detected",
                num_tables=len(nodes),
            )

        # Build table → module mapping from metadata and relationships
        table_modules: dict[str, str] = {}
        for tbl_name, tbl_meta in table_metadata.items():
            if isinstance(tbl_meta, dict):
                table_modules[tbl_name] = tbl_meta.get("module", "")

        for rel in relationships:
            module = rel.get("module", "")
            if module:
                for key in ("parent_table", "child_table"):
                    tbl = rel.get(key, "")
                    if tbl and tbl not in table_modules:
                        table_modules[tbl] = module

        return {
            "nodes": dict(nodes),
            "edges": edges,
            "root_tables": root_tables,
            "leaf_tables": leaf_tables,
            "has_circular_refs": has_circular,
            "table_modules": table_modules,
        }

    @staticmethod
    def _detect_circular_references(
        nodes: dict[str, dict[str, list[str]]],
    ) -> bool:
        """Detect circular references in the graph via iterative DFS.

        Uses three-colour marking (WHITE → GRAY → BLACK) to identify
        back-edges that indicate cycles, without risking Python's recursion
        limit on very deep dependency graphs.

        Args:
            nodes: Graph adjacency structure keyed by table name.

        Returns:
            ``True`` if at least one cycle exists, ``False`` otherwise.
        """
        state_white, state_gray, state_black = 0, 1, 2
        colour: dict[str, int] = dict.fromkeys(nodes, state_white)

        for start in nodes:
            if colour[start] != state_white:
                continue

            # Iterative DFS using an explicit stack of
            # (node, neighbour_iterator) pairs.
            stack: list[tuple[str, int]] = [(start, 0)]
            colour[start] = state_gray

            while stack:
                node, idx = stack[-1]
                neighbours = nodes.get(node, {}).get("out_edges", [])

                if idx < len(neighbours):
                    stack[-1] = (node, idx + 1)
                    neighbour = neighbours[idx]
                    if neighbour not in colour:
                        continue
                    if colour[neighbour] == state_gray:
                        return True  # Back-edge detected — cycle exists
                    if colour[neighbour] == state_white:
                        colour[neighbour] = state_gray
                        stack.append((neighbour, 0))
                else:
                    colour[node] = state_black
                    stack.pop()

        return False

    # ------------------------------------------------------------------
    # Uniqueness constraint validation
    # ------------------------------------------------------------------

    def _validate_uniqueness_constraints(
        self,
        table: pd.DataFrame,
        unique_columns: list[str],
    ) -> float:
        """Validate that columns marked as unique contain no duplicates.

        Supports both single-column unique constraints and composite
        uniqueness (when *unique_columns* lists multiple column names
        representing a composite key).

        Args:
            table: The DataFrame to validate.
            unique_columns: Column name(s) that must be unique.  If
                multiple columns are listed, their *combination* must be
                unique.

        Returns:
            Uniqueness compliance score in [0.0, 1.0] — the fraction of
            non-duplicate rows on the specified columns.
        """
        if table.empty:
            return 1.0

        existing_cols = [c for c in unique_columns if c in table.columns]
        if not existing_cols:
            return 1.0

        # Use duplicated() to identify non-unique rows
        if len(existing_cols) == 1:
            dup_mask = table[existing_cols[0]].duplicated(keep=False)
        else:
            dup_mask = table.duplicated(subset=existing_cols, keep=False)

        total_rows = len(table)
        duplicate_rows = int(dup_mask.sum())
        unique_rows = total_rows - duplicate_rows

        score = unique_rows / total_rows if total_rows > 0 else 1.0

        if duplicate_rows > 0:
            self.logger.info(
                "uniqueness_violation_detected",
                columns=existing_cols,
                duplicate_rows=duplicate_rows,
                total_rows=total_rows,
                score=round(score, 4),
            )

        return score

    # ------------------------------------------------------------------
    # Score aggregation and helpers
    # ------------------------------------------------------------------

    def _aggregate_relationship_scores(
        self,
        relationship_scores: dict[str, float],
    ) -> float:
        """Compute the weighted average of per-relationship scores.

        Currently uses uniform weights across all relationships.  Future
        implementations may weight by table size or relationship criticality.

        Args:
            relationship_scores: Mapping of relationship key → score.

        Returns:
            Aggregate score in [0.0, 1.0].
        """
        if not relationship_scores:
            return 1.0

        values = list(relationship_scores.values())
        return float(np.average(values))

    def _compute_orphan_score(
        self,
        total_orphans: int,
        total_records: int,
    ) -> float:
        """Compute the orphan-free sub-score.

        Scoring rules:

        - Zero tolerance (default): any orphan → ``0.0``.
        - Within tolerance: full score ``1.0``.
        - Beyond tolerance: linear degradation proportional to excess.

        Args:
            total_orphans: Total number of orphan records detected across
                all tables and relationships.
            total_records: Total number of records across all tables.

        Returns:
            Orphan-free score in [0.0, 1.0].
        """
        if total_records == 0 or total_orphans == 0:
            return 1.0

        orphan_ratio = total_orphans / total_records

        if self.orphan_tolerance == 0.0:
            # Zero-tolerance mode: any orphan → score drops to 0.0
            return 0.0

        if orphan_ratio <= self.orphan_tolerance:
            return 1.0

        # Linear degradation beyond the tolerance threshold
        excess = (orphan_ratio - self.orphan_tolerance) / (
            1.0 - self.orphan_tolerance
        )
        return max(0.0, 1.0 - excess)

    @staticmethod
    def _count_total_records(
        tables: dict[str, pd.DataFrame],
    ) -> int:
        """Count the total number of records across all tables.

        Args:
            tables: Mapping of table name → DataFrame.

        Returns:
            Sum of row counts across every DataFrame.
        """
        return int(np.sum([len(df) for df in tables.values()]))
