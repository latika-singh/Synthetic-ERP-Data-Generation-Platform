"""Business rules compliance validator for the Quality Service.

Implements the **30 % weighted** business-rules component of the composite
quality scoring model::

    Q = 0.4 * S_statistical + 0.3 * S_business_rules + 0.3 * S_referential

Validates that generated synthetic data adheres to ERP-specific business
rules across five rule categories:

1. **Format rules** — regex-based field-format constraints (GL accounts,
   employee IDs, order numbers, material numbers).
2. **Cross-field rules** — inter-field dependencies within records
   (debit/credit balance, price x quantity = total).
3. **Domain rules** — value membership in allowed sets (ISO 4217 currency
   codes, ISO 3166 country codes, status enumerations).
4. **Temporal rules** — chronological ordering constraints (hire before
   termination, order before delivery, posting within fiscal period).
5. **Conditional rules** — if-then logic (credit memo → negative amount,
   terminated → termination date set).

Four ERP modules are supported (per constraint C-005):
    - Financial Accounting
    - Human Resources
    - Sales & Distribution
    - Material Management

Extends :class:`BaseValidator` from the ``validators.base`` module,
conforming to the Strategy pattern used by :class:`QualityScorer`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, field_validator

from quality_service.validators.base import BaseValidator, ValidationResult
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level structured logger for business-rule validation events.
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Compiled regex patterns — cached at module level for performance.
# ---------------------------------------------------------------------------
_GL_ACCOUNT_PATTERN: re.Pattern = re.compile(
    r"^\d{4}[.\-]?\d{4}[.\-]?\d{2,4}$"
)
_EMPLOYEE_ID_PATTERN: re.Pattern = re.compile(r"^EMP[\-]?\d{5,8}$")
_ORDER_NUMBER_PATTERN: re.Pattern = re.compile(r"^(SO|ORD)[\-]?\d{6,10}$")
_MATERIAL_NUMBER_PATTERN: re.Pattern = re.compile(r"^(MAT|M)[\-]?\d{6,10}$")
_PO_NUMBER_PATTERN: re.Pattern = re.compile(r"^(PO|P)[\-]?\d{6,10}$")
_DOCUMENT_NUMBER_PATTERN: re.Pattern = re.compile(
    r"^(DOC|FI|JE)[\-]?\d{6,12}$"
)
_CURRENCY_CODE_PATTERN: re.Pattern = re.compile(r"^[A-Z]{3}$")
_COUNTRY_CODE_PATTERN: re.Pattern = re.compile(r"^[A-Z]{2}$")
_VENDOR_ID_PATTERN: re.Pattern = re.compile(r"^(VND|V)[\-]?\d{5,10}$")
_CUSTOMER_ID_PATTERN: re.Pattern = re.compile(r"^(CUST|C)[\-]?\d{5,10}$")

# ---------------------------------------------------------------------------
# Default category weights for rule-score aggregation.
# ---------------------------------------------------------------------------
_DEFAULT_CATEGORY_WEIGHTS: dict[str, float] = {
    "format": 0.20,
    "cross_field": 0.25,
    "domain": 0.20,
    "temporal": 0.20,
    "conditional": 0.15,
}

# ---------------------------------------------------------------------------
# Common ISO 4217 currency codes used in ERP systems.
# ---------------------------------------------------------------------------
_VALID_CURRENCY_CODES: frozenset[str] = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "CNY",
        "INR", "BRL", "KRW", "MXN", "SGD", "HKD", "NOK", "SEK",
        "DKK", "NZD", "ZAR", "RUB", "THB", "TWD", "PLN", "CZK",
        "HUF", "TRY", "ILS", "PHP", "MYR", "IDR", "ARS", "CLP",
        "COP", "PEN", "SAR", "AED", "EGP", "NGN", "KES", "GHS",
        "BHD", "KWD", "QAR", "OMR",
    }
)

# ---------------------------------------------------------------------------
# Supported ERP modules.
# ---------------------------------------------------------------------------
_SUPPORTED_MODULES: frozenset[str] = frozenset(
    {
        "financial_accounting",
        "human_resources",
        "sales_distribution",
        "material_management",
    }
)


# ---------------------------------------------------------------------------
# Pydantic domain-constraint models for structured validation.
# ---------------------------------------------------------------------------


class CurrencyCodeConstraint(BaseModel):
    """Pydantic model validating ISO 4217 currency codes.

    Used by :meth:`BusinessRulesValidator._validate_domain_rules` to verify
    that currency-code fields contain recognised three-letter codes.
    """

    code: str

    @field_validator("code")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        """Ensure the value is a known ISO 4217 currency code."""
        upper = value.strip().upper()
        if upper not in _VALID_CURRENCY_CODES:
            raise ValueError(f"Invalid ISO 4217 currency code: {value}")
        return upper


class CountryCodeConstraint(BaseModel):
    """Pydantic model validating ISO 3166-1 alpha-2 country codes.

    Uses :func:`re.fullmatch` to verify the two-uppercase-letter pattern
    before accepting the code.
    """

    code: str

    @field_validator("code")
    @classmethod
    def validate_country(cls, value: str) -> str:
        """Ensure the value matches the ISO 3166-1 alpha-2 format."""
        upper = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", upper):
            raise ValueError(
                f"Invalid ISO 3166-1 alpha-2 country code: {value}"
            )
        return upper


class DomainValueConstraint(BaseModel):
    """Generic Pydantic model for validating a value against an allowed set.

    Provides a reusable ``is_valid()`` check that callers invoke after
    :meth:`model_validate` to confirm membership.
    """

    value: str
    allowed_values: list[str]

    @field_validator("value")
    @classmethod
    def normalise_value(cls, v: str) -> str:
        """Strip leading/trailing whitespace from the value."""
        return v.strip()

    def is_valid(self) -> bool:
        """Return ``True`` when the value belongs to the allowed set."""
        return self.value in self.allowed_values


# ---------------------------------------------------------------------------
# BusinessRulesValidator — 30 % weighted component of quality scoring
# ---------------------------------------------------------------------------


class BusinessRulesValidator(BaseValidator):
    """Validates generated data against ERP-specific business rules.

    Implements the **30 %** weighted business-rules compliance component
    of the composite quality scoring model::

        Q = 0.4 * S_statistical + 0.3 * S_business_rules + 0.3 * S_ref_integrity

    Extends :class:`BaseValidator` (Strategy pattern) so that
    :class:`QualityScorer` can invoke business-rule validation
    polymorphically alongside statistical and referential-integrity
    validators.

    Five rule categories are evaluated:

    1. **Format** — regex field-format constraints.
    2. **Cross-field** — inter-field dependencies.
    3. **Domain** — value-set membership.
    4. **Temporal** — chronological ordering.
    5. **Conditional** — if-then logic.

    Four ERP modules are supported (per constraint C-005):
        - Financial Accounting
        - Human Resources
        - Sales & Distribution
        - Material Management

    Args:
        config: Optional configuration dictionary. Recognised keys:

            - ``minimum_threshold`` (float): Minimum pass score (default 0.95).
            - ``category_weights`` (dict): Per-category weight overrides.
            - ``custom_rules`` (dict): Additional rules keyed by module name.

    Example::

        validator = BusinessRulesValidator(config={"minimum_threshold": 0.90})
        result = validator.validate(df, profile={"module": "financial_accounting"})
        assert result.passed
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.weight: float = 0.3

        # Per-category weights for score aggregation (configurable).
        self._category_weights: dict[str, float] = dict(
            self.config.get("category_weights", _DEFAULT_CATEGORY_WEIGHTS)
        )

        # Compiled-regex cache: pattern_string → re.Pattern.
        self._compiled_patterns: dict[str, re.Pattern] = {}

        # Rule registry: module_name → list[rule_dict].
        self._rule_registry: dict[str, list[dict[str, Any]]] = {}

        # Register built-in rules for every supported ERP module.
        self._register_all_module_rules()

        # Merge optional custom rules supplied via ``config``.
        custom_rules: dict[str, list[dict[str, Any]]] = self.config.get(
            "custom_rules", {}
        )
        for module_name, rules in custom_rules.items():
            existing = self._rule_registry.get(module_name, [])
            existing.extend(rules)
            self._rule_registry[module_name] = existing

        self.logger.info(
            "business_rules_validator_initialized",
            modules_registered=list(self._rule_registry.keys()),
            total_rules=sum(
                len(r) for r in self._rule_registry.values()
            ),
            category_weights=self._category_weights,
        )

    # ------------------------------------------------------------------
    # Abstract-method implementations (BaseValidator contract)
    # ------------------------------------------------------------------

    def validate(
        self,
        generated_data: pd.DataFrame | dict[str, pd.DataFrame],
        profile: dict[str, Any],
    ) -> ValidationResult:
        """Run all business-rule checks against *generated_data*.

        Identifies the ERP module context from the *profile* metadata,
        loads the applicable rule set, executes each rule category, and
        aggregates per-category pass rates into a composite score.

        Args:
            generated_data: A single :class:`~pandas.DataFrame` or a
                mapping of table names to DataFrames for multi-table
                validation.
            profile: Schema / statistical profile metadata.  Should
                contain a ``"module"`` or ``"erp_module"`` key identifying
                the ERP module.

        Returns:
            A fully populated :class:`ValidationResult`.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Resolve the ERP module name from the profile dictionary.
        module_name = self._resolve_module_name(profile)

        # Multi-table input: validate each table then aggregate.
        if isinstance(generated_data, dict):
            return self._validate_multi_table(
                generated_data, profile, module_name, errors, warnings,
            )

        # --- Single-table validation path ---
        data: pd.DataFrame = generated_data
        total_records: int = data.shape[0]

        if total_records == 0:
            self.logger.warning(
                "empty_dataset_received", module=module_name,
            )
            return self.create_result(
                score=1.0,
                details={"message": "Empty dataset — no rules to validate"},
                warnings=["No records to validate"],
                records_validated=0,
                records_passed=0,
            )

        # Retrieve rules grouped by category for the target module.
        categorised_rules = self._get_module_rules(module_name)

        # Execute each rule category and collect scores.
        rule_scores: dict[str, float | None] = {}
        rule_details: dict[str, Any] = {}
        total_passed: int = total_records

        for category, rules in categorised_rules.items():
            if not rules:
                self.logger.debug(
                    "no_rules_for_category",
                    category=category,
                    module=module_name,
                )
                continue

            try:
                score, detail, cat_errors = self._execute_category(
                    data, rules, category,
                )
                rule_scores[category] = score
                rule_details[category] = detail
                errors.extend(cat_errors)

                # Track worst-case passing record count.
                passed_count = int(score * total_records)
                total_passed = min(total_passed, passed_count)

                self.logger.info(
                    "category_validation_complete",
                    category=category,
                    module=module_name,
                    score=round(score, 4),
                    rules_evaluated=len(rules),
                )
            except Exception as exc:
                rule_scores[category] = 0.0
                rule_details[category] = {"error": str(exc)}
                errors.append(
                    f"Category '{category}' validation failed: {exc}"
                )
                self.logger.error(
                    "category_validation_error",
                    category=category,
                    module=module_name,
                    error=str(exc),
                    exc_info=True,
                )

        # Aggregate per-category scores into composite business-rule score.
        composite_score = self._aggregate_rule_scores(rule_scores)

        self.logger.info(
            "business_rules_validation_complete",
            module=module_name,
            composite_score=round(composite_score, 4),
            category_scores={
                k: round(v, 4)
                for k, v in rule_scores.items()
                if v is not None
            },
            records_validated=total_records,
            passed=composite_score >= self.minimum_threshold,
        )

        return self.create_result(
            score=composite_score,
            details={
                "module": module_name,
                "category_scores": {
                    k: round(v, 4)
                    for k, v in rule_scores.items()
                    if v is not None
                },
                "category_details": rule_details,
            },
            errors=errors,
            warnings=warnings,
            records_validated=total_records,
            records_passed=total_passed,
        )

    def get_weight(self) -> float:
        """Return ``0.3`` — the business-rules weight in the composite score."""
        return 0.3

    def get_name(self) -> str:
        """Return the human-readable validator identifier."""
        return "business_rules"

    # ------------------------------------------------------------------
    # Multi-table handling
    # ------------------------------------------------------------------

    def _validate_multi_table(
        self,
        tables: dict[str, pd.DataFrame],
        profile: dict[str, Any],
        module_name: str,
        errors: list[str],
        warnings: list[str],
    ) -> ValidationResult:
        """Validate multiple tables and aggregate their scores.

        Each table is validated independently via :meth:`validate`; the
        composite score is the arithmetic mean across tables computed with
        :func:`numpy.mean`.
        """
        table_scores: list[float] = []
        table_details: dict[str, Any] = {}
        total_records = 0
        total_passed = 0

        for table_name, df in tables.items():
            table_profile: dict[str, Any] = dict(profile)
            table_profile["table_name"] = table_name

            sub_result: ValidationResult = self.validate(df, table_profile)
            table_scores.append(sub_result.score)
            table_details[table_name] = sub_result.details
            total_records += sub_result.records_validated
            total_passed += sub_result.records_passed
            errors.extend(sub_result.errors)
            warnings.extend(sub_result.warnings)

            self.logger.info(
                "table_validation_complete",
                table=table_name,
                score=round(sub_result.score, 4),
                passed=sub_result.passed,
            )

        composite = float(np.mean(table_scores)) if table_scores else 0.0

        return self.create_result(
            score=composite,
            details={
                "module": module_name,
                "table_scores": {
                    name: round(sc, 4)
                    for name, sc in zip(tables.keys(), table_scores, strict=True)
                },
                "table_details": table_details,
            },
            errors=errors,
            warnings=warnings,
            records_validated=total_records,
            records_passed=total_passed,
        )

    # ------------------------------------------------------------------
    # Category dispatch
    # ------------------------------------------------------------------

    def _execute_category(
        self,
        data: pd.DataFrame,
        rules: list[dict[str, Any]],
        category: str,
    ) -> tuple[float, dict[str, Any], list[str]]:
        """Dispatch validation to the correct category handler."""
        dispatch = {
            "format": self._validate_format_rules,
            "cross_field": self._validate_cross_field_rules,
            "domain": self._validate_domain_rules,
            "temporal": self._validate_temporal_rules,
            "conditional": self._validate_conditional_rules,
        }
        handler = dispatch.get(category)
        if handler is None:
            return 1.0, {"message": f"Unknown category: {category}"}, []
        return handler(data, rules)

    # ------------------------------------------------------------------
    # 1. Format Rules
    # ------------------------------------------------------------------

    def _validate_format_rules(
        self,
        data: pd.DataFrame,
        rules: list[dict[str, Any]],
    ) -> tuple[float, dict[str, Any], list[str]]:
        """Validate field-format constraints via compiled regex patterns.

        Uses :meth:`pandas.Series.str.match` for vectorised regex
        evaluation.  Missing columns are logged as warnings and skipped.

        Returns:
            ``(pass_rate, details_dict, errors_list)``
        """
        if not rules:
            return 1.0, {"message": "No format rules defined"}, []

        rule_results: dict[str, Any] = {}
        errors: list[str] = []
        pass_rates: list[float] = []
        total_records: int = data.shape[0]

        for rule in rules:
            rule_name: str = rule.get("name", "unknown_format_rule")
            field: str = rule.get("field", "")
            pattern_str: str = rule.get("pattern", "")

            if field not in data.columns:
                rule_results[rule_name] = {
                    "status": "skipped",
                    "reason": f"Column '{field}' not found",
                }
                continue

            # Compile / retrieve the cached regex pattern.
            compiled = self._get_compiled_pattern(pattern_str)

            # Coerce to string and fill NaN with empty string.
            series = data[field].astype(str).fillna("")

            # Vectorised pattern matching via pd.Series.str.match().
            matches: pd.Series = series.str.match(compiled.pattern)
            passed: int = int(matches.sum())
            rate = passed / total_records if total_records > 0 else 0.0
            pass_rates.append(rate)

            rule_results[rule_name] = {
                "field": field,
                "pattern": pattern_str,
                "records_passed": passed,
                "records_total": total_records,
                "pass_rate": round(rate, 4),
            }

            if rate < 1.0:
                failed_count = total_records - passed
                errors.append(
                    f"Format rule '{rule_name}': {failed_count}/{total_records}"
                    f" records failed pattern '{pattern_str}'"
                    f" on field '{field}'"
                )

        overall = float(np.mean(pass_rates)) if pass_rates else 1.0
        return overall, rule_results, errors

    # ------------------------------------------------------------------
    # 2. Cross-Field Rules
    # ------------------------------------------------------------------

    def _validate_cross_field_rules(
        self,
        data: pd.DataFrame,
        rules: list[dict[str, Any]],
    ) -> tuple[float, dict[str, Any], list[str]]:
        """Validate inter-field relationships within the same record.

        Supported *rule_type* values:

        - ``balance`` — field_a ≈ field_b (e.g. debit ≈ credit).
        - ``less_than`` — field_a < field_b.
        - ``less_than_or_equal`` — field_a ≤ field_b.
        - ``product_equals`` — field_a x field_b ~ target.
        - ``sum_equals`` — Σ source_fields ≈ target_field.
        - ``mutual_exclusive_nonzero`` — at most one of two fields ≠ 0.

        Returns:
            ``(pass_rate, details_dict, errors_list)``
        """
        if not rules:
            return 1.0, {"message": "No cross-field rules defined"}, []

        rule_results: dict[str, Any] = {}
        errors: list[str] = []
        pass_rates: list[float] = []
        total_records: int = data.shape[0]

        for rule in rules:
            rule_name: str = rule.get("name", "unknown_cross_field_rule")
            rule_type: str = rule.get("rule_type", "")
            fields: list[str] = rule.get("fields", [])

            missing = [f for f in fields if f not in data.columns]
            if missing:
                rule_results[rule_name] = {
                    "status": "skipped",
                    "reason": f"Missing columns: {missing}",
                }
                continue

            rate = self._evaluate_cross_field(
                data, rule_type, rule, total_records,
            )
            pass_rates.append(rate)

            rule_results[rule_name] = {
                "rule_type": rule_type,
                "fields": fields,
                "pass_rate": round(rate, 4),
            }

            if rate < 1.0:
                failed_count = int((1.0 - rate) * total_records)
                errors.append(
                    f"Cross-field rule '{rule_name}' ({rule_type}): "
                    f"{failed_count}/{total_records} records failed"
                )

        overall = float(np.mean(pass_rates)) if pass_rates else 1.0
        return overall, rule_results, errors

    def _evaluate_cross_field(
        self,
        data: pd.DataFrame,
        rule_type: str,
        rule: dict[str, Any],
        total_records: int,
    ) -> float:
        """Evaluate a single cross-field rule and return the pass rate.

        Dispatches to dedicated helper methods per rule type to keep the
        branch count manageable.
        """
        _dispatch: dict[str, Any] = {
            "balance": self._cross_field_balance,
            "less_than": self._cross_field_comparison,
            "less_than_or_equal": self._cross_field_comparison,
            "product_equals": self._cross_field_product,
            "sum_equals": self._cross_field_sum,
            "mutual_exclusive_nonzero": self._cross_field_exclusive,
        }
        handler = _dispatch.get(rule_type)
        if handler is None:
            self.logger.warning(
                "unknown_cross_field_rule_type", rule_type=rule_type,
            )
            return 1.0
        result: float = float(handler(data, rule, total_records, rule_type))
        return result

    # -- Cross-field sub-handlers ----------------------------------------

    @staticmethod
    def _cross_field_balance(
        data: pd.DataFrame,
        rule: dict[str, Any],
        total_records: int,
        _rule_type: str,
    ) -> float:
        """Check that two numeric fields are approximately equal."""
        fields: list[str] = rule.get("fields", [])
        if len(fields) < 2:
            return 1.0
        col_a = pd.to_numeric(data[fields[0]], errors="coerce").fillna(0)
        col_b = pd.to_numeric(data[fields[1]], errors="coerce").fillna(0)
        balanced = np.isclose(col_a, col_b, rtol=1e-4, atol=0.01)
        return float(np.sum(balanced)) / total_records if total_records else 0.0

    @staticmethod
    def _cross_field_comparison(
        data: pd.DataFrame,
        rule: dict[str, Any],
        _total_records: int,
        rule_type: str,
    ) -> float:
        """Check ``<`` or ``<=`` between two numeric fields."""
        fields: list[str] = rule.get("fields", [])
        if len(fields) < 2:
            return 1.0
        col_a = pd.to_numeric(data[fields[0]], errors="coerce")
        col_b = pd.to_numeric(data[fields[1]], errors="coerce")
        mask = col_a.notna() & col_b.notna()
        applicable = int(mask.sum())
        if applicable == 0:
            return 1.0
        valid = col_a[mask] <= col_b[mask] if rule_type == "less_than_or_equal" else col_a[mask] < col_b[mask]
        return float(valid.sum() / applicable)

    @staticmethod
    def _cross_field_product(
        data: pd.DataFrame,
        rule: dict[str, Any],
        total_records: int,
        _rule_type: str,
    ) -> float:
        """Check ``field_a * field_b ~ target_field``."""
        fields: list[str] = rule.get("fields", [])
        target_field: str = rule.get("target_field", "")
        if len(fields) < 2 or target_field not in data.columns:
            return 1.0
        col_a = pd.to_numeric(data[fields[0]], errors="coerce").fillna(0)
        col_b = pd.to_numeric(data[fields[1]], errors="coerce").fillna(0)
        col_t = pd.to_numeric(data[target_field], errors="coerce").fillna(0)
        product = col_a * col_b
        close = np.isclose(product, col_t, rtol=1e-3, atol=0.01)
        return float(np.sum(close)) / total_records if total_records else 0.0

    @staticmethod
    def _cross_field_sum(
        data: pd.DataFrame,
        rule: dict[str, Any],
        total_records: int,
        _rule_type: str,
    ) -> float:
        """Check ``sum(source_fields) ~ target_field``."""
        fields: list[str] = rule.get("fields", [])
        source_fields: list[str] = rule.get(
            "source_fields", fields[:-1] if fields else [],
        )
        target_field_se: str = rule.get(
            "target_field", fields[-1] if fields else "",
        )
        valid_sources = [f for f in source_fields if f in data.columns]
        if not valid_sources or target_field_se not in data.columns:
            return 1.0
        summed = pd.DataFrame(
            {
                f: pd.to_numeric(data[f], errors="coerce").fillna(0)
                for f in valid_sources
            }
        ).apply(np.sum, axis=1)
        col_t = pd.to_numeric(
            data[target_field_se], errors="coerce",
        ).fillna(0)
        close = np.isclose(summed, col_t, rtol=1e-3, atol=0.01)
        return float(np.sum(close)) / total_records if total_records else 0.0

    @staticmethod
    def _cross_field_exclusive(
        data: pd.DataFrame,
        rule: dict[str, Any],
        total_records: int,
        _rule_type: str,
    ) -> float:
        """Check that at most one of two fields is non-zero."""
        fields: list[str] = rule.get("fields", [])
        if len(fields) < 2:
            return 1.0
        col_a = pd.to_numeric(data[fields[0]], errors="coerce").fillna(0)
        col_b = pd.to_numeric(data[fields[1]], errors="coerce").fillna(0)
        both_nonzero = (col_a != 0) & (col_b != 0)
        violations = int(both_nonzero.sum())
        passed = total_records - violations
        return float(passed) / total_records if total_records else 0.0

    # ------------------------------------------------------------------
    # 3. Domain Rules
    # ------------------------------------------------------------------

    def _validate_domain_rules(
        self,
        data: pd.DataFrame,
        rules: list[dict[str, Any]],
    ) -> tuple[float, dict[str, Any], list[str]]:
        """Validate that field values belong to allowed domain sets.

        Uses Pydantic ``model_validate()`` for structured constraints
        (ISO currency / country codes) and :meth:`pandas.Series.isin` for
        simple enumeration membership checks.

        Returns:
            ``(pass_rate, details_dict, errors_list)``
        """
        if not rules:
            return 1.0, {"message": "No domain rules defined"}, []

        rule_results: dict[str, Any] = {}
        errors: list[str] = []
        pass_rates: list[float] = []

        for rule in rules:
            rule_name: str = rule.get("name", "unknown_domain_rule")
            field: str = rule.get("field", "")
            valid_values: list[str] = rule.get("valid_values", [])
            validation_model: str | None = rule.get("validation_model")

            if field not in data.columns:
                rule_results[rule_name] = {
                    "status": "skipped",
                    "reason": f"Column '{field}' not found",
                }
                continue

            series = data[field].dropna()
            applicable: int = len(series)

            if applicable == 0:
                rule_results[rule_name] = {
                    "status": "skipped",
                    "reason": "All values are null",
                }
                continue

            if validation_model == "currency":
                passed = self._validate_with_pydantic_model(
                    series, CurrencyCodeConstraint, "code",
                )
            elif validation_model == "country":
                passed = self._validate_with_pydantic_model(
                    series, CountryCodeConstraint, "code",
                )
            elif valid_values:
                matches = series.astype(str).isin(
                    [str(v) for v in valid_values]
                )
                passed = int(matches.sum())
            else:
                # No explicit value set — verify non-empty strings.
                str_series = series.astype(str).str.strip()
                passed = int((str_series.str.len() > 0).sum())

            rate = passed / applicable if applicable > 0 else 0.0
            pass_rates.append(rate)

            rule_results[rule_name] = {
                "field": field,
                "records_applicable": applicable,
                "records_passed": passed,
                "pass_rate": round(rate, 4),
            }

            if rate < 1.0:
                failed_count = applicable - passed
                errors.append(
                    f"Domain rule '{rule_name}': {failed_count}/{applicable}"
                    f" values failed for field '{field}'"
                )

        overall = float(np.mean(pass_rates)) if pass_rates else 1.0
        return overall, rule_results, errors

    def _validate_with_pydantic_model(
        self,
        series: pd.Series,
        model_class: type[BaseModel],
        field_name: str,
    ) -> int:
        """Validate series values using a Pydantic model.

        Iterates via :meth:`pandas.DataFrame.iterrows` and calls
        ``model_class.model_validate()`` / ``model_dump()`` for each row.

        Returns:
            Count of values that passed validation.
        """
        passed = 0
        # Wrap in a DataFrame so we can use iterrows().
        frame = pd.DataFrame(
            {field_name: series.values}, index=series.index,
        )
        for row_idx, row in frame.iterrows():
            try:
                obj = model_class.model_validate(
                    {field_name: str(row[field_name])}
                )
                # Exercise serialisation for round-trip consistency.
                obj.model_dump()
                passed += 1
            except Exception:
                self.logger.debug(
                    "domain_pydantic_validation_failure",
                    field=field_name,
                    row_index=row_idx,
                )
                continue
        return passed

    # ------------------------------------------------------------------
    # 4. Temporal Rules
    # ------------------------------------------------------------------

    def _validate_temporal_rules(
        self,
        data: pd.DataFrame,
        rules: list[dict[str, Any]],
    ) -> tuple[float, dict[str, Any], list[str]]:
        """Validate temporal ordering and consistency.

        Supported *rule_type* values:

        - ``before`` — field_before ≤ field_after.
        - ``after`` — field_after ≥ field_before.
        - ``between`` — field within [lower, upper] bounds.
        - ``range`` — field within N days relative to today.

        Uses :func:`pandas.to_datetime` for date parsing and
        :meth:`pandas.Series.between` for range checks.

        Returns:
            ``(pass_rate, details_dict, errors_list)``
        """
        if not rules:
            return 1.0, {"message": "No temporal rules defined"}, []

        rule_results: dict[str, Any] = {}
        errors: list[str] = []
        pass_rates: list[float] = []

        for rule in rules:
            rule_name: str = rule.get("name", "unknown_temporal_rule")
            rule_type: str = rule.get("rule_type", "before")

            try:
                rate = self._evaluate_temporal(data, rule_type, rule)
                pass_rates.append(rate)

                rule_results[rule_name] = {
                    "rule_type": rule_type,
                    "pass_rate": round(rate, 4),
                }

                if rate < 1.0:
                    total = data.shape[0]
                    failed = int((1.0 - rate) * total)
                    errors.append(
                        f"Temporal rule '{rule_name}' ({rule_type}): "
                        f"{failed}/{total} records failed"
                    )
            except Exception as exc:
                pass_rates.append(0.0)
                rule_results[rule_name] = {
                    "rule_type": rule_type,
                    "error": str(exc),
                }
                errors.append(
                    f"Temporal rule '{rule_name}' failed: {exc}"
                )

        overall = float(np.mean(pass_rates)) if pass_rates else 1.0
        return overall, rule_results, errors

    def _evaluate_temporal(
        self,
        data: pd.DataFrame,
        rule_type: str,
        rule: dict[str, Any],
    ) -> float:
        """Evaluate a single temporal rule and return the pass rate."""
        if rule_type == "before":
            field_before: str = rule.get("field_before", "")
            field_after: str = rule.get("field_after", "")
            if (
                field_before not in data.columns
                or field_after not in data.columns
            ):
                return 1.0
            dt_before = pd.to_datetime(data[field_before], errors="coerce")
            dt_after = pd.to_datetime(data[field_after], errors="coerce")
            mask = dt_before.notna() & dt_after.notna()
            applicable = int(mask.sum())
            if applicable == 0:
                return 1.0
            valid = dt_before[mask] <= dt_after[mask]
            return float(valid.sum() / applicable)

        if rule_type == "after":
            field_a: str = rule.get("field_after", "")
            field_b: str = rule.get("field_before", "")
            if field_a not in data.columns or field_b not in data.columns:
                return 1.0
            dt_a = pd.to_datetime(data[field_a], errors="coerce")
            dt_b = pd.to_datetime(data[field_b], errors="coerce")
            mask = dt_a.notna() & dt_b.notna()
            applicable = int(mask.sum())
            if applicable == 0:
                return 1.0
            valid = dt_a[mask] >= dt_b[mask]
            return float(valid.sum() / applicable)

        if rule_type == "between":
            field_check: str = rule.get("field", "")
            lower_field: str = rule.get("lower_bound_field", "")
            upper_field: str = rule.get("upper_bound_field", "")
            if (
                field_check not in data.columns
                or lower_field not in data.columns
                or upper_field not in data.columns
            ):
                return 1.0
            dt_val = pd.to_datetime(data[field_check], errors="coerce")
            dt_lower = pd.to_datetime(data[lower_field], errors="coerce")
            dt_upper = pd.to_datetime(data[upper_field], errors="coerce")
            mask = dt_val.notna() & dt_lower.notna() & dt_upper.notna()
            applicable = int(mask.sum())
            if applicable == 0:
                return 1.0
            in_range = dt_val[mask].between(
                dt_lower[mask], dt_upper[mask],
            )
            return float(in_range.sum() / applicable)

        if rule_type == "range":
            field_name: str = rule.get("field", "")
            min_days: int = rule.get("min_days_ago", 0)
            max_days: int = rule.get("max_days_ago", 36500)
            if field_name not in data.columns:
                return 1.0
            dt_vals = pd.to_datetime(data[field_name], errors="coerce")
            mask = dt_vals.notna()
            applicable = int(mask.sum())
            if applicable == 0:
                return 1.0
            # Use timezone-aware date for boundary calculation.
            today = datetime.now(tz=UTC).date()
            earliest = datetime.combine(
                today - timedelta(days=max_days),
                datetime.min.time(),
            )
            latest = datetime.combine(
                today - timedelta(days=min_days),
                datetime.max.time(),
            )
            ts_earliest = pd.Timestamp(earliest)
            ts_latest = pd.Timestamp(latest)
            in_range = dt_vals[mask].between(ts_earliest, ts_latest)
            return float(in_range.sum() / applicable)

        return 1.0

    # ------------------------------------------------------------------
    # 5. Conditional Rules
    # ------------------------------------------------------------------

    def _validate_conditional_rules(
        self,
        data: pd.DataFrame,
        rules: list[dict[str, Any]],
    ) -> tuple[float, dict[str, Any], list[str]]:
        """Validate conditional logic rules (if A then B).

        Supported *then_check* values:

        - ``negative`` — value < 0.
        - ``positive`` — value > 0.
        - ``non_negative`` — value ≥ 0.
        - ``not_null`` — value is not NaN / None.
        - ``is_null`` — value is NaN / None.
        - ``in_set`` — value in a specified set.
        - ``equals`` — value equals a specific target.

        Returns:
            ``(pass_rate, details_dict, errors_list)``
        """
        if not rules:
            return 1.0, {"message": "No conditional rules defined"}, []

        rule_results: dict[str, Any] = {}
        errors: list[str] = []
        pass_rates: list[float] = []

        for rule in rules:
            rule_name: str = rule.get("name", "unknown_conditional_rule")
            condition_field: str = rule.get("condition_field", "")
            condition_value = rule.get("condition_value")
            condition_operator: str = rule.get("condition_operator", "equals")
            then_field: str = rule.get("then_field", "")
            then_check: str = rule.get("then_check", "not_null")
            then_values: list[str] = rule.get("then_values", [])

            # Verify required columns exist.
            if condition_field not in data.columns:
                rule_results[rule_name] = {
                    "status": "skipped",
                    "reason": f"Condition field '{condition_field}' absent",
                }
                continue
            if then_field not in data.columns:
                rule_results[rule_name] = {
                    "status": "skipped",
                    "reason": f"Then field '{then_field}' absent",
                }
                continue

            # Build the condition mask using pd.DataFrame.loc[].
            condition_mask = self._build_condition_mask(
                data, condition_field, condition_operator, condition_value,
            )
            applicable = int(condition_mask.sum())

            if applicable == 0:
                rule_results[rule_name] = {
                    "status": "not_applicable",
                    "reason": "No records match condition",
                }
                continue

            # Evaluate the "then" constraint on matching records.
            filtered: pd.Series = data.loc[condition_mask, then_field]
            passed = self._evaluate_then_check(
                filtered, then_check, then_values, rule,
            )
            rate = float(passed) / float(applicable)
            pass_rates.append(rate)

            rule_results[rule_name] = {
                "condition": (
                    f"{condition_field} {condition_operator} {condition_value}"
                ),
                "then_check": f"{then_field} {then_check}",
                "applicable_records": applicable,
                "records_passed": int(passed),
                "pass_rate": round(rate, 4),
            }

            if rate < 1.0:
                failed = applicable - int(passed)
                errors.append(
                    f"Conditional rule '{rule_name}': {failed}/{applicable} "
                    f"records failed (when {condition_field}="
                    f"{condition_value}, {then_field} must be {then_check})"
                )

        overall = float(np.mean(pass_rates)) if pass_rates else 1.0
        return overall, rule_results, errors

    @staticmethod
    def _build_condition_mask(
        data: pd.DataFrame,
        field: str,
        operator: str,
        value: Any,
    ) -> pd.Series[Any]:
        """Build a boolean mask for the condition side of a conditional rule."""
        col = data[field]
        if operator == "equals":
            mask: pd.Series[Any] = pd.Series(col == value, index=data.index)
            return mask
        if operator == "not_equals":
            mask = pd.Series(col != value, index=data.index)
            return mask
        if operator == "in":
            vals = value if isinstance(value, list) else [value]
            return col.isin(vals)
        # Fallback: equality check.
        mask = pd.Series(col == value, index=data.index)
        return mask

    @staticmethod
    def _evaluate_then_check(
        series: pd.Series,
        check_type: str,
        check_values: list[str],
        rule: dict[str, Any],
    ) -> int:
        """Evaluate a 'then' constraint on a filtered series.

        Returns:
            Count of values passing the constraint.
        """
        if check_type == "negative":
            numeric = pd.to_numeric(series, errors="coerce")
            return int((numeric < 0).sum())

        if check_type == "positive":
            numeric = pd.to_numeric(series, errors="coerce")
            return int((numeric > 0).sum())

        if check_type == "non_negative":
            numeric = pd.to_numeric(series, errors="coerce")
            return int((numeric >= 0).sum())

        if check_type == "not_null":
            return int(series.notna().sum())

        if check_type == "is_null":
            return int(series.isna().sum())

        if check_type == "in_set":
            return int(series.isin(check_values).sum())

        if check_type == "equals":
            eq_value = rule.get("then_value")
            numeric_series = pd.to_numeric(series, errors="coerce")
            if eq_value is not None and not numeric_series.isna().all():
                return int(
                    np.isclose(
                        numeric_series.fillna(float("inf")),
                        float(eq_value),
                        atol=0.01,
                    ).sum()
                )
            return int((series == eq_value).sum())

        # Unknown check type — count all as passed.
        return len(series)

    # ------------------------------------------------------------------
    # Module rule retrieval & registration
    # ------------------------------------------------------------------

    def _resolve_module_name(self, profile: dict[str, Any]) -> str:
        """Extract the ERP module name from profile metadata.

        Falls back to heuristic table-name matching when no explicit
        ``module`` key is present.
        """
        module = (
            profile.get("module")
            or profile.get("erp_module")
            or ""
        )
        module = module.lower().strip().replace(" ", "_").replace("-", "_")

        if module in _SUPPORTED_MODULES:
            return module

        # Heuristic: infer module from table name.
        table_name = str(profile.get("table_name", "")).lower()
        _inference_map = {
            "financial_accounting": (
                "gl", "journal", "fiscal", "ledger", "posting",
                "payment", "invoice", "account",
            ),
            "human_resources": (
                "employee", "payroll", "benefit", "salary", "hire",
                "personnel", "workforce",
            ),
            "sales_distribution": (
                "order", "sales", "customer", "delivery",
                "invoice_line", "billing", "quotation",
            ),
            "material_management": (
                "material", "inventory", "purchase", "vendor",
                "stock", "po_", "warehouse",
            ),
        }
        for mod, keywords in _inference_map.items():
            if any(kw in table_name for kw in keywords):
                return mod

        self.logger.warning(
            "unknown_module_defaulting",
            raw_module=module,
            profile_keys=list(profile.keys()),
        )
        return module if module else "financial_accounting"

    def _get_module_rules(
        self, module_name: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Retrieve business rules organised by category for *module_name*.

        Args:
            module_name: One of the four supported ERP module identifiers.

        Returns:
            Dict mapping each rule category to its list of rule definitions.
        """
        all_rules = self._rule_registry.get(module_name, [])

        categorised: dict[str, list[dict[str, Any]]] = {
            "format": [],
            "cross_field": [],
            "domain": [],
            "temporal": [],
            "conditional": [],
        }

        for rule in all_rules:
            category = rule.get("category", "format")
            if category in categorised:
                categorised[category].append(rule)
            else:
                categorised.setdefault(category, []).append(rule)

        return categorised

    # ------------------------------------------------------------------
    # Module rule registration
    # ------------------------------------------------------------------

    def _register_all_module_rules(self) -> None:
        """Register business rules for all four supported ERP modules."""
        self._rule_registry["financial_accounting"] = (
            self._register_financial_accounting_rules()
        )
        self._rule_registry["human_resources"] = (
            self._register_hr_rules()
        )
        self._rule_registry["sales_distribution"] = (
            self._register_sales_distribution_rules()
        )
        self._rule_registry["material_management"] = (
            self._register_material_management_rules()
        )

    def _register_financial_accounting_rules(self) -> list[dict[str, Any]]:
        """Register Financial Accounting module business rules.

        Covers GL account formats, debit/credit balancing, fiscal period
        boundaries, currency code validity, and document-type conditional
        logic.

        Returns:
            Flat list of rule-definition dictionaries.
        """
        return [
            # ----- Format rules -----
            {
                "name": "gl_account_format",
                "category": "format",
                "field": "gl_account",
                "pattern": r"^\d{4}[.\-]?\d{4}[.\-]?\d{2,4}$",
                "description": (
                    "GL account must follow hierarchical "
                    "XXXX.XXXX.XXXX format"
                ),
            },
            {
                "name": "document_number_format",
                "category": "format",
                "field": "document_number",
                "pattern": r"^(DOC|FI|JE)[.\-]?\d{6,12}$",
                "description": "Document number must follow standard format",
            },
            {
                "name": "cost_center_format",
                "category": "format",
                "field": "cost_center",
                "pattern": r"^CC[.\-]?\d{4,8}$",
                "description": "Cost center code format",
            },
            # ----- Cross-field rules -----
            {
                "name": "debit_credit_exclusive",
                "category": "cross_field",
                "rule_type": "mutual_exclusive_nonzero",
                "fields": ["debit_amount", "credit_amount"],
                "description": (
                    "Debit and credit cannot both be non-zero "
                    "in the same line item"
                ),
            },
            # ----- Domain rules -----
            {
                "name": "fi_currency_code_valid",
                "category": "domain",
                "field": "currency_code",
                "validation_model": "currency",
                "description": "Currency code must be valid ISO 4217",
            },
            {
                "name": "document_type_valid",
                "category": "domain",
                "field": "document_type",
                "valid_values": [
                    "INVOICE", "CREDIT_MEMO", "DEBIT_MEMO",
                    "JOURNAL_ENTRY", "PAYMENT", "RECEIPT",
                    "ACCRUAL", "REVERSAL", "TRANSFER",
                ],
                "description": "Document type must be a valid enumeration",
            },
            {
                "name": "posting_status_valid",
                "category": "domain",
                "field": "posting_status",
                "valid_values": [
                    "POSTED", "PENDING", "REVERSED",
                    "PARKED", "CLEARED",
                ],
                "description": "Posting status must be a valid enumeration",
            },
            # ----- Temporal rules -----
            {
                "name": "posting_date_in_fiscal_period",
                "category": "temporal",
                "rule_type": "between",
                "field": "posting_date",
                "lower_bound_field": "fiscal_period_start",
                "upper_bound_field": "fiscal_period_end",
                "description": (
                    "Posting date must be within fiscal period boundaries"
                ),
            },
            {
                "name": "document_date_before_posting",
                "category": "temporal",
                "rule_type": "before",
                "field_before": "document_date",
                "field_after": "posting_date",
                "description": (
                    "Document date must be on or before posting date"
                ),
            },
            # ----- Conditional rules -----
            {
                "name": "credit_memo_negative_amount",
                "category": "conditional",
                "condition_field": "document_type",
                "condition_operator": "equals",
                "condition_value": "CREDIT_MEMO",
                "then_field": "amount",
                "then_check": "negative",
                "description": "Credit memos must have negative amounts",
            },
            {
                "name": "reversal_has_reference",
                "category": "conditional",
                "condition_field": "document_type",
                "condition_operator": "equals",
                "condition_value": "REVERSAL",
                "then_field": "reference",
                "then_check": "not_null",
                "description": (
                    "Reversals must reference an original document"
                ),
            },
        ]

    def _register_hr_rules(self) -> list[dict[str, Any]]:
        """Register Human Resources module business rules.

        Covers employee ID formats, date consistency (hire, birth,
        termination), salary ranges, employment-status enumerations, and
        status-dependent conditional rules.

        Returns:
            Flat list of rule-definition dictionaries.
        """
        return [
            # ----- Format rules -----
            {
                "name": "employee_id_format",
                "category": "format",
                "field": "employee_id",
                "pattern": r"^EMP[\-]?\d{5,8}$",
                "description": "Employee ID must follow EMP-NNNNN format",
            },
            # ----- Cross-field rules -----
            {
                "name": "salary_above_minimum",
                "category": "cross_field",
                "rule_type": "less_than_or_equal",
                "fields": ["min_salary", "salary"],
                "description": (
                    "Salary must be at or above minimum for grade"
                ),
            },
            # ----- Domain rules -----
            {
                "name": "employment_status_valid",
                "category": "domain",
                "field": "employment_status",
                "valid_values": [
                    "ACTIVE", "TERMINATED", "ON_LEAVE",
                    "SUSPENDED", "RETIRED", "PROBATION",
                ],
                "description": (
                    "Employment status must be a valid enumeration"
                ),
            },
            {
                "name": "gender_code_valid",
                "category": "domain",
                "field": "gender",
                "valid_values": [
                    "M", "F", "MALE", "FEMALE", "OTHER",
                    "NON_BINARY", "PREFER_NOT_TO_SAY",
                ],
                "description": "Gender code must be a valid enumeration",
            },
            {
                "name": "hr_country_code_valid",
                "category": "domain",
                "field": "country_code",
                "validation_model": "country",
                "description": (
                    "Country code must be valid ISO 3166-1 alpha-2"
                ),
            },
            # ----- Temporal rules -----
            {
                "name": "hire_before_termination",
                "category": "temporal",
                "rule_type": "before",
                "field_before": "hire_date",
                "field_after": "termination_date",
                "description": (
                    "Hire date must precede termination date"
                ),
            },
            {
                "name": "birth_date_reasonable",
                "category": "temporal",
                "rule_type": "range",
                "field": "birth_date",
                "min_days_ago": 5840,
                "max_days_ago": 29200,
                "description": (
                    "Birth date must make employee between 16 and 80 years"
                ),
            },
            {
                "name": "hire_date_reasonable",
                "category": "temporal",
                "rule_type": "range",
                "field": "hire_date",
                "min_days_ago": 0,
                "max_days_ago": 18250,
                "description": "Hire date must be within last 50 years",
            },
            # ----- Conditional rules -----
            {
                "name": "terminated_has_termination_date",
                "category": "conditional",
                "condition_field": "employment_status",
                "condition_operator": "equals",
                "condition_value": "TERMINATED",
                "then_field": "termination_date",
                "then_check": "not_null",
                "description": (
                    "Terminated employees must have a termination date"
                ),
            },
            {
                "name": "active_no_termination_date",
                "category": "conditional",
                "condition_field": "employment_status",
                "condition_operator": "equals",
                "condition_value": "ACTIVE",
                "then_field": "termination_date",
                "then_check": "is_null",
                "description": (
                    "Active employees should not have a termination date"
                ),
            },
            {
                "name": "retired_positive_pension",
                "category": "conditional",
                "condition_field": "employment_status",
                "condition_operator": "equals",
                "condition_value": "RETIRED",
                "then_field": "pension_amount",
                "then_check": "positive",
                "description": (
                    "Retired employees must have positive pension amount"
                ),
            },
        ]

    def _register_sales_distribution_rules(self) -> list[dict[str, Any]]:
        """Register Sales & Distribution module business rules.

        Covers order number formats, price/quantity/total consistency,
        order-status enumerations, delivery date ordering, and
        cancellation-dependent conditional rules.

        Returns:
            Flat list of rule-definition dictionaries.
        """
        return [
            # ----- Format rules -----
            {
                "name": "order_number_format",
                "category": "format",
                "field": "order_number",
                "pattern": r"^(SO|ORD)[\-]?\d{6,10}$",
                "description": "Order number must follow SO-NNNNNN format",
            },
            {
                "name": "customer_id_format",
                "category": "format",
                "field": "customer_id",
                "pattern": r"^(CUST|C)[\-]?\d{5,10}$",
                "description": "Customer ID must follow CUST-NNNNN format",
            },
            # ----- Cross-field rules -----
            {
                "name": "line_total_price_times_qty",
                "category": "cross_field",
                "rule_type": "product_equals",
                "fields": ["unit_price", "quantity"],
                "target_field": "total_amount",
                "description": (
                    "Line total must equal unit_price x quantity"
                ),
            },
            # ----- Domain rules -----
            {
                "name": "order_status_valid",
                "category": "domain",
                "field": "order_status",
                "valid_values": [
                    "CREATED", "CONFIRMED", "PROCESSING",
                    "SHIPPED", "DELIVERED", "INVOICED",
                    "CANCELLED", "RETURNED", "ON_HOLD",
                ],
                "description": "Order status must be a valid enumeration",
            },
            {
                "name": "payment_terms_valid",
                "category": "domain",
                "field": "payment_terms",
                "valid_values": [
                    "NET_30", "NET_60", "NET_90", "NET_15",
                    "COD", "PREPAID", "INSTALLMENT",
                    "2_10_NET_30",
                ],
                "description": "Payment terms must be a valid enumeration",
            },
            {
                "name": "sd_currency_code_valid",
                "category": "domain",
                "field": "currency_code",
                "validation_model": "currency",
                "description": "Currency code must be valid ISO 4217",
            },
            # ----- Temporal rules -----
            {
                "name": "order_date_before_delivery",
                "category": "temporal",
                "rule_type": "before",
                "field_before": "order_date",
                "field_after": "delivery_date",
                "description": (
                    "Order date must precede delivery date"
                ),
            },
            {
                "name": "delivery_before_invoice",
                "category": "temporal",
                "rule_type": "before",
                "field_before": "delivery_date",
                "field_after": "invoice_date",
                "description": (
                    "Delivery date must precede invoice date"
                ),
            },
            # ----- Conditional rules -----
            {
                "name": "cancelled_has_cancellation_date",
                "category": "conditional",
                "condition_field": "order_status",
                "condition_operator": "equals",
                "condition_value": "CANCELLED",
                "then_field": "cancellation_date",
                "then_check": "not_null",
                "description": (
                    "Cancelled orders must have a cancellation date"
                ),
            },
            {
                "name": "delivered_has_delivery_date",
                "category": "conditional",
                "condition_field": "order_status",
                "condition_operator": "equals",
                "condition_value": "DELIVERED",
                "then_field": "delivery_date",
                "then_check": "not_null",
                "description": (
                    "Delivered orders must have a delivery date"
                ),
            },
            {
                "name": "active_order_positive_qty",
                "category": "conditional",
                "condition_field": "order_status",
                "condition_operator": "in",
                "condition_value": [
                    "CREATED", "CONFIRMED", "PROCESSING",
                    "SHIPPED", "DELIVERED",
                ],
                "then_field": "quantity",
                "then_check": "positive",
                "description": (
                    "Active orders must have positive quantity"
                ),
            },
        ]

    def _register_material_management_rules(self) -> list[dict[str, Any]]:
        """Register Material Management module business rules.

        Covers material number formats, inventory non-negative constraints,
        PO amount/quantity consistency, vendor cross-references, and
        stock-status conditional rules.

        Returns:
            Flat list of rule-definition dictionaries.
        """
        return [
            # ----- Format rules -----
            {
                "name": "material_number_format",
                "category": "format",
                "field": "material_number",
                "pattern": r"^(MAT|M)[\-]?\d{6,10}$",
                "description": (
                    "Material number must follow MAT-NNNNNN format"
                ),
            },
            {
                "name": "po_number_format",
                "category": "format",
                "field": "po_number",
                "pattern": r"^(PO|P)[\-]?\d{6,10}$",
                "description": (
                    "Purchase order number must follow PO-NNNNNN format"
                ),
            },
            {
                "name": "vendor_id_format",
                "category": "format",
                "field": "vendor_id",
                "pattern": r"^(VND|V)[\-]?\d{5,10}$",
                "description": "Vendor ID must follow VND-NNNNN format",
            },
            # ----- Cross-field rules -----
            {
                "name": "po_total_price_times_qty",
                "category": "cross_field",
                "rule_type": "product_equals",
                "fields": ["unit_price", "quantity"],
                "target_field": "total_amount",
                "description": (
                    "PO total must equal unit_price x quantity"
                ),
            },
            # ----- Domain rules -----
            {
                "name": "material_type_valid",
                "category": "domain",
                "field": "material_type",
                "valid_values": [
                    "RAW", "SEMI_FINISHED", "FINISHED",
                    "TRADING", "PACKAGING",
                    "OPERATING_SUPPLIES", "SERVICE", "SPARE_PART",
                ],
                "description": (
                    "Material type must be a valid enumeration"
                ),
            },
            {
                "name": "unit_of_measure_valid",
                "category": "domain",
                "field": "unit_of_measure",
                "valid_values": [
                    "EA", "KG", "LB", "M", "FT", "L", "GAL",
                    "BOX", "PKG", "PCS", "SET", "DZ", "TON",
                    "CM", "MM", "IN", "ML", "OZ",
                ],
                "description": (
                    "Unit of measure must be a valid enumeration"
                ),
            },
            {
                "name": "po_status_valid",
                "category": "domain",
                "field": "po_status",
                "valid_values": [
                    "CREATED", "APPROVED", "ORDERED",
                    "RECEIVED", "PARTIALLY_RECEIVED",
                    "CANCELLED", "CLOSED",
                ],
                "description": "PO status must be a valid enumeration",
            },
            # ----- Temporal rules -----
            {
                "name": "po_date_before_delivery",
                "category": "temporal",
                "rule_type": "before",
                "field_before": "po_date",
                "field_after": "delivery_date",
                "description": "PO date must precede delivery date",
            },
            # ----- Conditional rules -----
            {
                "name": "inventory_non_negative",
                "category": "conditional",
                "condition_field": "material_type",
                "condition_operator": "in",
                "condition_value": [
                    "RAW", "SEMI_FINISHED", "FINISHED",
                    "TRADING", "PACKAGING", "SPARE_PART",
                ],
                "then_field": "stock_quantity",
                "then_check": "non_negative",
                "description": (
                    "Physical inventory must be non-negative"
                ),
            },
            {
                "name": "received_has_receipt_date",
                "category": "conditional",
                "condition_field": "po_status",
                "condition_operator": "equals",
                "condition_value": "RECEIVED",
                "then_field": "receipt_date",
                "then_check": "not_null",
                "description": (
                    "Received POs must have a receipt date"
                ),
            },
            {
                "name": "cancelled_po_zero_received",
                "category": "conditional",
                "condition_field": "po_status",
                "condition_operator": "equals",
                "condition_value": "CANCELLED",
                "then_field": "received_quantity",
                "then_check": "equals",
                "then_value": 0,
                "description": (
                    "Cancelled POs should have zero received quantity"
                ),
            },
        ]

    # ------------------------------------------------------------------
    # Score aggregation
    # ------------------------------------------------------------------

    def _aggregate_rule_scores(
        self,
        rule_scores: dict[str, float | None],
    ) -> float:
        """Compute weighted average of per-category rule scores.

        Uses :func:`numpy.average` with category weights from the
        configuration.  Categories with ``None`` scores (skipped) are
        excluded from the aggregation.

        Args:
            rule_scores: Mapping of category names to scores (0.0-1.0).

        Returns:
            Composite business-rules score in [0.0, 1.0].
        """
        scores: list[float] = []
        weights: list[float] = []

        for category, score in rule_scores.items():
            if score is not None:
                scores.append(float(score))
                weights.append(
                    self._category_weights.get(category, 0.2)
                )

        if not scores:
            return 0.0

        composite = float(
            np.average(
                np.array(scores, dtype=np.float64),
                weights=weights,
            )
        )
        # Clamp to [0.0, 1.0] to guard against floating-point drift.
        return max(0.0, min(1.0, composite))

    # ------------------------------------------------------------------
    # Helper utilities
    # ------------------------------------------------------------------

    def _get_compiled_pattern(self, pattern_str: str) -> re.Pattern:
        """Retrieve or compile and cache a regex pattern.

        Args:
            pattern_str: Raw regex string.

        Returns:
            Compiled :class:`re.Pattern`.
        """
        if pattern_str not in self._compiled_patterns:
            self._compiled_patterns[pattern_str] = re.compile(pattern_str)
        return self._compiled_patterns[pattern_str]
