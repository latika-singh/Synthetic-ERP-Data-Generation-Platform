"""Business rules engine for constraint-based synthetic data generation.

This module implements the **Rules Generator** — one of four concrete generation
strategies in the Synthetic ERP Data Generation Platform.  It produces synthetic
ERP records by evaluating a declarative set of constraint objects (value ranges,
format patterns, lookup tables, conditional logic, cross-field dependencies, and
date ranges) rather than by learning from data distributions.

Core Components:
    - :class:`RangeRule` — Numeric range constraints with distribution control.
    - :class:`FormatRule` — String format patterns for ERP document numbers.
    - :class:`LookupRule` — Enumerated value sets with optional weights.
    - :class:`ConditionalRule` — Parent-dependent field rules.
    - :class:`CrossFieldRule` — Inter-field formula relationships.
    - :class:`DateRule` — Date range generation with business-day filtering.
    - :class:`FieldRule` — Field-level rule container.
    - :class:`RulesConfig` — Top-level generator configuration.
    - :data:`ERP_MODULE_RULES` — Pre-built rule sets for four ERP modules.
    - :class:`RulesGenerator` — Concrete :class:`BaseGenerator` subclass.

Usage::

    from generation_engine.generators.rules_generator import RulesGenerator

    gen = RulesGenerator(config={"erp_module": "financial_accounting", "seed": 42})
    result = gen.generate(schema=my_schema, profile={}, num_records=10000)
"""

from __future__ import annotations

import random
import re
import string
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal


if TYPE_CHECKING:
    from collections.abc import Callable

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from generation_engine.generators.base import (
    BaseGenerator,
    GenerationError,
    GenerationResult,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Pre-compiled regex for format pattern placeholder extraction.
# Matches tokens: {YYYY}, {SEQ:8}, {NUM:6}, {ALPHA:4}, {HEX:8}.
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"\{(\w+)(?::(\d+))?\}")

# Whitelist of safe names for cross-field formula evaluation.
_SAFE_FORMULA_NAMES: dict[str, Any] = {
    "round": round,
    "abs": abs,
    "max": max,
    "min": min,
    "int": int,
    "float": float,
}

# Reject formulas with dangerous constructs.
_UNSAFE_FORMULA_RE: re.Pattern[str] = re.compile(
    r"(__|\bimport\b|\bexec\b|\beval\b|\bcompile\b|\bopen\b|\bgetattr\b|\bsetattr\b)"
)


# ---------------------------------------------------------------------------
# Pydantic Rule Model Classes
# ---------------------------------------------------------------------------


class RangeRule(BaseModel):
    """Numeric range constraint with distribution control.

    Attributes:
        min_value: Lower bound (inclusive).
        max_value: Upper bound (inclusive).
        distribution: Sampling distribution (uniform | normal | weighted).
        step: Optional rounding step (e.g. ``1`` for integers).
    """

    min_value: float = Field(..., description="Inclusive lower bound.")
    max_value: float = Field(..., description="Inclusive upper bound.")
    distribution: Literal["uniform", "normal", "weighted"] = Field(
        default="uniform",
        description="Sampling distribution.",
    )
    step: float | None = Field(default=None, description="Rounding step.")


class FormatRule(BaseModel):
    """String format pattern for ERP identifiers and document numbers.

    Placeholder tokens:
    - ``{YYYY}`` — Current year.  ``{SEQ:N}`` — Zero-padded sequential.
    - ``{NUM:N}`` — Random N-digit.  ``{ALPHA:N}`` — Random N-alpha.
    - ``{HEX:N}`` — Random N-hex.

    Attributes:
        pattern: Format pattern string.
        prefix: Optional prefix.
        suffix: Optional suffix.
        length: Optional fixed total string length.
        char_set: Character set for random segments.
    """

    pattern: str = Field(..., description="Format pattern with placeholder tokens.")
    prefix: str | None = Field(default=None, description="Prefix.")
    suffix: str | None = Field(default=None, description="Suffix.")
    length: int | None = Field(default=None, ge=1, description="Fixed total length.")
    char_set: Literal["alphanumeric", "numeric", "alpha", "hex"] = Field(
        default="alphanumeric",
        description="Character set for random segments.",
    )


class LookupRule(BaseModel):
    """Enumerated value set with optional probability weights.

    Attributes:
        values: List of allowed categorical values.
        weights: Optional selection probabilities (sum ~1.0).
        allow_null: Whether ``None`` is valid.
        null_probability: Per-record probability of ``None``.
    """

    values: list[Any] = Field(..., min_length=1, description="Allowed values.")
    weights: list[float] | None = Field(default=None, description="Selection probabilities.")
    allow_null: bool = Field(default=False, description="Allow NULL.")
    null_probability: float = Field(default=0.0, ge=0.0, le=1.0, description="NULL probability.")

    @model_validator(mode="after")
    def _validate_weights_length(self) -> LookupRule:
        """Ensure weights length matches values length when provided."""
        if self.weights is not None and len(self.weights) != len(self.values):
            raise ValueError(
                f"weights length ({len(self.weights)}) must match "
                f"values length ({len(self.values)})."
            )
        return self


class ConditionalRule(BaseModel):
    """Parent-dependent field rule for conditional generation.

    Attributes:
        depends_on: Parent field name.
        conditions: Parent value → child rule config mapping.
        default_rule: Fallback rule for unmatched parent values.
    """

    depends_on: str = Field(..., description="Parent field name.")
    conditions: dict[str, Any] = Field(..., description="Parent value → child rule config.")
    default_rule: Any | None = Field(default=None, description="Default rule.")


class CrossFieldRule(BaseModel):
    """Inter-field formula relationship for derived columns.

    Attributes:
        source_fields: Input column names consumed by the formula.
        formula: Arithmetic expression (e.g. ``"qty * price * (1 - disc/100)"``).
        validation_expression: Optional boolean validation expression.
    """

    source_fields: list[str] = Field(..., min_length=1, description="Input columns.")
    formula: str = Field(..., min_length=1, description="Arithmetic formula.")
    validation_expression: str | None = Field(default=None, description="Validation expr.")


