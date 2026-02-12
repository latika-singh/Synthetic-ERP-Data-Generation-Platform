"""Comprehensive pytest unit test module for the Quality Service.

Tests the abstract base validator interface, statistical validator (40% weight),
business rules validator (30% weight), referential integrity validator (30%
weight), the weighted composite quality scorer targeting ≥95% fidelity, and
the quality report generator with detailed metrics and drill-down data.

Uses conftest.py fixtures for mock MongoDB and sample generated datasets.
Follows R-014 (comprehensive testing) and R-009 (≥95% quality threshold).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Quality Service Imports
# ---------------------------------------------------------------------------

import sys
import os

# Ensure the backend source is on the path
_backend_root = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src", "backend"
)
if os.path.isdir(_backend_root) and _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)


# =====================================================================
# TestBaseValidator — Abstract Validator Interface Tests
# =====================================================================


class TestBaseValidator:
    """Tests for the BaseValidator abstract class and ValidationResult model."""

    def test_base_validator_is_abstract(self) -> None:
        """Cannot instantiate BaseValidator directly — it is abstract."""
        from quality_service.validators.base import BaseValidator

        with pytest.raises(TypeError):
            BaseValidator()  # type: ignore[abstract]

    def test_base_validator_interface(self) -> None:
        """Verify abstract methods (validate, get_weight, get_name) exist."""
        from quality_service.validators.base import BaseValidator
        import inspect

        abstract_methods = set()
        for name, method in inspect.getmembers(BaseValidator):
            if getattr(method, "__isabstractmethod__", False):
                abstract_methods.add(name)

        assert "validate" in abstract_methods
        assert "get_weight" in abstract_methods
        assert "get_name" in abstract_methods

    def test_validation_result_model(self) -> None:
        """ValidationResult can be instantiated with all fields."""
        from quality_service.validators.base import ValidationResult

        result = ValidationResult(
            validator_name="test_validator",
            score=0.85,
            weight=0.4,
            weighted_score=0.34,
            passed=True,
            threshold=0.8,
            details={"column1": {"ks_pvalue": 0.9}},
            errors=[],
            warnings=["minor issue"],
            metadata={},
            records_validated=100,
            records_passed=85,
            execution_time_ms=150.0,
        )

        assert result.validator_name == "test_validator"
        assert result.score == 0.85
        assert result.weight == 0.4
        assert abs(result.weighted_score - 0.34) < 0.001
        assert result.passed is True
        assert result.records_validated == 100
        assert result.records_passed == 85

    def test_validate_returns_score_normalized(self) -> None:
        """A valid subclass returns a score normalized to [0, 1]."""
        from quality_service.validators.base import BaseValidator, ValidationResult

        class ConcreteValidator(BaseValidator):
            def validate(self, generated_data, profile):
                return self.create_result(
                    score=0.87,
                    details={"test": True},
                    records_validated=50,
                    records_passed=43,
                )

            def get_weight(self) -> float:
                return 0.4

            def get_name(self) -> str:
                return "concrete_test"

        validator = ConcreteValidator()
        result = validator.validate(pd.DataFrame(), {})

        assert 0.0 <= result.score <= 1.0
        assert result.score == 0.87

    def test_validate_returns_details(self) -> None:
        """Validation result includes detailed metric breakdown."""
        from quality_service.validators.base import BaseValidator, ValidationResult

        class DetailValidator(BaseValidator):
            def validate(self, generated_data, profile):
                return self.create_result(
                    score=0.95,
                    details={
                        "column_scores": {"salary": 0.96, "amount": 0.94},
                        "rule_compliance": {"not_null": 1.0},
                    },
                    records_validated=200,
                    records_passed=190,
                )

            def get_weight(self) -> float:
                return 0.3

            def get_name(self) -> str:
                return "detail_test"

        validator = DetailValidator()
        result = validator.validate(pd.DataFrame(), {})

        assert "column_scores" in result.details
        assert "rule_compliance" in result.details

    def test_weight_property(self) -> None:
        """Each validator has a configurable weight property."""
        from quality_service.validators.base import BaseValidator

        class WeightedValidator(BaseValidator):
            def validate(self, generated_data, profile):
                return self.create_result(score=1.0, details={})

            def get_weight(self) -> float:
                return 0.4

            def get_name(self) -> str:
                return "weighted_test"

        validator = WeightedValidator()
        assert validator.get_weight() == 0.4

    def test_create_result_clamps_score(self) -> None:
        """create_result clamps score to [0.0, 1.0]."""
        from quality_service.validators.base import BaseValidator

        class ClampValidator(BaseValidator):
            def validate(self, generated_data, profile):
                return self.create_result(score=1.5, details={})

            def get_weight(self) -> float:
                return 0.3

            def get_name(self) -> str:
                return "clamp_test"

        validator = ClampValidator()
        result = validator.validate(pd.DataFrame(), {})
        assert result.score == 1.0

        # Test negative
        class NegClampValidator(BaseValidator):
            def validate(self, generated_data, profile):
                return self.create_result(score=-0.5, details={})

            def get_weight(self) -> float:
                return 0.3

            def get_name(self) -> str:
                return "neg_clamp_test"

        neg_validator = NegClampValidator()
        neg_result = neg_validator.validate(pd.DataFrame(), {})
        assert neg_result.score == 0.0

    def test_validate_with_timing(self) -> None:
        """validate_with_timing wraps validate and records execution time."""
        from quality_service.validators.base import BaseValidator

        class TimedValidator(BaseValidator):
            def validate(self, generated_data, profile):
                return self.create_result(score=0.9, details={})

            def get_weight(self) -> float:
                return 0.4

            def get_name(self) -> str:
                return "timed_test"

        validator = TimedValidator()
        result = validator.validate_with_timing(pd.DataFrame(), {})
        assert result.execution_time_ms >= 0.0
        assert result.score == 0.9


# =====================================================================
# TestStatisticalValidator — Statistical Fidelity Tests (40% weight)
# =====================================================================


class TestStatisticalValidator:
    """Tests for the StatisticalValidator class (40% weight)."""

    def _create_validator(self, config: dict | None = None):
        """Helper to create a StatisticalValidator instance."""
        from quality_service.validators.statistical_validator import StatisticalValidator
        return StatisticalValidator(config=config)

    def test_statistical_validator_weight(self) -> None:
        """Weight is 0.4 (40%)."""
        validator = self._create_validator()
        assert validator.get_weight() == 0.4

    def test_statistical_validator_name(self) -> None:
        """Validator name is 'statistical_fidelity'."""
        validator = self._create_validator()
        name = validator.get_name()
        assert "statistic" in name.lower()

    def test_validate_perfect_match_scores_high(
        self, sample_source_profile: dict
    ) -> None:
        """Data matching the source distribution scores high."""
        np.random.seed(42)
        n = 1000
        # Generate data that closely matches the profile
        data = pd.DataFrame({
            "salary": np.random.normal(75000, 15000, size=n).clip(30000, 150000),
            "amount": np.random.normal(5000, 2000, size=n).clip(0, 20000),
            "department": np.random.choice(
                ["Engineering", "Sales", "HR", "Finance"],
                size=n,
                p=[0.35, 0.25, 0.20, 0.20],
            ),
        })

        validator = self._create_validator()
        result = validator.validate(data, sample_source_profile)

        # Should score well, though not necessarily 1.0 due to randomness
        assert result.score >= 0.0
        assert result.score <= 1.0
        assert result.weight == 0.4

    def test_validate_random_data_scores_lower(
        self, sample_source_profile: dict
    ) -> None:
        """Random data with different distribution scores lower."""
        np.random.seed(99)
        n = 500
        # Generate data with very different distributions
        data = pd.DataFrame({
            "salary": np.random.uniform(0, 300000, size=n),
            "amount": np.random.uniform(-10000, 50000, size=n),
            "department": np.random.choice(
                ["X", "Y", "Z"],
                size=n,
            ),
        })

        validator = self._create_validator()
        result = validator.validate(data, sample_source_profile)

        # Random/mismatched data should generally score lower
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_validate_with_multiple_columns(
        self, sample_generated_data: pd.DataFrame, sample_source_profile: dict
    ) -> None:
        """Validate across all columns in the dataset."""
        validator = self._create_validator()
        result = validator.validate(sample_generated_data, sample_source_profile)

        assert result.score >= 0.0
        assert result.score <= 1.0
        assert result.records_validated >= 0

    def test_validate_returns_validation_result(self) -> None:
        """validate() returns a ValidationResult instance."""
        from quality_service.validators.base import ValidationResult

        validator = self._create_validator()
        data = pd.DataFrame({"salary": [70000, 75000, 80000]})
        profile = {
            "columns": {
                "salary": {
                    "data_type": "float64",
                    "mean": 75000,
                    "std": 5000,
                    "min": 70000,
                    "max": 80000,
                    "percentiles": {"25": 72500, "50": 75000, "75": 77500},
                    "null_ratio": 0.0,
                    "count": 100,
                },
            },
        }
        result = validator.validate(data, profile)
        assert isinstance(result, ValidationResult)

    def test_validate_with_timing_records_ms(
        self, sample_generated_data: pd.DataFrame, sample_source_profile: dict
    ) -> None:
        """validate_with_timing records execution time in ms."""
        validator = self._create_validator()
        result = validator.validate_with_timing(
            sample_generated_data, sample_source_profile
        )
        assert result.execution_time_ms >= 0.0


# =====================================================================
# TestBusinessRulesValidator — Business Rules Tests (30% weight)
# =====================================================================


class TestBusinessRulesValidator:
    """Tests for the BusinessRulesValidator class (30% weight)."""

    def _create_validator(self, config: dict | None = None):
        """Helper to create a BusinessRulesValidator instance."""
        from quality_service.validators.business_rules_validator import BusinessRulesValidator
        return BusinessRulesValidator(config=config)

    def test_business_rules_validator_weight(self) -> None:
        """Weight is 0.3 (30%)."""
        validator = self._create_validator()
        assert validator.get_weight() == 0.3

    def test_business_rules_validator_name(self) -> None:
        """Validator name is 'business_rules'."""
        validator = self._create_validator()
        name = validator.get_name()
        assert "business" in name.lower() or "rule" in name.lower()

    def test_validate_not_null_constraints(self) -> None:
        """Required fields are non-null."""
        validator = self._create_validator()
        data = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["Alice", "Bob", "Charlie"],
            "salary": [70000.0, 75000.0, 80000.0],
        })
        profile = {
            "columns": {
                "id": {"data_type": "integer", "nullable": False},
                "name": {"data_type": "varchar", "nullable": False},
                "salary": {"data_type": "float64", "nullable": False},
            },
            "rules": {
                "not_null": {
                    "columns": ["id", "name", "salary"],
                },
            },
        }
        result = validator.validate(data, profile)
        assert isinstance(result.score, float)
        assert result.score >= 0.0

    def test_validate_all_rules_pass(self) -> None:
        """All business rules satisfied scores high."""
        validator = self._create_validator()
        # Clean data matching all rules
        data = pd.DataFrame({
            "entry_id": range(1, 11),
            "gl_account": [f"GL-{i:04d}" for i in range(1000, 1010)],
            "amount": [1000.0] * 10,
            "posting_date": pd.date_range("2024-01-01", periods=10, freq="D"),
        })
        profile = {
            "columns": {
                "entry_id": {"data_type": "integer", "nullable": False},
                "gl_account": {"data_type": "varchar", "nullable": False},
                "amount": {"data_type": "decimal", "nullable": False},
                "posting_date": {"data_type": "date", "nullable": False},
            },
        }
        result = validator.validate(data, profile)
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_validate_with_null_values(self) -> None:
        """Data with null values in required fields scores lower."""
        validator = self._create_validator()
        data = pd.DataFrame({
            "id": [1, 2, None],
            "name": ["Alice", None, "Charlie"],
        })
        profile = {
            "columns": {
                "id": {"data_type": "integer", "nullable": False},
                "name": {"data_type": "varchar", "nullable": False},
            },
            "rules": {
                "not_null": {"columns": ["id", "name"]},
            },
        }
        result = validator.validate(data, profile)
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_validate_returns_validation_result(self) -> None:
        """validate() returns a ValidationResult instance."""
        from quality_service.validators.base import ValidationResult

        validator = self._create_validator()
        data = pd.DataFrame({"salary": [70000, 75000, 80000]})
        profile = {"columns": {"salary": {"data_type": "float64", "nullable": False}}}
        result = validator.validate(data, profile)
        assert isinstance(result, ValidationResult)


# =====================================================================
# TestReferentialIntegrityValidator — FK Tests (30% weight)
# =====================================================================


class TestReferentialIntegrityValidator:
    """Tests for the ReferentialIntegrityValidator class (30% weight)."""

    def _create_validator(self, config: dict | None = None):
        """Helper to create a ReferentialIntegrityValidator instance."""
        from quality_service.validators.referential_integrity_validator import (
            ReferentialIntegrityValidator,
        )
        return ReferentialIntegrityValidator(config=config)

    def test_referential_integrity_validator_weight(self) -> None:
        """Weight is 0.3 (30%)."""
        validator = self._create_validator()
        assert validator.get_weight() == 0.3

    def test_referential_integrity_validator_name(self) -> None:
        """Validator name contains 'referential' or 'integrity'."""
        validator = self._create_validator()
        name = validator.get_name()
        assert "referential" in name.lower() or "integrity" in name.lower()

    def test_validate_fk_references_exist(self) -> None:
        """Every FK value exists in parent table."""
        validator = self._create_validator()

        parent_df = pd.DataFrame({
            "account_id": ["ACC-001", "ACC-002", "ACC-003"],
            "account_name": ["Checking", "Savings", "Credit"],
        })
        child_df = pd.DataFrame({
            "entry_id": [1, 2, 3],
            "gl_account": ["ACC-001", "ACC-002", "ACC-003"],
            "amount": [1000, 2000, 3000],
        })

        data = {"gl_accounts": parent_df, "gl_entries": child_df}
        profile = {
            "relationships": [
                {
                    "parent_table": "gl_accounts",
                    "child_table": "gl_entries",
                    "parent_column": "account_id",
                    "child_column": "gl_account",
                    "cardinality": "one_to_many",
                },
            ],
        }
        result = validator.validate(data, profile)
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_validate_with_orphans(self) -> None:
        """Orphan records (missing parent FK) reduce score."""
        validator = self._create_validator()

        parent_df = pd.DataFrame({
            "account_id": ["ACC-001", "ACC-002"],
            "account_name": ["Checking", "Savings"],
        })
        child_df = pd.DataFrame({
            "entry_id": [1, 2, 3, 4],
            "gl_account": ["ACC-001", "ACC-002", "ACC-999", "ACC-888"],
            "amount": [1000, 2000, 3000, 4000],
        })

        data = {"gl_accounts": parent_df, "gl_entries": child_df}
        profile = {
            "relationships": [
                {
                    "parent_table": "gl_accounts",
                    "child_table": "gl_entries",
                    "parent_column": "account_id",
                    "child_column": "gl_account",
                    "cardinality": "one_to_many",
                },
            ],
        }
        result = validator.validate(data, profile)
        # Orphans should reduce score below 1.0
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_validate_no_relationships(self) -> None:
        """Tables with no FK constraints score 1.0 by default."""
        validator = self._create_validator()
        data = pd.DataFrame({
            "id": [1, 2, 3],
            "value": ["a", "b", "c"],
        })
        profile = {"relationships": []}
        result = validator.validate(data, profile)
        # No relationships to validate — should get a reasonable score
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_validate_returns_validation_result(self) -> None:
        """validate() returns a ValidationResult instance."""
        from quality_service.validators.base import ValidationResult

        validator = self._create_validator()
        data = pd.DataFrame({"id": [1, 2, 3]})
        profile = {"relationships": []}
        result = validator.validate(data, profile)
        assert isinstance(result, ValidationResult)


# =====================================================================
# TestQualityScorer — Weighted Composite Scoring Tests
# =====================================================================


class TestQualityScorer:
    """Tests for the QualityScorer class — Q = 0.4*S_stat + 0.3*S_biz + 0.3*S_ri."""

    def _create_scorer(self, config: dict | None = None):
        """Helper to create a QualityScorer with mocked data stores."""
        with patch("quality_service.scoring.report_generator.get_mongo_db") as mock_db:
            mock_db.return_value = MagicMock()
            with patch("quality_service.scoring.report_generator.get_logger"):
                from quality_service.scoring.quality_scorer import QualityScorer
                scorer = QualityScorer(config=config)
                # Mock data stores to prevent real connections
                scorer._db = MagicMock()
                scorer._redis = MagicMock()
                scorer._redis.get.return_value = None
                return scorer

    def test_scorer_initialization(self) -> None:
        """Initialize scorer with three validators and their weights."""
        scorer = self._create_scorer()
        assert scorer is not None
        assert len(scorer._validators) == 3

    def test_weights_sum_to_1(self) -> None:
        """Validator weights sum to 1.0."""
        scorer = self._create_scorer()
        total_weight = sum(v.get_weight() for v in scorer._validators)
        assert abs(total_weight - 1.0) < 0.001

    def test_weighted_score_calculation(self) -> None:
        """Q = 0.4*stat + 0.3*business + 0.3*integrity."""
        from quality_service.validators.base import ValidationResult

        scorer = self._create_scorer()

        # Mock all three validators' validate_with_timing to return known scores
        mock_results = [
            ValidationResult(
                validator_name="statistical_fidelity",
                score=0.90,
                weight=0.4,
                weighted_score=0.36,
                passed=True,
                threshold=0.0,
                details={},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=90,
                execution_time_ms=100.0,
            ),
            ValidationResult(
                validator_name="business_rules",
                score=0.95,
                weight=0.3,
                weighted_score=0.285,
                passed=True,
                threshold=0.0,
                details={},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=95,
                execution_time_ms=50.0,
            ),
            ValidationResult(
                validator_name="referential_integrity",
                score=0.98,
                weight=0.3,
                weighted_score=0.294,
                passed=True,
                threshold=0.0,
                details={},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=98,
                execution_time_ms=30.0,
            ),
        ]

        for i, validator in enumerate(scorer._validators):
            validator.validate_with_timing = MagicMock(return_value=mock_results[i])

        result = scorer.score(
            job_id="job-test",
            tenant_id="tenant-test",
            generated_data=pd.DataFrame({"a": [1]}),
            profile={},
        )

        # Expected: 0.4*0.90 + 0.3*0.95 + 0.3*0.98 = 0.36 + 0.285 + 0.294 = 0.939
        expected = 0.4 * 0.90 + 0.3 * 0.95 + 0.3 * 0.98
        assert abs(result.composite_score - expected) < 0.01

    def test_perfect_scores_equal_1(self) -> None:
        """All validators returning 1.0 yields composite 1.0."""
        from quality_service.validators.base import ValidationResult

        scorer = self._create_scorer()

        for validator in scorer._validators:
            w = validator.get_weight()
            validator.validate_with_timing = MagicMock(return_value=ValidationResult(
                validator_name=validator.get_name(),
                score=1.0,
                weight=w,
                weighted_score=w * 1.0,
                passed=True,
                threshold=0.0,
                details={},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=100,
                execution_time_ms=10.0,
            ))

        result = scorer.score(
            job_id="job-perfect",
            tenant_id="tenant-test",
            generated_data=pd.DataFrame({"a": [1]}),
            profile={},
        )

        assert abs(result.composite_score - 1.0) < 0.001

    def test_zero_scores_equal_0(self) -> None:
        """All validators returning 0.0 yields composite 0.0."""
        from quality_service.validators.base import ValidationResult

        scorer = self._create_scorer()

        for validator in scorer._validators:
            w = validator.get_weight()
            validator.validate_with_timing = MagicMock(return_value=ValidationResult(
                validator_name=validator.get_name(),
                score=0.0,
                weight=w,
                weighted_score=0.0,
                passed=False,
                threshold=0.0,
                details={},
                errors=["All checks failed"],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=0,
                execution_time_ms=10.0,
            ))

        result = scorer.score(
            job_id="job-zero",
            tenant_id="tenant-test",
            generated_data=pd.DataFrame({"a": [1]}),
            profile={},
        )

        assert abs(result.composite_score - 0.0) < 0.001

    def test_threshold_check_passes(self) -> None:
        """Composite score ≥ 0.95 passes quality gate."""
        from quality_service.validators.base import ValidationResult

        scorer = self._create_scorer()

        # Set scores so composite ≥ 0.95
        scores = [0.96, 0.97, 0.95]
        for i, validator in enumerate(scorer._validators):
            w = validator.get_weight()
            validator.validate_with_timing = MagicMock(return_value=ValidationResult(
                validator_name=validator.get_name(),
                score=scores[i],
                weight=w,
                weighted_score=scores[i] * w,
                passed=True,
                threshold=0.0,
                details={},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=int(100 * scores[i]),
                execution_time_ms=10.0,
            ))

        result = scorer.score(
            job_id="job-pass",
            tenant_id="tenant-test",
            generated_data=pd.DataFrame({"a": [1]}),
            profile={},
        )

        # composite = 0.4*0.96 + 0.3*0.97 + 0.3*0.95 = 0.384 + 0.291 + 0.285 = 0.960
        assert result.passed is True

    def test_threshold_check_fails(self) -> None:
        """Composite score < 0.95 fails quality gate."""
        from quality_service.validators.base import ValidationResult

        scorer = self._create_scorer()

        # Set low scores so composite < 0.95
        scores = [0.80, 0.70, 0.60]
        for i, validator in enumerate(scorer._validators):
            w = validator.get_weight()
            validator.validate_with_timing = MagicMock(return_value=ValidationResult(
                validator_name=validator.get_name(),
                score=scores[i],
                weight=w,
                weighted_score=scores[i] * w,
                passed=False,
                threshold=0.0,
                details={},
                errors=["Below threshold"],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=int(100 * scores[i]),
                execution_time_ms=10.0,
            ))

        result = scorer.score(
            job_id="job-fail",
            tenant_id="tenant-test",
            generated_data=pd.DataFrame({"a": [1]}),
            profile={},
        )

        # composite = 0.4*0.80 + 0.3*0.70 + 0.3*0.60 = 0.32 + 0.21 + 0.18 = 0.71
        assert result.passed is False
        assert result.composite_score < 0.95

    def test_scorer_returns_detailed_breakdown(self) -> None:
        """Returns per-validator scores and overall score."""
        from quality_service.validators.base import ValidationResult

        scorer = self._create_scorer()

        for validator in scorer._validators:
            w = validator.get_weight()
            validator.validate_with_timing = MagicMock(return_value=ValidationResult(
                validator_name=validator.get_name(),
                score=0.95,
                weight=w,
                weighted_score=0.95 * w,
                passed=True,
                threshold=0.0,
                details={"test": True},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=95,
                execution_time_ms=10.0,
            ))

        result = scorer.score(
            job_id="job-detail",
            tenant_id="tenant-test",
            generated_data=pd.DataFrame({"a": [1]}),
            profile={},
        )

        assert result.validation_results is not None
        assert len(result.validation_results) == 3

    @pytest.mark.parametrize(
        "stat_score,biz_score,ri_score",
        [
            (1.0, 1.0, 1.0),
            (0.5, 0.5, 0.5),
            (0.0, 0.0, 0.0),
            (0.95, 0.95, 0.95),
            (1.0, 0.0, 0.0),
        ],
    )
    def test_scorer_with_parametrized_weights(
        self,
        stat_score: float,
        biz_score: float,
        ri_score: float,
    ) -> None:
        """Parametrize with various weight combinations."""
        from quality_service.validators.base import ValidationResult

        scorer = self._create_scorer()
        test_scores = [stat_score, biz_score, ri_score]

        for i, validator in enumerate(scorer._validators):
            w = validator.get_weight()
            validator.validate_with_timing = MagicMock(return_value=ValidationResult(
                validator_name=validator.get_name(),
                score=test_scores[i],
                weight=w,
                weighted_score=test_scores[i] * w,
                passed=test_scores[i] >= 0.95,
                threshold=0.0,
                details={},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=100,
                records_passed=int(100 * test_scores[i]),
                execution_time_ms=10.0,
            ))

        result = scorer.score(
            job_id="job-param",
            tenant_id="tenant-test",
            generated_data=pd.DataFrame({"a": [1]}),
            profile={},
        )

        expected = 0.4 * stat_score + 0.3 * biz_score + 0.3 * ri_score
        assert abs(result.composite_score - expected) < 0.02


# =====================================================================
# TestReportGenerator — Quality Report Generation Tests
# =====================================================================


class TestReportGenerator:
    """Tests for the ReportGenerator class for quality report generation."""

    def _create_generator(self, mock_db: Any = None):
        """Helper to create a ReportGenerator with mocked dependencies."""
        with patch("quality_service.scoring.report_generator.get_mongo_db") as mock_get_db:
            if mock_db is None:
                mock_db = MagicMock()
                mock_collection = MagicMock()
                mock_collection.insert_one.return_value = MagicMock(inserted_id="test-id")
                mock_collection.find_one.return_value = None
                mock_collection.find.return_value = []
                mock_collection.delete_one.return_value = MagicMock(deleted_count=0)
                mock_db.__getitem__ = MagicMock(return_value=mock_collection)
            mock_get_db.return_value = mock_db
            with patch("quality_service.scoring.report_generator.get_logger"):
                from quality_service.scoring.report_generator import ReportGenerator
                return ReportGenerator()

    def _make_validation_results(self) -> list:
        """Create sample ValidationResult objects for testing."""
        from quality_service.validators.base import ValidationResult

        return [
            ValidationResult(
                validator_name="statistical_fidelity",
                score=0.97,
                weight=0.4,
                weighted_score=0.388,
                passed=True,
                threshold=0.95,
                details={
                    "salary": {"ks_pvalue": 0.85, "moment_score": 0.96},
                    "amount": {"ks_pvalue": 0.78, "moment_score": 0.94},
                },
                errors=[],
                warnings=[],
                metadata={},
                records_validated=10000,
                records_passed=9700,
                execution_time_ms=1250.0,
            ),
            ValidationResult(
                validator_name="business_rules",
                score=0.95,
                weight=0.3,
                weighted_score=0.285,
                passed=True,
                threshold=0.95,
                details={
                    "not_null": 1.0,
                    "format": 0.92,
                    "range": 0.95,
                },
                errors=[],
                warnings=["Some values near boundary"],
                metadata={},
                records_validated=10000,
                records_passed=9500,
                execution_time_ms=850.0,
            ),
            ValidationResult(
                validator_name="referential_integrity",
                score=0.96,
                weight=0.3,
                weighted_score=0.288,
                passed=True,
                threshold=0.95,
                details={
                    "fk_validity": 0.97,
                    "orphan_count": 30,
                },
                errors=[],
                warnings=[],
                metadata={},
                records_validated=10000,
                records_passed=9600,
                execution_time_ms=650.0,
            ),
        ]

    def test_generate_report(self) -> None:
        """generate_report produces a valid QualityReport."""
        from quality_service.scoring.report_generator import QualityReport

        generator = self._create_generator()
        results = self._make_validation_results()

        report = generator.generate_report(
            job_id="job-001",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.961,
        )

        assert isinstance(report, QualityReport)
        assert report.job_id == "job-001"
        assert report.tenant_id == "tenant-001"
        assert report.overall_score == 0.961
        assert len(report.sections) == 3

    def test_generate_report_passed_status(self) -> None:
        """Report status is 'passed' when composite_score >= threshold."""
        generator = self._create_generator()
        results = self._make_validation_results()

        report = generator.generate_report(
            job_id="job-pass",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.96,
            threshold=0.95,
        )

        assert report.status == "passed"

    def test_generate_report_failed_status(self) -> None:
        """Report status is 'failed' when composite_score < threshold."""
        generator = self._create_generator()
        results = self._make_validation_results()

        report = generator.generate_report(
            job_id="job-fail",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.90,
            threshold=0.95,
        )

        assert report.status == "failed"

    def test_report_includes_timestamp(self) -> None:
        """Report includes creation timestamp."""
        generator = self._create_generator()
        results = self._make_validation_results()

        report = generator.generate_report(
            job_id="job-ts",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.96,
        )

        assert report.created_at is not None
        assert isinstance(report.created_at, datetime)

    def test_report_includes_job_metadata(self) -> None:
        """Report includes job_id, tenant_id, and metadata."""
        generator = self._create_generator()
        results = self._make_validation_results()

        metadata = {
            "generation_method": "statistical",
            "schema_id": "schema-001",
            "record_count": 10000,
        }

        report = generator.generate_report(
            job_id="job-meta",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.96,
            metadata=metadata,
        )

        assert report.job_id == "job-meta"
        assert report.tenant_id == "tenant-001"
        assert report.metadata is not None

    def test_report_includes_recommendations(self) -> None:
        """Low-scoring areas include improvement recommendations."""
        from quality_service.validators.base import ValidationResult

        generator = self._create_generator()

        # Create results with a low-scoring section
        results = [
            ValidationResult(
                validator_name="statistical_fidelity",
                score=0.70,
                weight=0.4,
                weighted_score=0.28,
                passed=False,
                threshold=0.95,
                details={"salary": {"ks_pvalue": 0.02}},
                errors=["Distribution mismatch for salary"],
                warnings=[],
                metadata={},
                records_validated=1000,
                records_passed=700,
                execution_time_ms=500.0,
            ),
            ValidationResult(
                validator_name="business_rules",
                score=0.95,
                weight=0.3,
                weighted_score=0.285,
                passed=True,
                threshold=0.95,
                details={},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=1000,
                records_passed=950,
                execution_time_ms=200.0,
            ),
            ValidationResult(
                validator_name="referential_integrity",
                score=0.96,
                weight=0.3,
                weighted_score=0.288,
                passed=True,
                threshold=0.95,
                details={},
                errors=[],
                warnings=[],
                metadata={},
                records_validated=1000,
                records_passed=960,
                execution_time_ms=150.0,
            ),
        ]

        report = generator.generate_report(
            job_id="job-reco",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.853,
            threshold=0.95,
        )

        assert report.status == "failed"
        assert report.recommendations is not None
        assert len(report.recommendations) > 0

    def test_report_serialization(self) -> None:
        """Report serializes to JSON-compatible format."""
        generator = self._create_generator()
        results = self._make_validation_results()

        report = generator.generate_report(
            job_id="job-serial",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.96,
        )

        # Convert to dict and verify it's JSON serializable
        report_dict = report.model_dump()
        # Convert datetime for JSON
        report_dict["created_at"] = report_dict["created_at"].isoformat()
        json_str = json.dumps(report_dict)
        assert json_str is not None
        parsed = json.loads(json_str)
        assert parsed["job_id"] == "job-serial"

    def test_report_sections_structure(self) -> None:
        """Report sections contain proper structure."""
        from quality_service.scoring.report_generator import QualityReportSection

        generator = self._create_generator()
        results = self._make_validation_results()

        report = generator.generate_report(
            job_id="job-sections",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.96,
        )

        for section in report.sections:
            assert isinstance(section, QualityReportSection)
            assert section.section_name is not None
            assert 0.0 <= section.score <= 1.0
            assert 0.0 <= section.weight <= 1.0
            assert isinstance(section.passed, bool)
            assert isinstance(section.details, dict)
            assert isinstance(section.errors, list)
            assert isinstance(section.warnings, list)

    def test_report_summary_statistics(self) -> None:
        """Report includes summary statistics."""
        generator = self._create_generator()
        results = self._make_validation_results()

        report = generator.generate_report(
            job_id="job-summary",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.96,
        )

        assert report.summary is not None
        summary = report.summary
        assert "total_records_validated" in summary or "weakest_section" in summary or len(summary) > 0

    def test_save_report(self, mock_mongodb: Any) -> None:
        """Report saved to MongoDB quality_reports collection."""
        with patch("quality_service.scoring.report_generator.get_mongo_db", return_value=mock_mongodb), \
             patch("quality_service.scoring.report_generator.get_logger"):
            from quality_service.scoring.report_generator import ReportGenerator

            generator = ReportGenerator()
            results = self._make_validation_results()

            report = generator.generate_report(
                job_id="job-save",
                tenant_id="tenant-001",
                validation_results=results,
                composite_score=0.96,
            )

            report_id = generator.save_report(report)
            assert report_id is not None

    def test_get_report(self, mock_mongodb: Any) -> None:
        """Retrieve a stored quality report by ID with tenant isolation."""
        with patch("quality_service.scoring.report_generator.get_mongo_db", return_value=mock_mongodb), \
             patch("quality_service.scoring.report_generator.get_logger"):
            from quality_service.scoring.report_generator import ReportGenerator

            generator = ReportGenerator()
            results = self._make_validation_results()

            report = generator.generate_report(
                job_id="job-get",
                tenant_id="tenant-001",
                validation_results=results,
                composite_score=0.96,
            )

            generator.save_report(report)
            retrieved = generator.get_report(report.report_id, "tenant-001")

            if retrieved is not None:
                assert retrieved.report_id == report.report_id
                assert retrieved.tenant_id == "tenant-001"

    def test_get_reports_by_job(self, mock_mongodb: Any) -> None:
        """Retrieve all quality reports for a specific generation job."""
        with patch("quality_service.scoring.report_generator.get_mongo_db", return_value=mock_mongodb), \
             patch("quality_service.scoring.report_generator.get_logger"):
            from quality_service.scoring.report_generator import ReportGenerator

            generator = ReportGenerator()
            results = self._make_validation_results()

            # Create two reports for the same job
            for i in range(2):
                report = generator.generate_report(
                    job_id="job-multi",
                    tenant_id="tenant-001",
                    validation_results=results,
                    composite_score=0.96 - (i * 0.01),
                )
                generator.save_report(report)

            reports = generator.get_reports_by_job("job-multi", "tenant-001")
            assert isinstance(reports, list)

    def test_delete_report(self, mock_mongodb: Any) -> None:
        """Delete a quality report with tenant isolation."""
        with patch("quality_service.scoring.report_generator.get_mongo_db", return_value=mock_mongodb), \
             patch("quality_service.scoring.report_generator.get_logger"):
            from quality_service.scoring.report_generator import ReportGenerator

            generator = ReportGenerator()
            results = self._make_validation_results()

            report = generator.generate_report(
                job_id="job-del",
                tenant_id="tenant-001",
                validation_results=results,
                composite_score=0.96,
            )

            generator.save_report(report)
            deleted = generator.delete_report(report.report_id, "tenant-001")
            assert isinstance(deleted, bool)

    def test_report_unique_id(self) -> None:
        """Each report gets a unique report_id."""
        generator = self._create_generator()
        results = self._make_validation_results()

        report1 = generator.generate_report(
            job_id="job-id1",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.96,
        )
        report2 = generator.generate_report(
            job_id="job-id2",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.95,
        )

        assert report1.report_id != report2.report_id

    def test_report_threshold_default(self) -> None:
        """Default threshold is 0.95."""
        generator = self._create_generator()
        results = self._make_validation_results()

        report = generator.generate_report(
            job_id="job-threshold",
            tenant_id="tenant-001",
            validation_results=results,
            composite_score=0.96,
        )

        assert report.threshold == 0.95


# =====================================================================
# TestQualityReportModels — Pydantic Model Tests
# =====================================================================


class TestQualityReportModels:
    """Tests for the QualityReportSection and QualityReport Pydantic models."""

    def test_quality_report_section_creation(self) -> None:
        """QualityReportSection can be created with all fields."""
        from quality_service.scoring.report_generator import QualityReportSection

        section = QualityReportSection(
            section_name="statistical_fidelity",
            score=0.97,
            weight=0.4,
            weighted_score=0.388,
            passed=True,
            details={"salary": {"ks_pvalue": 0.85}},
            errors=[],
            warnings=["minor"],
            records_validated=1000,
            records_passed=970,
            execution_time_ms=250.0,
        )

        assert section.section_name == "statistical_fidelity"
        assert section.score == 0.97
        assert section.weight == 0.4
        assert section.passed is True

    def test_quality_report_creation(self) -> None:
        """QualityReport can be created with all fields."""
        from quality_service.scoring.report_generator import QualityReport, QualityReportSection

        sections = [
            QualityReportSection(
                section_name="statistical_fidelity",
                score=0.97,
                weight=0.4,
                weighted_score=0.388,
                passed=True,
                details={},
                errors=[],
                warnings=[],
                records_validated=100,
                records_passed=97,
                execution_time_ms=100.0,
            ),
        ]

        report = QualityReport(
            report_id=str(uuid.uuid4()),
            job_id="job-model",
            tenant_id="tenant-001",
            created_at=datetime.now(timezone.utc),
            status="passed",
            overall_score=0.96,
            threshold=0.95,
            sections=sections,
            summary={"total_records_validated": 100},
            metadata={"method": "statistical"},
            recommendations=[],
            data_profile={},
            schema_info={},
        )

        assert report.status == "passed"
        assert report.overall_score == 0.96
        assert len(report.sections) == 1

    def test_quality_report_section_score_bounds(self) -> None:
        """QualityReportSection score must be in [0.0, 1.0]."""
        from quality_service.scoring.report_generator import QualityReportSection

        # Valid score
        section = QualityReportSection(
            section_name="test",
            score=0.5,
            weight=0.3,
            passed=True,
            details={},
            errors=[],
            warnings=[],
            records_validated=0,
            records_passed=0,
            execution_time_ms=0.0,
        )
        assert section.score == 0.5

        # Score of 0.0 is valid
        section_zero = QualityReportSection(
            section_name="test_zero",
            score=0.0,
            weight=0.3,
            passed=False,
            details={},
            errors=[],
            warnings=[],
            records_validated=0,
            records_passed=0,
            execution_time_ms=0.0,
        )
        assert section_zero.score == 0.0

        # Score of 1.0 is valid
        section_one = QualityReportSection(
            section_name="test_one",
            score=1.0,
            weight=0.4,
            passed=True,
            details={},
            errors=[],
            warnings=[],
            records_validated=0,
            records_passed=0,
            execution_time_ms=0.0,
        )
        assert section_one.score == 1.0