class DateRule(BaseModel):
    """Date range constraint with business-day filtering.

    Attributes:
        start_date: ISO start date string.
        end_date: ISO end date string.
        format_string: ``strftime`` output format.
        business_days_only: Exclude weekends.
        exclude_holidays: Exclude common holidays.
    """

    start_date: str = Field(default="2020-01-01", description="ISO start date.")
    end_date: str = Field(default="2025-12-31", description="ISO end date.")
    format_string: str = Field(default="%Y-%m-%d", description="Output format.")
    business_days_only: bool = Field(default=False, description="Business days only.")
    exclude_holidays: bool = Field(default=False, description="Exclude holidays.")


class FieldRule(BaseModel):
    """Field-level rule container aggregating a specific rule type.

    Attributes:
        field_name: Target column name.
        rule_type: Rule type discriminator.
        rule_config: Rule configuration object.
        nullable: Whether the field accepts ``None``.
        null_probability: Per-record NULL probability.
    """

    field_name: str = Field(..., description="Target column name.")
    rule_type: Literal["range", "format", "lookup", "conditional", "cross_field", "date"] = Field(
        ..., description="Rule type discriminator."
    )
    rule_config: RangeRule | FormatRule | LookupRule | ConditionalRule | CrossFieldRule | DateRule = Field(
        ..., description="Rule configuration object."
    )
    nullable: bool = Field(default=False, description="Allow NULL.")
    null_probability: float = Field(default=0.0, ge=0.0, le=1.0, description="NULL probability.")

    _RULE_TYPE_MAP: dict[str, type] = {
        "range": RangeRule,
        "format": FormatRule,
        "lookup": LookupRule,
        "conditional": ConditionalRule,
        "cross_field": CrossFieldRule,
        "date": DateRule,
    }

    @model_validator(mode="before")
    @classmethod
    def _coerce_rule_config(cls, data: Any) -> Any:
        """Coerce *rule_config* from a raw dict into the correct Pydantic model."""
        if isinstance(data, dict):
            rule_type = data.get("rule_type")
            rule_config = data.get("rule_config")
            type_map = {
                "range": RangeRule,
                "format": FormatRule,
                "lookup": LookupRule,
                "conditional": ConditionalRule,
                "cross_field": CrossFieldRule,
                "date": DateRule,
            }
            if isinstance(rule_config, dict) and rule_type in type_map:
                data["rule_config"] = type_map[rule_type](**rule_config)
        return data


class RulesConfig(BaseModel):
    """Top-level configuration for the rules-based generator.

    Attributes:
        rules: Ordered list of field-level generation rules.
        erp_module: ERP module context for default rules.
        enforce_cross_field: Enforce cross-field dependency rules.
        seed: Random seed for reproducibility.
        custom_validators: Custom validation expressions by field name.
    """

    rules: list[FieldRule] = Field(default_factory=list, description="Field-level rules.")
    erp_module: Literal["financial_accounting", "hr", "sales_distribution", "material_management"] | None = Field(default=None, description="ERP module context.")
    enforce_cross_field: bool = Field(default=True, description="Enforce cross-field rules.")
    seed: int | None = Field(default=None, description="Random seed.")
    custom_validators: dict[str, str] | None = Field(
        default=None, description="Custom validation expressions."
    )


# ---------------------------------------------------------------------------
# ERP Module Pre-Built Rule Sets
# ---------------------------------------------------------------------------

ERP_MODULE_RULES: dict[str, list[FieldRule]] = {
    # ------------------------------------------------------------------
    # Financial Accounting Module
    # ------------------------------------------------------------------
    "financial_accounting": [
        FieldRule(
            field_name="document_number",
            rule_type="format",
            rule_config=FormatRule(pattern="FI-{YYYY}-{SEQ:8}"),
        ),
        FieldRule(
            field_name="company_code",
            rule_type="lookup",
            rule_config=LookupRule(values=["1000", "2000", "3000", "4000"]),
        ),
        FieldRule(
            field_name="fiscal_year",
            rule_type="range",
            rule_config=RangeRule(min_value=2020, max_value=2025, step=1),
        ),
        FieldRule(
            field_name="posting_date",
            rule_type="date",
            rule_config=DateRule(
                start_date="2020-01-01",
                end_date="2025-12-31",
                business_days_only=True,
            ),
        ),
        FieldRule(
            field_name="amount",
            rule_type="range",
            rule_config=RangeRule(
                min_value=0.01,
                max_value=999999999.99,
                distribution="normal",
                step=0.01,
            ),
        ),
        FieldRule(
            field_name="currency",
            rule_type="lookup",
            rule_config=LookupRule(
                values=["USD", "EUR", "GBP", "JPY"],
                weights=[0.4, 0.3, 0.2, 0.1],
            ),
        ),
        FieldRule(
            field_name="gl_account",
            rule_type="format",
            rule_config=FormatRule(pattern="{NUM:10}", prefix=""),
        ),
        FieldRule(
            field_name="cost_center",
            rule_type="format",
            rule_config=FormatRule(pattern="CC-{NUM:6}"),
        ),
        FieldRule(
            field_name="debit_credit",
            rule_type="lookup",
            rule_config=LookupRule(values=["D", "C"], weights=[0.5, 0.5]),
        ),
    ],
    # ------------------------------------------------------------------
    # Human Resources Module
    # ------------------------------------------------------------------
    "hr": [
        FieldRule(
            field_name="employee_id",
            rule_type="format",
            rule_config=FormatRule(pattern="EMP-{SEQ:8}"),
        ),
        FieldRule(
            field_name="hire_date",
            rule_type="date",
            rule_config=DateRule(
                start_date="2010-01-01",
                end_date="2025-12-31",
                business_days_only=True,
            ),
        ),
        FieldRule(
            field_name="department",
            rule_type="lookup",
            rule_config=LookupRule(
                values=["Finance", "HR", "IT", "Sales", "Operations", "Legal"],
            ),
        ),
        FieldRule(
            field_name="salary",
            rule_type="range",
            rule_config=RangeRule(
                min_value=30000,
                max_value=500000,
                distribution="normal",
                step=0.01,
            ),
        ),
        FieldRule(
            field_name="pay_grade",
            rule_type="lookup",
            rule_config=LookupRule(values=["G1", "G2", "G3", "G4", "G5", "G6"]),
        ),
        FieldRule(
            field_name="employment_status",
            rule_type="lookup",
            rule_config=LookupRule(
                values=["Active", "On Leave", "Terminated"],
                weights=[0.85, 0.1, 0.05],
            ),
        ),
    ],
    # ------------------------------------------------------------------
    # Sales & Distribution Module
    # ------------------------------------------------------------------
    "sales_distribution": [
        FieldRule(
            field_name="order_number",
            rule_type="format",
            rule_config=FormatRule(pattern="SO-{YYYY}-{SEQ:8}"),
        ),
        FieldRule(
            field_name="customer_id",
            rule_type="format",
            rule_config=FormatRule(pattern="CUST-{SEQ:6}"),
        ),
        FieldRule(
            field_name="order_date",
            rule_type="date",
            rule_config=DateRule(
                start_date="2020-01-01",
                end_date="2025-12-31",
                business_days_only=False,
            ),
        ),
        FieldRule(
            field_name="quantity",
            rule_type="range",
            rule_config=RangeRule(min_value=1, max_value=10000, distribution="normal", step=1),
        ),
        FieldRule(
            field_name="unit_price",
            rule_type="range",
            rule_config=RangeRule(min_value=0.01, max_value=99999.99, step=0.01),
        ),
        FieldRule(
            field_name="discount_pct",
            rule_type="range",
            rule_config=RangeRule(min_value=0, max_value=50, step=0.01),
        ),
        FieldRule(
            field_name="net_amount",
            rule_type="cross_field",
            rule_config=CrossFieldRule(
                source_fields=["quantity", "unit_price", "discount_pct"],
                formula="quantity * unit_price * (1 - discount_pct / 100)",
            ),
        ),
    ],
    # ------------------------------------------------------------------
    # Material Management Module
    # ------------------------------------------------------------------
    "material_management": [
        FieldRule(
            field_name="material_number",
            rule_type="format",
            rule_config=FormatRule(pattern="MAT-{SEQ:8}"),
        ),
        FieldRule(
            field_name="plant",
            rule_type="lookup",
            rule_config=LookupRule(values=["P100", "P200", "P300", "P400"]),
        ),
        FieldRule(
            field_name="storage_location",
            rule_type="format",
            rule_config=FormatRule(pattern="SL-{NUM:4}"),
        ),
        FieldRule(
            field_name="stock_quantity",
            rule_type="range",
            rule_config=RangeRule(min_value=0, max_value=100000, step=1),
        ),
        FieldRule(
            field_name="unit_of_measure",
            rule_type="lookup",
            rule_config=LookupRule(values=["EA", "KG", "LB", "L", "M", "FT"]),
        ),
        FieldRule(
            field_name="purchase_order",
            rule_type="format",
            rule_config=FormatRule(pattern="PO-{YYYY}-{SEQ:6}"),
        ),
        FieldRule(
            field_name="vendor_id",
            rule_type="format",
            rule_config=FormatRule(pattern="VND-{SEQ:6}"),
        ),
    ],
}


# ---------------------------------------------------------------------------
# Common US Federal Holiday Dates (for exclude_holidays support)
# ---------------------------------------------------------------------------

def _get_us_holidays(year: int) -> set[datetime]:
    """Return a set of common US federal holiday dates for the given *year*.

    Covers: New Year, MLK Day (3rd Mon Jan), Presidents Day (3rd Mon Feb),
    Memorial Day (last Mon May), Independence Day, Labor Day (1st Mon Sep),
    Veterans Day, Thanksgiving (4th Thu Nov), Christmas.

    All returned datetimes are UTC-aware to satisfy strict linting rules.
    """
    _utc = UTC
    holidays: set[datetime] = set()
    holidays.add(datetime(year, 1, 1, tzinfo=_utc))   # New Year
    holidays.add(datetime(year, 7, 4, tzinfo=_utc))   # Independence Day
    holidays.add(datetime(year, 11, 11, tzinfo=_utc))  # Veterans Day
    holidays.add(datetime(year, 12, 25, tzinfo=_utc))  # Christmas

    # MLK Day — 3rd Monday in January
    jan1 = datetime(year, 1, 1, tzinfo=_utc)
    first_monday = jan1 + timedelta(days=(7 - jan1.weekday()) % 7)
    holidays.add(first_monday + timedelta(weeks=2))

    # Presidents Day — 3rd Monday in February
    feb1 = datetime(year, 2, 1, tzinfo=_utc)
    first_monday = feb1 + timedelta(days=(7 - feb1.weekday()) % 7)
    holidays.add(first_monday + timedelta(weeks=2))

    # Memorial Day — last Monday in May
    may31 = datetime(year, 5, 31, tzinfo=_utc)
    holidays.add(may31 - timedelta(days=may31.weekday()))

    # Labor Day — 1st Monday in September
    sep1 = datetime(year, 9, 1, tzinfo=_utc)
    first_monday = sep1 + timedelta(days=(7 - sep1.weekday()) % 7)
    holidays.add(first_monday)

    # Thanksgiving — 4th Thursday in November
    nov1 = datetime(year, 11, 1, tzinfo=_utc)
    first_thursday = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    holidays.add(first_thursday + timedelta(weeks=3))

    return holidays


# ---------------------------------------------------------------------------
# RulesGenerator — Concrete BaseGenerator Subclass
# ---------------------------------------------------------------------------


class RulesGenerator(BaseGenerator):
    """Business rules engine for constraint-based ERP data generation.

    Implements the :class:`BaseGenerator` Strategy pattern contract by
    evaluating a declarative set of rule objects (ranges, formats, lookups,
    conditionals, cross-field formulas, dates) to produce synthetic records
    that satisfy ERP business constraints.

    The generator supports four ERP modules out-of-the-box via
    :data:`ERP_MODULE_RULES` and allows custom rule overrides.

    Args:
        config: Optional configuration dictionary parsed into
            :class:`RulesConfig`.  May include ``erp_module``, ``rules``,
            ``seed``, and ``enforce_cross_field`` keys.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.logger = get_logger(__name__)

        # Parse config into RulesConfig (tolerant of missing/empty config).
        self._rules_config: RulesConfig | None = None
        self._effective_rules: list[FieldRule] = []
        self._seq_counters: dict[str, int] = {}

        if config:
            try:
                self._rules_config = RulesConfig(**config)
            except Exception as exc:
                self.logger.warning(
                    "rules_config_parse_warning",
                    error=str(exc),
                    fallback="empty config",
                )
                self._rules_config = RulesConfig()

        # Seed random generators for reproducibility.
        if self._rules_config and self._rules_config.seed is not None:
            random.seed(self._rules_config.seed)
            np.random.seed(self._rules_config.seed)
            self.logger.info("random_seed_set", seed=self._rules_config.seed)

        # Load ERP module defaults and merge with custom rules.
        self._load_and_merge_rules()

    # ------------------------------------------------------------------
    # Rule Merging
    # ------------------------------------------------------------------

    def _load_and_merge_rules(self) -> None:
        """Load ERP module default rules and merge with user-supplied rules.

        Custom rules override module defaults on a per-field basis (matched
        by ``field_name``).
        """
        module_rules: list[FieldRule] = []

        if self._rules_config and self._rules_config.erp_module:
            module_name = self._rules_config.erp_module
            if module_name in ERP_MODULE_RULES:
                module_rules = list(ERP_MODULE_RULES[module_name])
                self.logger.info(
                    "erp_module_rules_loaded",
                    module=module_name,
                    num_rules=len(module_rules),
                )
            else:
                self.logger.warning("unknown_erp_module", module=module_name)

        custom_rules: list[FieldRule] = (
            self._rules_config.rules if self._rules_config else []
        )
        custom_field_names: set[str] = {r.field_name for r in custom_rules}

        # Module defaults for fields not overridden by custom rules.
        merged: list[FieldRule] = [
            r for r in module_rules if r.field_name not in custom_field_names
        ]
        merged.extend(custom_rules)
        self._effective_rules = merged

        self.logger.debug(
            "rules_merged",
            total=len(merged),
            module_defaults=len(module_rules),
            custom=len(custom_rules),
        )

    # ------------------------------------------------------------------
    # Generation Preparation Helpers
    # ------------------------------------------------------------------

    def _reseed_rngs(self) -> None:
        """Re-seed RNGs so that repeated ``generate()`` calls with the same
        seed always produce identical output, regardless of intervening random
        consumption by other code."""
        if self._rules_config and self._rules_config.seed is not None:
            random.seed(self._rules_config.seed)
            np.random.seed(self._rules_config.seed)

    def _resolve_effective_rules(self, kwargs: dict[str, Any]) -> list[FieldRule]:
        """Return the effective rule list, optionally overridden at runtime.

        If ``kwargs["rules_config"]`` is a *dict*, it is parsed as a
        :class:`RulesConfig` and merged with the appropriate ERP module
        defaults.  Otherwise the pre-computed ``_effective_rules`` are used.

        Raises:
            GenerationError: If the runtime config is malformed.
        """
        runtime_config = kwargs.get("rules_config")
        if runtime_config and isinstance(runtime_config, dict):
            try:
                rt_cfg = RulesConfig(**runtime_config)
                return self._merge_rules_lists(
                    ERP_MODULE_RULES.get(rt_cfg.erp_module, [])
                    if rt_cfg.erp_module
                    else [],
                    rt_cfg.rules,
                )
            except Exception as exc:
                raise GenerationError(
                    message=f"Invalid runtime rules_config: {exc}",
                    method="rules",
                    details={"error": str(exc)},
                ) from exc
        return self._effective_rules

    def _generate_empty_fallback(
        self,
        schema: dict[str, Any],
        num_records: int,
    ) -> GenerationResult:
        """Produce an all-*None* DataFrame when no rules are configured."""
        self.logger.warning("no_rules_configured")
        columns = [
            c.get("name", f"col_{i}")
            if isinstance(c, dict)
            else getattr(c, "name", f"col_{i}")
            for i, c in enumerate(schema.get("columns", []))
        ]
        empty_df = pd.DataFrame(
            {col: [None] * num_records for col in columns}
        )
        return self._build_result(
            empty_df, metadata={"method": "rules", "warning": "no_rules"},
        )

    def _prepare_generation_plan(
        self,
        effective: list[FieldRule],
    ) -> list[list[FieldRule]]:
        """Validate the config and resolve the topological generation order.

        Raises:
            GenerationError: On invalid configuration or circular dependencies.
        """
        try:
            self.validate_config(self.config)
        except ValueError as exc:
            raise GenerationError(
                message=f"Rules configuration invalid: {exc}",
                method="rules",
                details={"error": str(exc)},
            ) from exc

        try:
            return self._build_generation_order(effective)
        except ValueError as exc:
            raise GenerationError(
                message=f"Dependency resolution failed: {exc}",
                method="rules",
                details={"error": str(exc)},
            ) from exc

    # ------------------------------------------------------------------
    # Core Generation Method
    # ------------------------------------------------------------------

    def generate(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
        num_records: int,
        **kwargs: Any,
    ) -> GenerationResult:
        """Generate synthetic data by evaluating the configured rule set.

        Resolves cross-field dependencies via topological sort, generates
        independent fields first, then dependent fields in order, and
        finally applies null injection.

        Args:
            schema: Table schema with a ``"columns"`` key.
            profile: Statistical profile (may be empty for rules-only generation).
            num_records: Number of records to produce.
            **kwargs: Additional runtime overrides (``rules_config`` dict).

        Returns:
            :class:`GenerationResult` wrapping a :class:`pandas.DataFrame`.

        Raises:
            GenerationError: On circular dependencies, invalid rules, or
                fatal generation failures.
        """
        # ``profile`` is part of the BaseGenerator interface; rules-based
        # generation may optionally consult it but does not require it.
        _ = profile

        self._start_timer()

        self._reseed_rngs()

        self.logger.info(
            "rules_generation_started",
            num_records=num_records,
            erp_module=(
                self._rules_config.erp_module if self._rules_config else None
            ),
        )

        # Resolve the effective rule list (allows runtime override via kwargs).
        effective = self._resolve_effective_rules(kwargs)

        if not effective:
            return self._generate_empty_fallback(schema, num_records)

        # Validate, resolve dependencies, and build the generation plan.
        generation_batches = self._prepare_generation_plan(effective)

        # Reset sequence counters for this generation run.
        self._seq_counters = {}

        # Generate records.
        df = pd.DataFrame()
        total_fields = sum(len(b) for b in generation_batches)
        fields_done = 0

        for batch in generation_batches:
            for field_rule in batch:
                try:
                    values = self._generate_field(field_rule, df, num_records)
                    df[field_rule.field_name] = values

                    # Apply field-level null injection.
                    if field_rule.nullable and field_rule.null_probability > 0:
                        null_mask = np.random.random(num_records) < field_rule.null_probability
                        if null_mask.any():
                            df.loc[null_mask, field_rule.field_name] = None

                except GenerationError:
                    raise
                except Exception as exc:
                    raise GenerationError(
                        message=(
                            f"Failed to generate field '{field_rule.field_name}' "
                            f"(rule_type={field_rule.rule_type}): {exc}"
                        ),
                        method="rules",
                        details={
                            "field": field_rule.field_name,
                            "rule_type": field_rule.rule_type,
                            "error": str(exc),
                        },
                    ) from exc

                fields_done += 1
                self._log_progress(
                    records_generated=int(fields_done / total_fields * num_records),
                    total_records=num_records,
                )

        # Apply schema-level null injection (for columns defined in schema).
        df = self._apply_nulls(df, schema)

        # Build result metadata.
        metadata: dict[str, Any] = {
            "method": "rules",
            "erp_module": self._rules_config.erp_module if self._rules_config else None,
            "num_rules": len(effective),
            "num_fields_generated": len(df.columns),
            "generation_timestamp": datetime.now(tz=UTC).isoformat(),
        }

        result = self._build_result(df, metadata=metadata)
        self.logger.info(
            "rules_generation_completed",
            num_records=result.num_records,
            columns=result.columns,
            time_seconds=result.generation_time_seconds,
        )
        return result

    # ------------------------------------------------------------------
    # Field Dispatch
    # ------------------------------------------------------------------

    def _generate_field(
        self,
        field_rule: FieldRule,
        current_df: pd.DataFrame,
        size: int,
    ) -> np.ndarray:
        """Dispatch generation to the appropriate rule-type handler.

        Args:
            field_rule: The field rule to evaluate.
            current_df: DataFrame with already-generated columns.
            size: Number of values to produce.

        Returns:
            Array of generated values.
        """
        cfg = field_rule.rule_config

        if field_rule.rule_type == "range" and isinstance(cfg, RangeRule):
            return self._generate_range_field(cfg, size)

        if field_rule.rule_type == "format" and isinstance(cfg, FormatRule):
            return self._generate_format_field(cfg, size)

        if field_rule.rule_type == "lookup" and isinstance(cfg, LookupRule):
            return self._generate_lookup_field(cfg, size)

        if field_rule.rule_type == "date" and isinstance(cfg, DateRule):
            return self._generate_date_field(cfg, size)

        if field_rule.rule_type == "conditional" and isinstance(cfg, ConditionalRule):
            if cfg.depends_on not in current_df.columns:
                raise GenerationError(
                    message=(
                        f"Conditional rule for '{field_rule.field_name}' depends on "
                        f"'{cfg.depends_on}' which has not been generated yet."
                    ),
                    method="rules",
                )
            parent_values = current_df[cfg.depends_on]
            return self._generate_conditional_field(cfg, parent_values, size)

        if field_rule.rule_type == "cross_field" and isinstance(cfg, CrossFieldRule):
            missing = [f for f in cfg.source_fields if f not in current_df.columns]
            if missing:
                raise GenerationError(
                    message=(
                        f"Cross-field rule for '{field_rule.field_name}' requires "
                        f"columns {missing} which have not been generated yet."
                    ),
                    method="rules",
                )
            return self._generate_cross_field(cfg, current_df)

        raise GenerationError(
            message=f"Unsupported rule type: {field_rule.rule_type}",
            method="rules",
            details={"field": field_rule.field_name},
        )

    # ------------------------------------------------------------------
    # Range Field Generation
    # ------------------------------------------------------------------

    def _generate_range_field(self, rule: RangeRule, size: int) -> np.ndarray:
        """Generate numeric values within the specified range and distribution.

        Args:
            rule: Range constraint configuration.
            size: Number of values to generate.

        Returns:
            NumPy array of generated numeric values.
        """
        if rule.distribution == "uniform":
            values = np.random.uniform(rule.min_value, rule.max_value, size)
        elif rule.distribution == "normal":
            mean = (rule.min_value + rule.max_value) / 2.0
            std = (rule.max_value - rule.min_value) / 6.0  # ~99.7% within range
            if std <= 0:
                std = 1.0
            values = np.random.normal(mean, std, size)
        elif rule.distribution == "weighted":
            # Weighted: bias towards the centre of the range with triangular dist.
            values = np.random.triangular(
                rule.min_value,
                (rule.min_value + rule.max_value) / 2.0,
                rule.max_value,
                size,
            )
        else:
            values = np.random.uniform(rule.min_value, rule.max_value, size)

        # Clip to enforce strict range bounds.
        values = np.clip(values, rule.min_value, rule.max_value)

        # Apply step rounding if configured.
        if rule.step is not None and rule.step > 0:
            values = np.round(values / rule.step) * rule.step
            values = np.clip(values, rule.min_value, rule.max_value)

        return values

    # ------------------------------------------------------------------
    # Format Field Generation
    # ------------------------------------------------------------------

    def _resolve_format_token(
        self,
        token_upper: str,
        width: int,
        char_pool: str,
        current_year: str,
        seq_num: int,
    ) -> str:
        """Resolve a single format placeholder token to its replacement string.

        This helper is separated from :meth:`_generate_format_field` to keep
        the branch count within linting limits.

        Args:
            token_upper: Upper-cased token name (``YYYY``, ``SEQ``, etc.).
            width: Desired output width for the token.
            char_pool: Fallback character pool derived from the rule's char_set.
            current_year: Pre-computed 4-digit year string.
            seq_num: Current sequence number (used only for ``SEQ`` tokens).

        Returns:
            Replacement string for the token.
        """
        # Map well-known random-char tokens to their character sets.  Using
        # ``random.choices`` is intentional for synthetic data generation
        # (non-cryptographic), hence the S311 suppressions.
        _random_token_pools: dict[str, str] = {
            "NUM": string.digits,
            "ALPHA": string.ascii_uppercase,
            "HEX": string.hexdigits[:16].upper(),
        }

        if token_upper == "YYYY":
            return current_year
        if token_upper == "SEQ":
            return str(seq_num).zfill(width)
        pool = _random_token_pools.get(token_upper)
        if pool is not None:
            return "".join(random.choices(pool, k=width))  # noqa: S311
        # Fallback: use the configured char_set pool.
        return "".join(random.choices(char_pool, k=width))  # noqa: S311

    def _generate_format_field(self, rule: FormatRule, size: int) -> np.ndarray:
        """Generate formatted string values by expanding placeholder tokens.

        Handles ``{YYYY}``, ``{SEQ:N}``, ``{NUM:N}``, ``{ALPHA:N}``, and
        ``{HEX:N}`` tokens.

        Args:
            rule: Format pattern configuration.
            size: Number of values to generate.

        Returns:
            NumPy array of formatted strings.
        """
        pattern = rule.pattern
        results: list[str] = []

        # Determine character set for any random segments.
        charset_map: dict[str, str] = {
            "alphanumeric": string.ascii_uppercase + string.digits,
            "numeric": string.digits,
            "alpha": string.ascii_uppercase,
            "hex": string.hexdigits[:16].upper(),
        }
        char_pool = charset_map.get(rule.char_set, string.ascii_uppercase + string.digits)

        # Find all placeholders in the pattern.
        placeholders = _PLACEHOLDER_RE.findall(pattern)

        # Determine starting sequence number for SEQ placeholders.
        seq_key = f"__format_{rule.pattern}"
        if seq_key not in self._seq_counters:
            self._seq_counters[seq_key] = 1
        seq_start = self._seq_counters[seq_key]

        current_year = str(datetime.now(tz=UTC).year)

        for i in range(size):
            value = pattern
            for token, width_str in placeholders:
                width = int(width_str) if width_str else 4
                replacement = self._resolve_format_token(
                    token.upper(), width, char_pool, current_year, seq_start + i,
                )
                # Replace first occurrence of this token placeholder.
                placeholder_str = f"{{{token}:{width_str}}}" if width_str else f"{{{token}}}"
                value = value.replace(placeholder_str, replacement, 1)

            # Apply optional prefix/suffix and enforce fixed length.
            if rule.prefix is not None:
                value = rule.prefix + value
            if rule.suffix is not None:
                value = value + rule.suffix
            if rule.length is not None:
                value = value[:rule.length].ljust(rule.length)

            results.append(value)

        # Update sequence counter for next call.
        self._seq_counters[seq_key] = seq_start + size

        return np.array(results, dtype=object)

    # ------------------------------------------------------------------
    # Lookup Field Generation
    # ------------------------------------------------------------------

    def _generate_lookup_field(self, rule: LookupRule, size: int) -> np.ndarray:
        """Sample values from a lookup table with optional probability weights.

        Args:
            rule: Lookup constraint configuration.
            size: Number of values to generate.

        Returns:
            NumPy array of sampled categorical values.
        """
        values_array = np.array(rule.values, dtype=object)

        if rule.weights is not None:
            # Normalise weights to ensure they sum to 1.0.
            weights = np.array(rule.weights, dtype=np.float64)
            weight_sum = weights.sum()
            weights = weights / weight_sum if weight_sum > 0 else np.ones(len(rule.values)) / len(rule.values)
            sampled = np.random.choice(values_array, size=size, replace=True, p=weights)
        else:
            sampled = np.random.choice(values_array, size=size, replace=True)

        # Apply null injection from lookup rule settings.
        if rule.allow_null and rule.null_probability > 0:
            null_mask = np.random.random(size) < rule.null_probability
            sampled = sampled.astype(object)
            sampled[null_mask] = None

        return sampled

    # ------------------------------------------------------------------
    # Conditional Field Generation
    # ------------------------------------------------------------------

    def _generate_conditional_field(
        self,
        rule: ConditionalRule,
        parent_values: pd.Series,
        size: int,
    ) -> np.ndarray:
        """Generate values conditioned on the parent field's values.

        For each unique parent value, the corresponding child rule from
        *conditions* is evaluated.  Records with unmatched parent values
        fall back to *default_rule*.

        Args:
            rule: Conditional rule configuration.
            parent_values: Series of parent field values.
            size: Number of values to generate.

        Returns:
            NumPy array of conditionally-generated values.
        """
        result = np.empty(size, dtype=object)

        for parent_val in parent_values.unique():
            mask = (parent_values == parent_val).values
            count = int(mask.sum())
            if count == 0:
                continue

            parent_key = str(parent_val)
            child_cfg = rule.conditions.get(parent_key, rule.default_rule)

            if child_cfg is None:
                result[mask] = None
                self.logger.debug(
                    "conditional_no_match",
                    parent_value=parent_key,
                    field_count=count,
                )
                continue

            child_values = self._dispatch_child_rule(child_cfg, count)
            result[mask] = child_values

        return result

    def _dispatch_child_rule(self, rule_cfg: Any, size: int) -> np.ndarray:
        """Dispatch a child rule configuration to the appropriate generator.

        Accepts both Pydantic model instances and raw dicts.

        Args:
            rule_cfg: Rule configuration (Pydantic model or dict).
            size: Number of values to generate.

        Returns:
            NumPy array of generated values.
        """
        if isinstance(rule_cfg, RangeRule):
            return self._generate_range_field(rule_cfg, size)
        if isinstance(rule_cfg, FormatRule):
            return self._generate_format_field(rule_cfg, size)
        if isinstance(rule_cfg, LookupRule):
            return self._generate_lookup_field(rule_cfg, size)
        if isinstance(rule_cfg, DateRule):
            return self._generate_date_field(rule_cfg, size)

        # Attempt dict-based dispatch.
        if isinstance(rule_cfg, dict):
            return self._dispatch_child_rule_from_dict(rule_cfg, size)

        raise GenerationError(
            message=f"Unsupported child rule type: {type(rule_cfg).__name__}",
            method="rules",
        )

    def _dispatch_child_rule_from_dict(self, cfg: dict[str, Any], size: int) -> np.ndarray:
        """Parse a raw dict into a rule model and generate values.

        Detection heuristic based on distinguishing keys:
        - ``min_value`` / ``max_value`` → :class:`RangeRule`
        - ``pattern`` → :class:`FormatRule`
        - ``values`` → :class:`LookupRule`
        - ``start_date`` / ``end_date`` → :class:`DateRule`

        Args:
            cfg: Raw rule configuration dict.
            size: Number of values.

        Returns:
            NumPy array of generated values.
        """
        type_attempts: list[tuple[type, Callable[..., np.ndarray]]] = [
            (LookupRule, self._generate_lookup_field),
            (RangeRule, self._generate_range_field),
            (FormatRule, self._generate_format_field),
            (DateRule, self._generate_date_field),
        ]

        # Heuristic ordering by key presence.
        if "min_value" in cfg or "max_value" in cfg:
            type_attempts = [(RangeRule, self._generate_range_field)] + [
                t for t in type_attempts if t[0] is not RangeRule
            ]
        elif "pattern" in cfg:
            type_attempts = [(FormatRule, self._generate_format_field)] + [
                t for t in type_attempts if t[0] is not FormatRule
            ]
        elif "start_date" in cfg or "end_date" in cfg:
            type_attempts = [(DateRule, self._generate_date_field)] + [
                t for t in type_attempts if t[0] is not DateRule
            ]

        for rule_cls, gen_method in type_attempts:
            try:
                rule_obj = rule_cls(**cfg)
                return gen_method(rule_obj, size)
            except Exception:
                self.logger.debug(
                    "conditional_child_rule_parse_skip",
                    attempted_type=rule_cls.__name__,
                    config_keys=list(cfg.keys()),
                )
                continue

        # Last resort: return the value directly if it is a scalar.
        if "value" in cfg:
            return np.array([cfg["value"]] * size, dtype=object)

        raise GenerationError(
            message=f"Cannot parse child rule config: {cfg}",
            method="rules",
        )

    # ------------------------------------------------------------------
    # Cross-Field Generation
    # ------------------------------------------------------------------

    def _generate_cross_field(
        self,
        rule: CrossFieldRule,
        df: pd.DataFrame,
    ) -> np.ndarray:
        """Evaluate a cross-field formula using already-generated columns.

        The formula is executed via restricted ``eval`` with only the
        source column values and safe math functions in scope.

        Args:
            rule: Cross-field formula rule.
            df: DataFrame containing all source columns.

        Returns:
            NumPy array of computed values.

        Raises:
            GenerationError: If the formula is unsafe or evaluation fails.
        """
        # Safety check: reject formulas with dangerous patterns.
        if _UNSAFE_FORMULA_RE.search(rule.formula):
            raise GenerationError(
                message=f"Unsafe formula detected: {rule.formula!r}",
                method="rules",
                details={"formula": rule.formula},
            )

        # Build evaluation namespace with source field values.
        namespace: dict[str, Any] = dict(_SAFE_FORMULA_NAMES)
        for field_name in rule.source_fields:
            if field_name in df.columns:
                namespace[field_name] = df[field_name].values.astype(np.float64)
            else:
                raise GenerationError(
                    message=f"Source field '{field_name}' not found in DataFrame.",
                    method="rules",
                    details={"available_columns": list(df.columns)},
                )

        # Prevent access to built-ins.
        safe_globals: dict[str, Any] = {"__builtins__": {}}
        safe_globals.update(namespace)

        try:
            result = eval(rule.formula, safe_globals, namespace)  # noqa: S307
        except Exception as exc:
            raise GenerationError(
                message=f"Formula evaluation failed: {exc}",
                method="rules",
                details={"formula": rule.formula, "error": str(exc)},
            ) from exc

        result_array = np.array(result, dtype=np.float64)

        # Apply optional validation expression.
        if rule.validation_expression:
            if _UNSAFE_FORMULA_RE.search(rule.validation_expression):
                raise GenerationError(
                    message=f"Unsafe validation expression: {rule.validation_expression!r}",
                    method="rules",
                )
            try:
                validation_ns: dict[str, Any] = {"__builtins__": {}}
                validation_ns.update(namespace)
                validation_ns["result"] = result_array
                valid = eval(rule.validation_expression, validation_ns, validation_ns)  # noqa: S307
                if isinstance(valid, np.ndarray) and not valid.all():
                    invalid_count = int((~valid).sum())
                    self.logger.warning(
                        "cross_field_validation_failures",
                        formula=rule.formula,
                        invalid_count=invalid_count,
                    )
            except Exception as exc:
                self.logger.warning(
                    "cross_field_validation_error",
                    expression=rule.validation_expression,
                    error=str(exc),
                )

        return result_array

    # ------------------------------------------------------------------
    # Date Field Generation
    # ------------------------------------------------------------------

    def _generate_date_field(self, rule: DateRule, size: int) -> np.ndarray:
        """Generate random dates within the specified range.

        Supports business-day filtering and common holiday exclusion.

        Args:
            rule: Date range configuration.
            size: Number of dates to generate.

        Returns:
            NumPy array of formatted date strings.
        """
        start_dt = datetime.strptime(rule.start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt = datetime.strptime(rule.end_date, "%Y-%m-%d").replace(tzinfo=UTC)

        if end_dt <= start_dt:
            raise GenerationError(
                message=f"end_date ({rule.end_date}) must be after start_date ({rule.start_date}).",
                method="rules",
            )

        total_days = (end_dt - start_dt).days

        # Build pool of eligible dates if filtering is needed.
        if rule.business_days_only or rule.exclude_holidays:
            holiday_set: set[datetime] = set()
            if rule.exclude_holidays:
                for year in range(start_dt.year, end_dt.year + 1):
                    holiday_set.update(_get_us_holidays(year))

            eligible_dates: list[datetime] = []
            for day_offset in range(total_days + 1):
                candidate = start_dt + timedelta(days=day_offset)
                if rule.business_days_only and candidate.weekday() >= 5:
                    continue  # Skip Saturday (5) and Sunday (6).
                if rule.exclude_holidays and candidate in holiday_set:
                    continue
                eligible_dates.append(candidate)

            if not eligible_dates:
                raise GenerationError(
                    message="No eligible dates found after applying filters.",
                    method="rules",
                    details={
                        "start_date": rule.start_date,
                        "end_date": rule.end_date,
                        "business_days_only": rule.business_days_only,
                        "exclude_holidays": rule.exclude_holidays,
                    },
                )

            indices = np.random.randint(0, len(eligible_dates), size=size)
            selected = [eligible_dates[idx] for idx in indices]
        else:
            offsets = np.random.randint(0, total_days + 1, size=size)
            selected = [start_dt + timedelta(days=int(offset)) for offset in offsets]

        formatted = np.array(
            [d.strftime(rule.format_string) for d in selected],
            dtype=object,
        )
        return formatted

    # ------------------------------------------------------------------
    # Dependency Resolution (Topological Sort)
    # ------------------------------------------------------------------

    def _build_generation_order(
        self,
        rules: list[FieldRule],
    ) -> list[list[FieldRule]]:
        """Topological sort of field rules based on cross-field dependencies.

        Returns a list of *batches*: each batch contains rules whose
        dependencies have already been resolved in earlier batches.  Rules
        within the same batch are independent and can be generated in any
        order.

        Args:
            rules: Flat list of field rules to sort.

        Returns:
            Ordered list of rule batches.

        Raises:
            ValueError: If circular dependencies are detected.
        """
        if not rules:
            return []

        field_map: dict[str, FieldRule] = {r.field_name: r for r in rules}
        rule_field_names: set[str] = set(field_map.keys())

        # Build dependency graph considering only intra-rule-set edges.
        deps: dict[str, set[str]] = {}
        for rule in rules:
            field_deps: set[str] = set()
            cfg = rule.rule_config

            if isinstance(cfg, CrossFieldRule):
                field_deps.update(
                    f for f in cfg.source_fields if f in rule_field_names
                )
            elif isinstance(cfg, ConditionalRule) and cfg.depends_on in rule_field_names:
                field_deps.add(cfg.depends_on)

            deps[rule.field_name] = field_deps

        # Compute in-degrees.
        in_degree: dict[str, int] = {name: len(d) for name, d in deps.items()}

        # Build reverse adjacency for efficient traversal.
        reverse_deps: dict[str, set[str]] = defaultdict(set)
        for name, d in deps.items():
            for dep in d:
                reverse_deps[dep].add(name)

        # Kahn's algorithm: peel off zero-in-degree nodes in waves.
        result_batches: list[list[FieldRule]] = []
        processed: set[str] = set()

        current_names = {name for name, deg in in_degree.items() if deg == 0}

        while current_names:
            batch = [field_map[name] for name in sorted(current_names)]
            result_batches.append(batch)
            processed.update(current_names)

            next_names: set[str] = set()
            for name in current_names:
                for dependent in reverse_deps.get(name, set()):
                    if dependent in processed:
                        continue
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        next_names.add(dependent)
            current_names = next_names

        # Detect circular dependencies.
        if len(processed) != len(rules):
            unprocessed = rule_field_names - processed
            raise ValueError(
                f"Circular dependency detected among fields: {sorted(unprocessed)}"
            )

        self.logger.debug(
            "generation_order_resolved",
            num_batches=len(result_batches),
            order=[
                [r.field_name for r in batch] for batch in result_batches
            ],
        )

        return result_batches

    # ------------------------------------------------------------------
    # Configuration Validation
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate rules generator configuration.

        Checks Pydantic parsing, circular dependencies, and field
        reference consistency.

        Args:
            config: Configuration dictionary to validate.

        Returns:
            ``True`` when the configuration is valid.

        Raises:
            ValueError: With a descriptive message when validation fails.
        """
        if not config:
            return True  # Empty config is valid (uses defaults).

        # Parse config through RulesConfig Pydantic model.
        try:
            parsed = RulesConfig(**config)
        except Exception as exc:
            raise ValueError(f"Invalid rules configuration: {exc}") from exc

        # Validate ERP module reference.
        if parsed.erp_module and parsed.erp_module not in ERP_MODULE_RULES:
            raise ValueError(
                f"Unknown ERP module '{parsed.erp_module}'. "
                f"Valid modules: {list(ERP_MODULE_RULES.keys())}."
            )

        # Merge rules for dependency checking.
        module_rules = (
            list(ERP_MODULE_RULES.get(parsed.erp_module, []))
            if parsed.erp_module
            else []
        )
        all_rules = self._merge_rules_lists(module_rules, parsed.rules)

        # Check for circular dependencies.
        try:
            self._build_generation_order(all_rules)
        except ValueError:
            raise  # Re-raise with the original circular dependency message.

        # Verify cross-field source references exist.
        all_field_names = {r.field_name for r in all_rules}
        for rule in all_rules:
            if isinstance(rule.rule_config, CrossFieldRule):
                for src_field in rule.rule_config.source_fields:
                    if src_field not in all_field_names:
                        self.logger.warning(
                            "cross_field_external_dependency",
                            field=rule.field_name,
                            missing_source=src_field,
                        )

            if isinstance(rule.rule_config, ConditionalRule) and (
                rule.rule_config.depends_on not in all_field_names
            ):
                self.logger.warning(
                    "conditional_external_dependency",
                    field=rule.field_name,
                    missing_parent=rule.rule_config.depends_on,
                )

        self.logger.info(
            "config_validated",
            num_rules=len(all_rules),
            erp_module=parsed.erp_module,
        )
        return True

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def get_capabilities(self) -> dict[str, Any]:
        """Return a machine-readable description of this generator's capabilities.

        Returns:
            Capability dictionary consumed by the method selector.
        """
        return {
            "name": "rules",
            "description": "Business rules engine for constraint-based ERP data generation",
            "supported_erp_modules": [
                "financial_accounting",
                "hr",
                "sales_distribution",
                "material_management",
            ],
            "supports_gpu": False,
            "supports_training": False,
            "supports_pretrained": False,
            "best_for": [
                "erp_compliance",
                "format_enforcement",
                "value_constraints",
                "cross_field_dependencies",
            ],
            "column_types": [
                "numeric",
                "categorical",
                "datetime",
                "text",
                "formatted_string",
            ],
            "rule_types": [
                "range",
                "format",
                "lookup",
                "conditional",
                "cross_field",
                "date",
            ],
        }

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_rules_lists(
        module_rules: list[FieldRule],
        custom_rules: list[FieldRule],
    ) -> list[FieldRule]:
        """Merge module default rules with custom overrides.

        Custom rules override module defaults on a per-field basis
        (matched by ``field_name``).

        Args:
            module_rules: Default rules from :data:`ERP_MODULE_RULES`.
            custom_rules: User-supplied rule overrides.

        Returns:
            Merged list of :class:`FieldRule` instances.
        """
        custom_names = {r.field_name for r in custom_rules}
        merged = [r for r in module_rules if r.field_name not in custom_names]
        merged.extend(custom_rules)
        return merged
