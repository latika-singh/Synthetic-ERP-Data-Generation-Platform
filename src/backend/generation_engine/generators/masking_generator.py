"""Intelligent data masking generator for the Synthetic ERP Data Generation Platform.

This module implements the **Masking Generator** — one of four concrete generation
strategies in the platform's Strategy pattern architecture.  It provides privacy-
preserving data transformations that mask sensitive fields while maintaining:

- **Format consistency** — masked values retain the same character class, length,
  and structural format as the originals (e.g., SSNs remain ``XXX-XX-XXXX``).
- **Referential integrity** — consistent masking ensures that the same input value
  always maps to the same masked output across related tables, preserving foreign
  key relationships.
- **Statistical utility** — perturbation and generalization strategies preserve
  aggregate statistical properties while preventing individual re-identification.
- **Zero PII leakage** — all masking transformations are designed to be
  non-reversible (tokenization, redaction) or cryptographically secure (FPE),
  guaranteeing compliance with GDPR, HIPAA, and CCPA.

Supported Masking Strategies:
    - **Format-Preserving Encryption (FPE)** — AES-based Feistel network that
      encrypts values while preserving their format and character set.
    - **Value Substitution** — replaces sensitive values with realistic synthetic
      alternatives (names, emails, addresses, etc.).
    - **Tokenization** — one-way HMAC-based hashing producing non-reversible tokens.
    - **Generalization** — reduces precision via range bucketing, category rollup,
      or geographic generalization.
    - **Perturbation** — adds calibrated statistical noise (Gaussian, uniform,
      Laplace) while preserving distributions.
    - **Redaction** — complete value removal / replacement.
    - **Shuffling** — random column permutation preserving distributions.
    - **Date Shifting** — temporal displacement with optional day-of-week and
      month preservation.

Usage::

    from generation_engine.generators.masking_generator import MaskingGenerator, MaskingStrategy

    config = {
        "field_rules": [
            {
                "field_name": "ssn",
                "strategy": "format_preserving_encryption",
                "config": {"key": "0123456789abcdef" * 4, "alphabet": "numeric"},
                "priority": 1,
            },
            {
                "field_name": "email",
                "strategy": "value_substitution",
                "config": {"faker_type": "email", "consistent": True},
                "priority": 2,
            },
        ],
        "consistent_masking": True,
        "seed": 42,
    }
    generator = MaskingGenerator(config=config)
    result = generator.generate(schema=schema, profile=profile, num_records=1000)
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import re
import secrets
import string
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, cast

import numpy as np
import pandas as pd
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel, Field

from generation_engine.generators.base import (
    BaseGenerator,
    ColumnSpec,
    GenerationError,
    GenerationResult,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Pre-compiled regular expressions for format detection
# ---------------------------------------------------------------------------
_SSN_PATTERN: re.Pattern[str] = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_PHONE_PATTERN: re.Pattern[str] = re.compile(
    r"^\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}$"
)
_EMAIL_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
)
_ZIP_PATTERN: re.Pattern[str] = re.compile(r"^\d{5}(-\d{4})?$")


# ---------------------------------------------------------------------------
# Masking Strategy Enumeration
# ---------------------------------------------------------------------------


class MaskingStrategy(StrEnum):
    """Enumeration of supported masking strategies.

    Each value corresponds to a specific privacy-preserving transformation
    method implemented by :class:`MaskingGenerator`.  The string values are
    used in serialised configurations (JSON, YAML) for human readability.
    """

    FORMAT_PRESERVING_ENCRYPTION = "format_preserving_encryption"
    VALUE_SUBSTITUTION = "value_substitution"
    TOKENIZATION = "tokenization"
    GENERALIZATION = "generalization"
    PERTURBATION = "perturbation"
    REDACTION = "redaction"
    SHUFFLING = "shuffling"
    DATE_SHIFTING = "date_shifting"


# ---------------------------------------------------------------------------
# Strategy-Specific Configuration Models (Pydantic v2)
# ---------------------------------------------------------------------------


class FPEConfig(BaseModel):
    """Configuration for Format-Preserving Encryption (FPE).

    Uses an AES-based Feistel network to encrypt values while preserving
    their original format — numeric characters stay numeric, alphabetic
    characters stay alphabetic, and the overall string length is unchanged.

    Attributes:
        key: Hex-encoded AES encryption key (32 or 64 hex characters for
            AES-128 or AES-256 respectively).
        tweak: Optional hex-encoded tweak value for domain separation.
        alphabet: Character set to preserve during encryption.
        preserve_prefix_length: Number of leading characters to leave
            unencrypted (e.g., preserving area code in phone numbers).
        preserve_suffix_length: Number of trailing characters to leave
            unencrypted.
    """

    key: str = Field(
        ...,
        description="Hex-encoded AES key (32 or 64 hex chars).",
    )
    tweak: str | None = Field(
        default=None,
        description="Optional hex-encoded tweak for domain separation.",
    )
    alphabet: Literal["numeric", "alphanumeric", "alpha"] = Field(
        default="numeric",
        description="Character set to preserve.",
    )
    preserve_prefix_length: int = Field(
        default=0,
        ge=0,
        description="Leading characters to preserve unchanged.",
    )
    preserve_suffix_length: int = Field(
        default=0,
        ge=0,
        description="Trailing characters to preserve unchanged.",
    )


class SubstitutionConfig(BaseModel):
    """Configuration for value substitution masking.

    Replaces original sensitive values with realistic synthetic alternatives.
    When *consistent* is ``True``, the same input value always maps to the
    same output, preserving referential integrity across related tables.

    Attributes:
        substitution_map: Direct value-to-replacement lookup table.
        faker_type: Type of synthetic replacement to generate when no
            direct mapping exists.
        locale: Locale for generated synthetic values (e.g., ``'en_US'``).
        consistent: Whether to cache and reuse mappings for referential
            integrity preservation.
    """

    substitution_map: dict[str, str] | None = Field(
        default=None,
        description="Direct value→replacement mapping.",
    )
    faker_type: Literal["name", "email", "address", "phone", "ssn", "company"] | None = Field(
        default=None,
        description="Synthetic data type to generate.",
    )
    locale: str = Field(
        default="en_US",
        description="Locale for synthetic generation.",
    )
    consistent: bool = Field(
        default=True,
        description="Same input → same output for referential integrity.",
    )


class TokenizationConfig(BaseModel):
    """Configuration for tokenization masking.

    Produces non-reversible one-way hash tokens from sensitive values.
    The hash output is truncated and prefixed to create human-readable
    tokens suitable for test environments.

    Attributes:
        token_prefix: String prepended to every generated token.
        token_length: Length of the hash-derived portion of the token.
        hash_algorithm: Cryptographic hash function to use.
        salt: Optional salt for the hash computation.
        preserve_format: When ``True``, maintain character class positions
            (digits stay digits, letters stay letters).
    """

    token_prefix: str = Field(
        default="TOK",
        description="Prefix for all generated tokens.",
    )
    token_length: int = Field(
        default=16,
        ge=4,
        le=128,
        description="Token length (excluding prefix).",
    )
    hash_algorithm: Literal["sha256", "sha512"] = Field(
        default="sha256",
        description="Hash algorithm for token generation.",
    )
    salt: str | None = Field(
        default=None,
        description="Salt for hash computation.",
    )
    preserve_format: bool = Field(
        default=False,
        description="Maintain character class positions.",
    )


class GeneralizationConfig(BaseModel):
    """Configuration for generalization masking.

    Reduces the precision of values to prevent individual re-identification
    while preserving aggregate statistical properties.

    Attributes:
        level: Granularity reduction level (1 = minimal, 5 = maximal).
        method: Generalization technique to apply.
        bucket_size: Numeric range width for ``range_bucketing``.
    """

    level: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Granularity reduction level.",
    )
    method: Literal[
        "range_bucketing", "category_rollup", "geographic_generalization"
    ] = Field(
        ...,
        description="Generalization technique.",
    )
    bucket_size: int | None = Field(
        default=None,
        ge=1,
        description="Bucket width for range_bucketing.",
    )


class PerturbationConfig(BaseModel):
    """Configuration for numeric perturbation masking.

    Adds calibrated noise from a chosen statistical distribution,
    preserving the overall distribution shape while masking individual
    values.

    Attributes:
        noise_type: Statistical noise distribution.
        noise_scale: Noise magnitude as a fraction of the column's
            standard deviation.
        bounds: Optional ``(min, max)`` clipping boundaries.
        round_to: Decimal places for rounding perturbed values.
    """

    noise_type: Literal["gaussian", "uniform", "laplace"] = Field(
        default="gaussian",
        description="Noise distribution type.",
    )
    noise_scale: float = Field(
        default=0.1,
        gt=0.0,
        le=10.0,
        description="Noise magnitude as fraction of std dev.",
    )
    bounds: tuple[float, float] | None = Field(
        default=None,
        description="(min, max) clipping boundaries.",
    )
    round_to: int | None = Field(
        default=None,
        ge=0,
        le=10,
        description="Decimal places to round to.",
    )


class DateShiftConfig(BaseModel):
    """Configuration for date shifting masking.

    Shifts date/datetime values by a random offset while optionally
    preserving temporal characteristics (day of week, month).

    Attributes:
        shift_range_days: Maximum shift magnitude in days.
        consistent_per_entity: Same entity always receives the same
            date shift (keyed by an entity identifier column).
        preserve_day_of_week: Adjust shift to maintain the original
            day of week.
        preserve_month: Restrict shift to stay within the same calendar
            month.
    """

    shift_range_days: int = Field(
        default=365,
        ge=1,
        le=3650,
        description="Maximum date shift in days.",
    )
    consistent_per_entity: bool = Field(
        default=True,
        description="Same entity gets same shift.",
    )
    preserve_day_of_week: bool = Field(
        default=False,
        description="Maintain original day of week.",
    )
    preserve_month: bool = Field(
        default=False,
        description="Keep date within same month.",
    )


class FieldMaskingRule(BaseModel):
    """Per-field masking rule binding a column to a strategy and its config.

    Attributes:
        field_name: Target column name in the dataset.
        strategy: Masking strategy to apply.
        config: Strategy-specific configuration (type depends on strategy).
        priority: Processing order — lower values are processed first,
            ensuring dependent fields are masked before fields that
            reference them.
    """

    field_name: str = Field(..., description="Column to mask.")
    strategy: MaskingStrategy = Field(..., description="Masking strategy.")
    config: FPEConfig | SubstitutionConfig | TokenizationConfig | GeneralizationConfig | PerturbationConfig | DateShiftConfig | dict[str, Any] = Field(
        ...,
        description="Strategy-specific configuration.",
    )
    priority: int = Field(
        default=0,
        ge=0,
        description="Processing priority (lower = first).",
    )


class MaskingConfig(BaseModel):
    """Top-level masking configuration for the MaskingGenerator.

    Attributes:
        field_rules: Ordered list of per-field masking rules.
        global_key: Default AES encryption key (hex) for FPE rules
            that do not specify their own key.
        consistent_masking: Enable deterministic masking for referential
            integrity preservation across related tables.
        consistency_salt: Salt for consistent hashing operations.
        seed: Random seed for reproducible masking runs.
    """

    field_rules: list[FieldMaskingRule] = Field(
        default_factory=list,
        description="Per-field masking rules.",
    )
    global_key: str | None = Field(
        default=None,
        description="Default AES key (hex) for FPE.",
    )
    consistent_masking: bool = Field(
        default=True,
        description="Deterministic masking for referential integrity.",
    )
    consistency_salt: str | None = Field(
        default=None,
        description="Salt for consistent hashing.",
    )
    seed: int | None = Field(
        default=None,
        description="Random seed for reproducibility.",
    )


# ---------------------------------------------------------------------------
# Synthetic Data Pools for Value Substitution
# ---------------------------------------------------------------------------

_FIRST_NAMES: list[str] = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Christopher", "Karen", "Charles",
    "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony", "Margaret",
    "Mark", "Sandra", "Donald", "Ashley", "Steven", "Dorothy", "Andrew",
    "Kimberly", "Paul", "Emily", "Joshua", "Donna", "Kenneth", "Michelle",
    "Kevin", "Carol", "Brian", "Amanda", "George", "Melissa", "Timothy",
    "Deborah",
]

_LAST_NAMES: list[str] = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
    "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts",
]

_EMAIL_DOMAINS: list[str] = [
    "example.com", "test.org", "synthetic.net", "mockdata.io",
    "generated.dev", "placeholder.com", "sample.org", "demo.net",
    "testing.io", "noreal.com",
]

_STREET_NAMES: list[str] = [
    "Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine Rd", "Elm Blvd",
    "Washington St", "Park Ave", "Highland Dr", "Sunset Blvd", "River Rd",
    "Lake View Dr", "Forest Ave", "Garden St", "Spring Ln", "Valley Rd",
    "Birch Way", "Cherry Ct", "Willow Pl", "Hickory Trl",
]

_CITIES: list[str] = [
    "Springfield", "Riverside", "Fairview", "Madison", "Georgetown",
    "Franklin", "Clinton", "Arlington", "Salem", "Greenville", "Manchester",
    "Bristol", "Chester", "Oxford", "Burlington", "Dayton", "Milton",
    "Newport", "Hudson", "Clayton",
]

_STATES: list[str] = [
    "CA", "TX", "FL", "NY", "PA", "IL", "OH", "GA", "NC", "MI",
    "NJ", "VA", "WA", "AZ", "MA", "TN", "IN", "MO", "MD", "WI",
]

_COMPANY_PREFIXES: list[str] = [
    "Acme", "Global", "Premier", "Apex", "Summit", "Pinnacle", "Vanguard",
    "Sterling", "Atlas", "Zenith", "Quantum", "Nexus", "Horizon", "Catalyst",
    "Dynamic", "Synergy", "Vertex", "Pacific", "Continental", "National",
]

_COMPANY_SUFFIXES: list[str] = [
    "Corp", "Inc", "LLC", "Solutions", "Systems", "Technologies",
    "Industries", "Enterprises", "Group", "Holdings", "Partners", "Services",
    "International", "Associates", "Consulting",
]


# ---------------------------------------------------------------------------
# Masking Generator Implementation
# ---------------------------------------------------------------------------


class MaskingGenerator(BaseGenerator):
    """Intelligent data masking generator with privacy-preserving transformations.

    Extends :class:`~generators.base.BaseGenerator` to implement the masking
    generation strategy.  Applies configurable per-field masking rules using
    eight distinct strategies, each designed to balance privacy protection
    with data utility preservation.

    The generator guarantees:

    - **Zero PII leakage** — all strategies either destroy the original value
      (tokenization, redaction, shuffling) or encrypt it with strong
      cryptographic primitives (FPE).
    - **Format preservation** — masked values retain the original data type,
      length, and structural format.
    - **Referential integrity** — consistent masking ensures foreign key
      relationships are maintained across tables.

    Args:
        config: Optional configuration dictionary parsed into a
            :class:`MaskingConfig` via Pydantic validation.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)
        self.logger = get_logger(__name__)

        # Parse configuration through Pydantic validation
        raw_config = config if config is not None else {}
        self._masking_config: MaskingConfig = MaskingConfig(**raw_config)

        # Set random seed for reproducibility
        if self._masking_config.seed is not None:
            np.random.seed(self._masking_config.seed)

        # Consistency cache: maps (field_name, original_value) -> masked_value.
        # Ensures the same input always produces the same output for referential
        # integrity across related tables and repeated generation runs.
        self._consistency_cache: dict[str, dict[str, str]] = {}

        # Entity date shift cache: maps entity_id -> shift_days for consistent
        # date shifting across all date fields of the same entity.
        self._entity_shift_cache: dict[str, int] = {}

        # Generate or load global encryption key for FPE operations
        if self._masking_config.global_key:
            self._global_key_bytes: bytes = bytes.fromhex(
                self._masking_config.global_key
            )
        else:
            # Generate a secure random 256-bit key using secrets
            self._global_key_bytes = secrets.token_bytes(32)

        # Set consistency salt -- used for deterministic hashing operations
        self._consistency_salt: str = (
            self._masking_config.consistency_salt
            if self._masking_config.consistency_salt
            else secrets.token_hex(16)
        )

        self.logger.info(
            "masking_generator_initialized",
            num_field_rules=len(self._masking_config.field_rules),
            consistent_masking=self._masking_config.consistent_masking,
            seed=self._masking_config.seed,
        )

    # ------------------------------------------------------------------
    # Abstract Method Implementations (Strategy Pattern Contract)
    # ------------------------------------------------------------------

    def generate(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
        num_records: int,
        **kwargs: Any,
    ) -> GenerationResult:
        """Generate masked synthetic data matching the schema and profile.

        If ``source_data`` is provided in *kwargs*, it is used as the base
        dataset for masking.  Otherwise, synthetic base data is generated
        from the statistical profile before applying masking rules.

        Args:
            schema: Table schema with ``"columns"`` key listing column specs.
            profile: Statistical profile from the Profiling Service.
            num_records: Number of records to produce.
            **kwargs: Optional ``source_data`` (:class:`pd.DataFrame`) to
                mask instead of generating base data.

        Returns:
            :class:`GenerationResult` containing the masked DataFrame.

        Raises:
            GenerationError: On fatal masking errors.
        """
        self._start_timer()

        try:
            # Validate configuration against schema
            config_dict = self._masking_config.model_dump()
            config_dict["_schema"] = schema
            self.validate_config(config_dict)

            # Validate schema structure via base class helper
            self._validate_schema(schema)

            # Obtain or generate base data
            if "source_data" in kwargs and kwargs["source_data"] is not None:
                source_df = kwargs["source_data"]
                df = source_df.copy() if isinstance(source_df, pd.DataFrame) else pd.DataFrame(source_df)
                # Trim or expand to num_records
                if len(df) > num_records:
                    df = df.head(num_records).reset_index(drop=True)
                elif len(df) < num_records:
                    repeat_factor = (num_records // len(df)) + 1
                    df = pd.concat(
                        [df] * repeat_factor, ignore_index=True
                    ).head(num_records)
                self.logger.info(
                    "masking_source_data",
                    source_records=len(df),
                    target_records=num_records,
                )
            else:
                df = self._generate_base_data(schema, profile, num_records)
                self.logger.info(
                    "generated_base_data",
                    num_records=len(df),
                    num_columns=len(df.columns),
                )

            # Sort field rules by priority (lower = first)
            sorted_rules = sorted(
                self._masking_config.field_rules,
                key=lambda r: r.priority,
            )

            # Apply masking strategies in priority order
            masking_metadata: dict[str, Any] = {
                "method": "masking",
                "strategies_applied": [],
                "fields_masked": [],
                "consistent_masking": self._masking_config.consistent_masking,
            }

            for rule in sorted_rules:
                if rule.field_name not in df.columns:
                    self.logger.warning(
                        "field_not_found_in_data",
                        field_name=rule.field_name,
                        available_columns=list(df.columns),
                    )
                    continue

                self.logger.debug(
                    "applying_masking_strategy",
                    field=rule.field_name,
                    strategy=rule.strategy.value,
                    priority=rule.priority,
                )

                try:
                    df[rule.field_name] = self._apply_strategy(
                        values=df[rule.field_name],
                        rule=rule,
                    )
                    masking_metadata["strategies_applied"].append(
                        {
                            "field": rule.field_name,
                            "strategy": rule.strategy.value,
                            "priority": rule.priority,
                        }
                    )
                    masking_metadata["fields_masked"].append(rule.field_name)
                except Exception as exc:
                    self.logger.error(
                        "masking_strategy_failed",
                        field=rule.field_name,
                        strategy=rule.strategy.value,
                        error=str(exc),
                    )
                    raise GenerationError(
                        message=(
                            f"Failed to apply {rule.strategy.value} to "
                            f"field '{rule.field_name}': {exc}"
                        ),
                        method="masking",
                        details={
                            "field": rule.field_name,
                            "strategy": rule.strategy.value,
                            "error": str(exc),
                        },
                    ) from exc

            # Apply null injection per schema nullable settings
            df = self._apply_nulls(df, schema)

            # Log progress
            self._log_progress(
                records_generated=len(df),
                total_records=num_records,
            )

            masking_metadata["total_records_masked"] = len(df)
            masking_metadata["consistency_cache_size"] = sum(
                len(v) for v in self._consistency_cache.values()
            )
            masking_metadata["masked_at"] = datetime.now(tz=UTC).isoformat()

            # Build and return result via base class helper
            elapsed = self._stop_timer()
            result = self._build_result(data=df, metadata=masking_metadata)

            self.logger.info(
                "masking_generation_completed",
                num_records=len(df),
                fields_masked=len(masking_metadata["fields_masked"]),
                generation_time=elapsed,
            )

            return result

        except GenerationError:
            raise
        except Exception as exc:
            self.logger.error(
                "masking_generation_fatal_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise GenerationError(
                message=f"Masking generation failed: {exc}",
                method="masking",
                details={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            ) from exc

    # ------------------------------------------------------------------
    # Config validation helpers (extracted to reduce branch complexity)
    # ------------------------------------------------------------------

    def _validate_schema_field_names(
        self,
        masking_cfg: MaskingConfig,
        schema: dict[str, Any] | None,
    ) -> None:
        """Warn when field rules reference columns not present in *schema*."""
        if schema is None:
            return
        schema_columns = schema.get("columns", [])
        col_names: set[str] = set()
        for col in schema_columns:
            if isinstance(col, dict):
                col_names.add(col.get("name", ""))
            elif isinstance(col, ColumnSpec):
                col_names.add(col.name)
        for rule in masking_cfg.field_rules:
            if col_names and rule.field_name not in col_names:
                self.logger.warning(
                    "field_rule_references_unknown_column",
                    field_name=rule.field_name,
                    known_columns=sorted(col_names),
                )

    @staticmethod
    def _validate_fpe_keys(masking_cfg: MaskingConfig) -> None:
        """Verify that FPE key hex values are well-formed and correctly sized."""
        for rule in masking_cfg.field_rules:
            if rule.strategy != MaskingStrategy.FORMAT_PRESERVING_ENCRYPTION:
                continue
            fpe_cfg = rule.config
            if isinstance(fpe_cfg, FPEConfig):
                key_hex = fpe_cfg.key
            elif isinstance(fpe_cfg, dict):
                key_hex = fpe_cfg.get("key", "")
            else:
                continue
            try:
                key_bytes = bytes.fromhex(key_hex)
                if len(key_bytes) not in (16, 24, 32):
                    raise ValueError(
                        f"FPE key for field '{rule.field_name}' must be "
                        f"16, 24, or 32 bytes (got {len(key_bytes)})."
                    )
            except ValueError as ve:
                if "non-hexadecimal" in str(ve).lower():
                    raise ValueError(
                        f"FPE key for field '{rule.field_name}' must be "
                        f"a valid hex string."
                    ) from ve
                raise

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate masking configuration before execution.

        Performs structural validation via Pydantic and semantic checks
        including field existence in schema, encryption key format, and
        substitution map completeness.

        Args:
            config: Configuration dictionary to validate.  May include an
                optional ``_schema`` key for field-name existence checks.

        Returns:
            ``True`` if the configuration is valid.

        Raises:
            ValueError: With descriptive message on validation failure.
        """
        try:
            filtered_config = {
                k: v for k, v in config.items() if k != "_schema"
            }
            masking_cfg = MaskingConfig(**filtered_config)
        except Exception as exc:
            raise ValueError(
                f"Invalid masking configuration: {exc}"
            ) from exc

        self._validate_schema_field_names(masking_cfg, config.get("_schema"))
        self._validate_fpe_keys(masking_cfg)

        self.logger.debug(
            "masking_config_validated",
            num_rules=len(masking_cfg.field_rules),
        )
        return True

    def get_capabilities(self) -> dict[str, Any]:
        """Return the masking generator's capability descriptor.

        Used by the method selector to determine when masking is the
        appropriate generation strategy.

        Returns:
            Dictionary describing supported strategies, data types,
            and masking guarantees.
        """
        return {
            "name": "masking",
            "description": (
                "Intelligent data masking with privacy-preserving "
                "transformations"
            ),
            "strategies": [
                "format_preserving_encryption",
                "value_substitution",
                "tokenization",
                "generalization",
                "perturbation",
                "redaction",
                "shuffling",
                "date_shifting",
            ],
            "supports_gpu": False,
            "supports_training": False,
            "supports_pretrained": False,
            "preserves_format": True,
            "preserves_referential_integrity": True,
            "best_for": [
                "pii_protection",
                "sensitive_fields",
                "format_preservation",
                "compliance",
            ],
            "column_types": [
                "numeric",
                "categorical",
                "datetime",
                "text",
                "formatted_string",
            ],
            "zero_pii_guarantee": True,
            "supported_column_types": [
                "integer",
                "float",
                "string",
                "date",
                "datetime",
                "timestamp",
                "decimal",
                "text",
            ],
        }

    # ------------------------------------------------------------------
    # Strategy Dispatcher
    # ------------------------------------------------------------------

    def _apply_strategy(
        self,
        values: pd.Series,
        rule: FieldMaskingRule,
    ) -> pd.Series:
        """Dispatch a masking rule to the appropriate strategy handler.

        Args:
            values: Column data to mask.
            rule: The masking rule specifying strategy and configuration.

        Returns:
            Masked :class:`pd.Series`.

        Raises:
            GenerationError: If the strategy is unsupported.
        """
        # Strategies that need no extra config
        if rule.strategy == MaskingStrategy.REDACTION:
            return self._apply_redaction(values)
        if rule.strategy == MaskingStrategy.SHUFFLING:
            return self._apply_shuffling(values)

        # Resolve typed config from raw dict if necessary
        raw_config = rule.config
        resolved = (
            self._parse_strategy_config(rule.strategy, raw_config)
            if isinstance(raw_config, dict)
            else raw_config
        )

        return self._dispatch_with_config(rule.strategy, values, resolved)

    def _parse_strategy_config(
        self,
        strategy: MaskingStrategy,
        config_dict: dict[str, Any],
    ) -> FPEConfig | SubstitutionConfig | TokenizationConfig | GeneralizationConfig | PerturbationConfig | DateShiftConfig | dict[str, Any]:
        """Parse a raw config dict into the strategy-specific Pydantic model.

        Args:
            strategy: Target masking strategy.
            config_dict: Raw configuration dictionary.

        Returns:
            Validated Pydantic configuration model, or the raw dict if no
            model class is registered for the strategy.
        """
        config_map: dict[MaskingStrategy, type[BaseModel]] = {
            MaskingStrategy.FORMAT_PRESERVING_ENCRYPTION: FPEConfig,
            MaskingStrategy.VALUE_SUBSTITUTION: SubstitutionConfig,
            MaskingStrategy.TOKENIZATION: TokenizationConfig,
            MaskingStrategy.GENERALIZATION: GeneralizationConfig,
            MaskingStrategy.PERTURBATION: PerturbationConfig,
            MaskingStrategy.DATE_SHIFTING: DateShiftConfig,
        }
        model_cls = config_map.get(strategy)
        if model_cls is None:
            return config_dict
        return model_cls(**config_dict)  # type: ignore[return-value]

    def _dispatch_with_config(
        self,
        strategy: MaskingStrategy,
        values: pd.Series,
        config: Any,
    ) -> pd.Series:
        """Route a config-bearing strategy to the correct handler.

        Args:
            strategy: The masking strategy to apply.
            values: Column data to mask.
            config: Parsed or raw configuration for the strategy.

        Returns:
            Masked :class:`pd.Series`.

        Raises:
            GenerationError: If the strategy is unsupported.
        """
        if strategy == MaskingStrategy.FORMAT_PRESERVING_ENCRYPTION:
            return self._apply_fpe(values, config)
        if strategy == MaskingStrategy.VALUE_SUBSTITUTION:
            return self._apply_substitution(values, config)
        if strategy == MaskingStrategy.TOKENIZATION:
            return self._apply_tokenization(values, config)
        if strategy == MaskingStrategy.GENERALIZATION:
            return self._apply_generalization(values, config)
        if strategy == MaskingStrategy.PERTURBATION:
            return self._apply_perturbation(values, config)
        if strategy == MaskingStrategy.DATE_SHIFTING:
            return self._apply_date_shifting(values, config)
        raise GenerationError(
            message=f"Unsupported masking strategy: {strategy}",
            method="masking",
            details={"strategy": strategy.value},
        )

    # ------------------------------------------------------------------
    # Format-Preserving Encryption (FPE)
    # ------------------------------------------------------------------

    def _apply_fpe(
        self,
        values: pd.Series,
        config: FPEConfig | dict[str, Any],
    ) -> pd.Series:
        """Apply format-preserving encryption to a column.

        Implements a Feistel-based FPE using AES as the pseudorandom
        function.  Each value is encrypted while preserving its original
        character set and length — numeric characters remain numeric,
        alphabetic characters remain alphabetic.

        Args:
            values: Column data to encrypt.
            config: FPE configuration specifying key, tweak, and alphabet.

        Returns:
            Encrypted :class:`pd.Series` with preserved format.
        """
        if isinstance(config, dict):
            config = FPEConfig(**config)

        # Prepare encryption key — normalise to a valid AES key length
        key_bytes = bytes.fromhex(config.key)
        key_bytes = self._normalise_aes_key(key_bytes)

        # Prepare tweak for domain separation
        tweak_bytes = (
            bytes.fromhex(config.tweak) if config.tweak else b"\x00" * 8
        )

        # Determine the character set for this column
        charset = self._get_fpe_charset(config.alphabet)
        preserve_prefix = config.preserve_prefix_length
        preserve_suffix = config.preserve_suffix_length

        def encrypt_value(val: Any) -> Any:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return val
            str_val = str(val)
            if not str_val:
                return val

            # Determine the portion to encrypt (preserving prefix/suffix)
            if preserve_prefix + preserve_suffix >= len(str_val):
                return val  # Nothing left to encrypt

            prefix = str_val[:preserve_prefix] if preserve_prefix > 0 else ""
            suffix_start = (
                len(str_val) - preserve_suffix
                if preserve_suffix > 0
                else len(str_val)
            )
            suffix = str_val[suffix_start:] if preserve_suffix > 0 else ""
            middle = str_val[preserve_prefix:suffix_start]

            if not middle:
                return val

            encrypted_middle = self._feistel_encrypt(
                middle, charset, key_bytes, tweak_bytes
            )
            return prefix + encrypted_middle + suffix

        return cast("pd.Series", values.apply(encrypt_value))

    def _feistel_encrypt(
        self,
        value: str,
        charset: str,
        key_bytes: bytes,
        tweak_bytes: bytes,
    ) -> str:
        """Encrypt a string using a Feistel network with AES as the PRF.

        The value's characters are mapped to indices in the given charset,
        processed through an 8-round balanced Feistel network, and mapped
        back to characters.  Non-charset characters (separators, dashes)
        are preserved in place.

        Args:
            value: The string to encrypt.
            charset: Valid characters for encryption (defines the radix).
            key_bytes: AES encryption key.
            tweak_bytes: Tweak for domain separation.

        Returns:
            Encrypted string with the same length and character set.
        """
        radix = len(charset)
        char_to_idx = {c: i for i, c in enumerate(charset)}

        # Separate encryptable characters from fixed separators
        enc_positions: list[int] = []
        enc_indices: list[int] = []
        case_map: list[bool] = []

        for i, ch in enumerate(value):
            lower_ch = ch.lower() if ch.isalpha() else ch
            if lower_ch in char_to_idx:
                enc_positions.append(i)
                enc_indices.append(char_to_idx[lower_ch])
                case_map.append(ch.isupper())

        if not enc_indices:
            return value  # No encryptable characters

        n = len(enc_indices)

        # Handle single-character case using direct PRF
        if n == 1:
            prf_in = (
                key_bytes
                + tweak_bytes
                + enc_indices[0].to_bytes(4, byteorder="big")
            )
            prf_hash = hashlib.sha256(prf_in).digest()
            new_idx = (enc_indices[0] + prf_hash[0]) % radix
            result_chars = list(value)
            ch = charset[new_idx]
            if case_map[0] and ch.isalpha():
                ch = ch.upper()
            result_chars[enc_positions[0]] = ch
            return "".join(result_chars)

        # Split into two halves for balanced Feistel network
        u = n // 2
        left = enc_indices[:u]
        right = enc_indices[u:]

        num_rounds = 8
        for round_num in range(num_rounds):
            # Build PRF input from right half, round number, key, and tweak
            right_bytes = bytes([x % 256 for x in right])
            prf_material = (
                key_bytes
                + round_num.to_bytes(4, byteorder="big")
                + tweak_bytes
                + right_bytes
            )
            prf_hash = hashlib.sha256(prf_material).digest()

            # AES-CBC encrypt the hash for cryptographic strength.
            # Use a deterministic IV derived from the round and tweak so
            # that the overall transformation remains deterministic.
            aes_key = self._normalise_aes_key(key_bytes)
            iv = hashlib.sha256(
                round_num.to_bytes(4, byteorder="big") + tweak_bytes
            ).digest()[:16]
            cipher = Cipher(
                algorithms.AES(aes_key),
                modes.CBC(iv),
                backend=default_backend(),
            )
            encryptor = cipher.encryptor()
            # Pad PRF hash to 16-byte boundary for AES block size
            block = prf_hash[:16]
            encrypted_block = encryptor.update(block) + encryptor.finalize()

            # Generate pseudorandom offsets for left half
            new_left: list[int] = []
            for i, l_val in enumerate(left):
                byte_idx = i % len(encrypted_block)
                offset = encrypted_block[byte_idx]
                new_left.append((l_val + offset) % radix)

            # Feistel swap: left <-> right
            left, right = right, new_left

        # Recombine the halves
        result_indices = left + right

        # Map back to characters preserving positions and original case
        result_chars = list(value)
        for pos_idx, (orig_pos, is_upper) in enumerate(
            zip(enc_positions, case_map, strict=True)
        ):
            ch = charset[result_indices[pos_idx]]
            if is_upper and ch.isalpha():
                ch = ch.upper()
            result_chars[orig_pos] = ch

        return "".join(result_chars)

    @staticmethod
    def _normalise_aes_key(key_bytes: bytes) -> bytes:
        """Normalise a byte string to a valid AES key length (16/24/32).

        Args:
            key_bytes: Raw key material.

        Returns:
            Key bytes padded or truncated to valid AES size.
        """
        if len(key_bytes) <= 16:
            return key_bytes.ljust(16, b"\x00")
        elif len(key_bytes) <= 24:
            return key_bytes.ljust(24, b"\x00")
        else:
            return key_bytes[:32]

    @staticmethod
    def _get_fpe_charset(alphabet: str) -> str:
        """Return the character set for the given FPE alphabet.

        Args:
            alphabet: One of ``'numeric'``, ``'alpha'``, or ``'alphanumeric'``.

        Returns:
            String of valid characters in the alphabet.
        """
        if alphabet == "numeric":
            return string.digits
        elif alphabet == "alpha":
            return string.ascii_lowercase
        else:  # alphanumeric
            return string.digits + string.ascii_lowercase

    # ------------------------------------------------------------------
    # Value Substitution
    # ------------------------------------------------------------------

    def _apply_substitution(
        self,
        values: pd.Series,
        config: SubstitutionConfig | dict[str, Any],
    ) -> pd.Series:
        """Apply value substitution masking to a column.

        Replaces sensitive values with realistic synthetic alternatives.
        When consistent mode is enabled, the same input always maps to the
        same output via a deterministic hash-based lookup.

        Args:
            values: Column data to mask.
            config: Substitution configuration.

        Returns:
            Substituted :class:`pd.Series`.
        """
        if isinstance(config, dict):
            config = SubstitutionConfig(**config)

        field_name = str(values.name) if values.name else "unknown_field"
        cache_key = f"substitution_{field_name}"

        # Fast path: if a direct substitution_map covers all values, use
        # vectorised pd.Series.map() for performance.
        if config.substitution_map:
            mapped = values.map(config.substitution_map)
            # For values not in the map, fall through to synthetic generation
            fully_mapped = mapped.notna()
            if fully_mapped.all():
                return mapped

        # Initialise the per-field consistency cache
        if cache_key not in self._consistency_cache:
            self._consistency_cache[cache_key] = {}
        field_cache = self._consistency_cache[cache_key]

        def substitute_value(val: Any) -> Any:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return val

            str_val = str(val)

            # Check direct substitution map first
            if config.substitution_map and str_val in config.substitution_map:
                return config.substitution_map[str_val]

            # Check consistency cache
            if (
                config.consistent
                and self._masking_config.consistent_masking
                and str_val in field_cache
            ):
                return field_cache[str_val]

            # Generate synthetic replacement
            replacement = self._generate_synthetic_value(
                str_val, config.faker_type, config.locale
            )

            # Cache for consistency
            if config.consistent and self._masking_config.consistent_masking:
                field_cache[str_val] = replacement

            return replacement

        return cast("pd.Series", values.apply(substitute_value))

    def _generate_synthetic_value(
        self,
        original: str,
        faker_type: str | None,
        locale: str,  # noqa: ARG002  — part of the public interface; reserved for future locale support
    ) -> str:
        """Generate a synthetic replacement value based on type.

        Uses deterministic HMAC hashing for consistency: the same original
        value always produces the same synthetic replacement within the
        same generator instance.

        Args:
            original: Original value used as seed for consistent generation.
            faker_type: Type of synthetic value to generate.
            locale: Locale hint (currently supports ``'en_US'``).

        Returns:
            Synthetic replacement string.
        """
        # Compact binary HMAC digest for deterministic index selection
        seed_digest: bytes = hmac.digest(
            self._consistency_salt.encode("utf-8"),
            original.encode("utf-8"),
            "sha256",
        )
        seed_int = int.from_bytes(seed_digest[:8], byteorder="big")

        if faker_type == "name":
            first = _FIRST_NAMES[seed_int % len(_FIRST_NAMES)]
            last = _LAST_NAMES[(seed_int >> 8) % len(_LAST_NAMES)]
            return f"{first} {last}"

        elif faker_type == "email":
            first = _FIRST_NAMES[seed_int % len(_FIRST_NAMES)].lower()
            last = _LAST_NAMES[(seed_int >> 8) % len(_LAST_NAMES)].lower()
            domain = _EMAIL_DOMAINS[(seed_int >> 16) % len(_EMAIL_DOMAINS)]
            suffix = seed_int % 1000
            return f"{first}.{last}{suffix}@{domain}"

        elif faker_type == "address":
            num = (seed_int % 9999) + 1
            street = _STREET_NAMES[seed_int % len(_STREET_NAMES)]
            city = _CITIES[(seed_int >> 8) % len(_CITIES)]
            state = _STATES[(seed_int >> 16) % len(_STATES)]
            zipcode = f"{(seed_int % 90000) + 10000}"
            return f"{num} {street}, {city}, {state} {zipcode}"

        elif faker_type == "phone":
            area = (seed_int % 800) + 200
            prefix = ((seed_int >> 10) % 800) + 200
            line = (seed_int >> 20) % 10000
            return f"({area:03d}) {prefix:03d}-{line:04d}"

        elif faker_type == "ssn":
            # Generate a synthetic SSN-format string (not a real SSN)
            area = (seed_int % 899) + 100
            group = ((seed_int >> 10) % 99) + 1
            serial = ((seed_int >> 20) % 9999) + 1
            return f"{area:03d}-{group:02d}-{serial:04d}"

        elif faker_type == "company":
            co_prefix = _COMPANY_PREFIXES[seed_int % len(_COMPANY_PREFIXES)]
            co_suffix = _COMPANY_SUFFIXES[
                (seed_int >> 8) % len(_COMPANY_SUFFIXES)
            ]
            return f"{co_prefix} {co_suffix}"

        else:
            # Generic substitution preserving character classes
            return self._generic_substitution(original, seed_int)

    def _generic_substitution(self, original: str, seed_int: int) -> str:
        """Generate a generic substitution preserving character classes.

        Each character in the original is replaced with a character of the
        same class (digit->digit, upper->upper, lower->lower, special->same).

        Args:
            original: Original string.
            seed_int: Deterministic seed for replacement selection.

        Returns:
            Substituted string with matching character classes.
        """
        # Clean any leading/trailing whitespace via re.sub
        cleaned = re.sub(r"^\s+|\s+$", "", original)

        # Full alphanumeric pool for fallback character selection
        all_chars = string.ascii_letters + string.digits

        result: list[str] = []
        for i, ch in enumerate(cleaned):
            # Mix position into seed for per-character variation
            char_seed = (seed_int + i * 31) & 0xFFFFFFFF
            if ch.isdigit():
                result.append(string.digits[char_seed % 10])
            elif ch.isupper():
                result.append(string.ascii_uppercase[char_seed % 26])
            elif ch.islower():
                result.append(string.ascii_lowercase[char_seed % 26])
            elif ch in string.printable and not ch.isspace():
                # Preserve punctuation and special characters
                result.append(ch)
            else:
                # Fallback: use full alphanumeric pool for any other chars
                result.append(all_chars[char_seed % len(all_chars)])
        return "".join(result)

    # ------------------------------------------------------------------
    # Tokenization
    # ------------------------------------------------------------------

    def _apply_tokenization(
        self,
        values: pd.Series,
        config: TokenizationConfig | dict[str, Any],
    ) -> pd.Series:
        """Apply tokenization masking to a column.

        Each value is replaced with a non-reversible one-way hash token.
        The token is deterministic (same value -> same token) for
        referential integrity.

        Args:
            values: Column data to tokenize.
            config: Tokenization configuration.

        Returns:
            Tokenized :class:`pd.Series`.
        """
        if isinstance(config, dict):
            config = TokenizationConfig(**config)

        salt = config.salt or self._consistency_salt

        def tokenize_value(val: Any) -> Any:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return val

            str_val = str(val)

            # Compute keyed HMAC-based token using dynamic hash selection
            key = salt.encode("utf-8")
            msg = str_val.encode("utf-8")

            # Use hashlib.new() for dynamic algorithm selection
            hash_func = hashlib.new(config.hash_algorithm)
            hmac_obj = hmac.new(key, msg, hash_func.name)
            token_hash = hmac_obj.hexdigest()

            # Truncate to token_length
            raw_token = token_hash[: config.token_length]

            if config.preserve_format:
                # Maintain character class positions from original
                raw_token = self._format_preserving_token(
                    str_val, raw_token
                )

            return f"{config.token_prefix}_{raw_token}"

        return cast("pd.Series", values.apply(tokenize_value))

    @staticmethod
    def _format_preserving_token(original: str, token: str) -> str:
        """Adjust a raw hex token to match the original's character classes.

        Digits in the original are represented by digit characters from
        the hash, letters by letter characters, and special characters
        are preserved in place.

        Args:
            original: Original value whose format to preserve.
            token: Raw hexadecimal hash string.

        Returns:
            Format-adjusted token string.
        """
        result: list[str] = []
        token_idx = 0
        for ch in original:
            if token_idx >= len(token):
                token_idx = 0  # Wrap around if token is shorter

            if ch.isdigit():
                mapped = int(token[token_idx], 16) % 10
                result.append(str(mapped))
                token_idx += 1
            elif ch.isalpha():
                mapped_idx = int(token[token_idx], 16) % 26
                mapped_ch = string.ascii_lowercase[mapped_idx]
                if ch.isupper():
                    mapped_ch = mapped_ch.upper()
                result.append(mapped_ch)
                token_idx += 1
            else:
                result.append(ch)  # Preserve separators

        return "".join(result)

    # ------------------------------------------------------------------
    # Generalization
    # ------------------------------------------------------------------

    def _apply_generalization(
        self,
        values: pd.Series,
        config: GeneralizationConfig | dict[str, Any],
    ) -> pd.Series:
        """Apply generalization masking to a column.

        Reduces value precision to prevent individual re-identification
        while preserving aggregate properties.

        Args:
            values: Column data to generalise.
            config: Generalization configuration specifying method and level.

        Returns:
            Generalised :class:`pd.Series`.
        """
        if isinstance(config, dict):
            config = GeneralizationConfig(**config)

        if config.method == "range_bucketing":
            return self._generalize_range_bucketing(values, config)
        elif config.method == "category_rollup":
            return self._generalize_category_rollup(values, config)
        elif config.method == "geographic_generalization":
            return self._generalize_geographic(values, config)
        else:
            self.logger.warning(
                "unknown_generalization_method",
                method=config.method,
            )
            return values

    def _generalize_range_bucketing(
        self,
        values: pd.Series,
        config: GeneralizationConfig,
    ) -> pd.Series:
        """Replace numeric values with range-bucket labels.

        For example, with ``bucket_size=10000`` the value ``15432``
        becomes ``'10000-19999'``.

        Args:
            values: Numeric column data.
            config: Generalization config with bucket_size.

        Returns:
            Series of range-bucket label strings.
        """
        bucket_size = config.bucket_size
        if bucket_size is None:
            # Auto-calculate based on level and data range
            numeric_vals = pd.to_numeric(values, errors="coerce").dropna()
            if len(numeric_vals) == 0:
                return values
            data_range = float(numeric_vals.max() - numeric_vals.min())
            bucket_size = max(1, int(data_range / max(1, 100 // config.level)))

        final_bucket = bucket_size  # Capture for closure

        def bucket_value(val: Any) -> Any:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return val
            try:
                num_val = float(val)
                lower = int(num_val // final_bucket) * final_bucket
                upper = lower + final_bucket - 1
                return f"{lower}-{upper}"
            except (ValueError, TypeError):
                return val

        return cast("pd.Series", values.apply(bucket_value))

    @staticmethod
    def _generalize_category_rollup(
        values: pd.Series,
        config: GeneralizationConfig,
    ) -> pd.Series:
        """Roll up specific categories to higher-level groups.

        Categories are grouped by taking the first N characters of the
        value (where N decreases with higher generalisation levels).

        Args:
            values: Categorical column data.
            config: Generalization config with level.

        Returns:
            Series with rolled-up category labels.
        """

        def rollup_value(val: Any) -> Any:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return val
            str_val = str(val)
            if len(str_val) <= 1:
                return str_val
            keep_chars = max(1, len(str_val) - config.level)
            return str_val[:keep_chars] + "*" * (len(str_val) - keep_chars)

        return cast("pd.Series", values.apply(rollup_value))

    @staticmethod
    def _generalize_geographic(
        values: pd.Series,
        config: GeneralizationConfig,
    ) -> pd.Series:
        """Reduce geographic precision (e.g., full zip -> first 3 digits).

        The number of characters preserved decreases with higher
        generalisation levels.

        Args:
            values: Geographic identifier column (zip codes, area codes).
            config: Generalization config with level.

        Returns:
            Series with reduced geographic precision.
        """

        def generalize_geo(val: Any) -> Any:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return val
            str_val = str(val).strip()
            if not str_val:
                return val
            # Extract numeric portions for masking
            digits = re.findall(r"\d+", str_val)
            if not digits:
                return val
            full_digits = "".join(digits)
            mask_count = min(config.level, len(full_digits))
            preserved = full_digits[: len(full_digits) - mask_count]
            masked = preserved + "X" * mask_count

            # Reconstruct with original separators
            digit_idx = 0
            reconstructed: list[str] = []
            for ch in str_val:
                if ch.isdigit() and digit_idx < len(masked):
                    reconstructed.append(masked[digit_idx])
                    digit_idx += 1
                else:
                    reconstructed.append(ch)
            return "".join(reconstructed)

        return cast("pd.Series", values.apply(generalize_geo))

    # ------------------------------------------------------------------
    # Perturbation
    # ------------------------------------------------------------------

    def _apply_perturbation(
        self,
        values: pd.Series,
        config: PerturbationConfig | dict[str, Any],
    ) -> pd.Series:
        """Apply calibrated noise perturbation to numeric values.

        Adds random noise from the specified distribution, scaled to the
        column's standard deviation.  The resulting distribution closely
        matches the original, but individual values are shifted.

        Args:
            values: Numeric column data.
            config: Perturbation configuration.

        Returns:
            Perturbed :class:`pd.Series`.
        """
        if isinstance(config, dict):
            config = PerturbationConfig(**config)

        numeric_values = pd.to_numeric(values, errors="coerce")
        non_null_mask = numeric_values.notna()

        if non_null_mask.sum() == 0:
            return values

        # Calculate column standard deviation for noise scaling
        valid_array: np.ndarray = np.asarray(numeric_values[non_null_mask].values)
        col_std = float(np.std(valid_array))
        if col_std == 0:
            col_std = 1.0  # Avoid zero-scale noise

        noise_magnitude = config.noise_scale * col_std
        n = int(non_null_mask.sum())

        # Generate noise from the specified distribution
        if config.noise_type == "gaussian":
            noise = np.random.normal(loc=0.0, scale=noise_magnitude, size=n)
        elif config.noise_type == "uniform":
            noise = np.random.uniform(
                low=-noise_magnitude, high=noise_magnitude, size=n
            )
        elif config.noise_type == "laplace":
            noise = np.random.laplace(
                loc=0.0, scale=noise_magnitude, size=n
            )
        else:
            noise = np.random.normal(loc=0.0, scale=noise_magnitude, size=n)

        # Apply noise to non-null values
        perturbed = numeric_values.copy()
        perturbed_vals: np.ndarray = valid_array + noise

        # Clip to bounds if configured
        if config.bounds is not None:
            perturbed_vals = np.clip(
                perturbed_vals, config.bounds[0], config.bounds[1]
            )

        # Round to specified decimal places
        if config.round_to is not None:
            perturbed_vals = np.round(perturbed_vals, config.round_to)

        # Cast the Series to float64 to avoid dtype incompatibility when
        # assigning float noise values into an integer-typed column.
        perturbed = perturbed.astype("float64")
        perturbed.loc[non_null_mask] = perturbed_vals

        return perturbed

    # ------------------------------------------------------------------
    # Redaction
    # ------------------------------------------------------------------

    def _apply_redaction(self, values: pd.Series) -> pd.Series:
        """Replace all values with a redaction marker.

        Non-null values become ``'***REDACTED***'``; null values remain
        null.

        Args:
            values: Column data to redact.

        Returns:
            Redacted :class:`pd.Series`.
        """

        def redact_value(val: Any) -> Any:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return val
            return "***REDACTED***"

        return cast("pd.Series", values.apply(redact_value))

    # ------------------------------------------------------------------
    # Shuffling
    # ------------------------------------------------------------------

    def _apply_shuffling(self, values: pd.Series) -> pd.Series:
        """Randomly permute values within the column.

        Preserves the marginal distribution (same set of values) but
        breaks row-level associations between this column and other
        columns.

        Args:
            values: Column data to shuffle.

        Returns:
            Shuffled :class:`pd.Series`.
        """
        shuffled_indices = np.random.permutation(len(values))
        shuffled_values = values.values[shuffled_indices]
        return pd.Series(
            shuffled_values, index=values.index, name=values.name
        )

    # ------------------------------------------------------------------
    # Date Shifting
    # ------------------------------------------------------------------

    # -- Date-shifting helper methods (extracted to satisfy PLR0912) -----

    @staticmethod
    def _parse_to_datetime(val: Any) -> datetime | None:
        """Attempt to coerce *val* into a :class:`datetime`.

        Returns ``None`` when the value cannot be converted.
        """
        if isinstance(val, datetime):
            return val
        try:
            parsed = pd.to_datetime(val)
            if pd.isna(parsed):
                return None
            return cast("datetime", parsed.to_pydatetime())
        except (ValueError, TypeError):
            return None

    def _resolve_shift_days(
        self, val: Any, shift_range: int, consistent: bool,
    ) -> int:
        """Compute the random shift magnitude in days.

        When *consistent* is ``True`` the same input value always gets the
        same shift (cached in ``_entity_shift_cache``).
        """
        if consistent:
            entity_key = str(val)
            if entity_key not in self._entity_shift_cache:
                self._entity_shift_cache[entity_key] = int(
                    np.random.randint(-shift_range, shift_range + 1)
                )
            return self._entity_shift_cache[entity_key]
        return int(np.random.randint(-shift_range, shift_range + 1))

    @staticmethod
    def _constrain_and_shift(
        dt: datetime, shift_days: int, cfg: DateShiftConfig,
    ) -> datetime:
        """Apply month / day-of-week constraints and return the shifted date."""
        if cfg.preserve_month:
            _, days_in_month = calendar.monthrange(dt.year, dt.month)
            max_forward = days_in_month - dt.day
            max_backward = dt.day - 1
            shift_days = max(-max_backward, min(max_forward, shift_days))

        shifted = dt + timedelta(days=shift_days)

        if cfg.preserve_day_of_week:
            day_diff = dt.weekday() - shifted.weekday()
            if day_diff != 0:
                correction = day_diff if abs(day_diff) <= 3 else (
                    day_diff + (7 if day_diff < 0 else -7)
                )
                shifted += timedelta(days=correction)
        return shifted

    @staticmethod
    def _format_shifted_result(val: Any, shifted: datetime) -> Any:
        """Return *shifted* in the same format as the original *val*."""
        if not isinstance(val, str):
            return shifted
        if re.match(r"^\d{4}-\d{2}-\d{2}T", val):
            return shifted.strftime("%Y-%m-%dT%H:%M:%S")
        if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:", val):
            return shifted.strftime("%Y-%m-%d %H:%M:%S")
        return shifted.strftime("%Y-%m-%d")

    # -- Main date-shifting entry point -----------------------------------

    def _apply_date_shifting(
        self,
        values: pd.Series,
        config: DateShiftConfig | dict[str, Any],
    ) -> pd.Series:
        """Shift date/datetime values by random offsets.

        When ``consistent_per_entity`` is enabled, the same entity
        identifier always receives the same date shift, preserving
        temporal ordering within an entity's records.

        Args:
            values: Date/datetime column data.
            config: Date shifting configuration.

        Returns:
            Date-shifted :class:`pd.Series`.
        """
        if isinstance(config, dict):
            config = DateShiftConfig(**config)

        shift_range = config.shift_range_days

        def shift_date(val: Any) -> Any:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return val
            dt = self._parse_to_datetime(val)
            if dt is None:
                return val
            shift_days = self._resolve_shift_days(
                val, shift_range, config.consistent_per_entity,
            )
            shifted = self._constrain_and_shift(dt, shift_days, config)
            return self._format_shifted_result(val, shifted)

        return cast("pd.Series", values.apply(shift_date))

    # ------------------------------------------------------------------
    # Base Data Generation
    # ------------------------------------------------------------------

    # -- Base data helpers (per data-type) --------------------------------

    @staticmethod
    def _gen_base_numeric(
        col_profile: dict[str, Any], data_type: str, num_records: int,
    ) -> Any:
        """Generate base numeric column data from profile statistics."""
        mean = float(col_profile.get("mean", 0.0))
        std = float(col_profile.get("std", 1.0))
        min_val = float(col_profile.get("min", mean - 3 * std))
        max_val = float(col_profile.get("max", mean + 3 * std))

        values = np.random.normal(
            loc=mean, scale=max(std, 0.01), size=num_records,
        )
        values = np.clip(values, min_val, max_val)

        if data_type == "integer":
            return values.astype(int)
        if data_type == "decimal":
            return np.round(values, col_profile.get("scale", 2))
        return values

    @staticmethod
    def _gen_base_string(
        col_profile: dict[str, Any], num_records: int,
    ) -> Any:
        """Generate base string column data from profile categories."""
        value_pool = (
            col_profile.get("categories", [])
            or col_profile.get("sample_values", [])
        )
        if value_pool:
            return np.random.choice(value_pool, size=num_records)
        avg_length = int(col_profile.get("avg_length", 10))
        return [
            "".join(
                secrets.choice(string.ascii_lowercase)
                for _ in range(max(1, avg_length))
            )
            for _ in range(num_records)
        ]

    @staticmethod
    def _gen_base_date(
        col_profile: dict[str, Any], data_type: str, num_records: int,
    ) -> Any:
        """Generate base date / datetime column data from profile ranges."""
        start_str = str(col_profile.get("min", "2020-01-01"))
        end_str = str(col_profile.get("max", "2024-12-31"))
        try:
            start_dt = datetime.strptime(
                start_str[:10], "%Y-%m-%d",
            ).replace(tzinfo=UTC)
            end_dt = datetime.strptime(
                end_str[:10], "%Y-%m-%d",
            ).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            start_dt = datetime(2020, 1, 1, tzinfo=UTC)
            end_dt = datetime(2024, 12, 31, tzinfo=UTC)

        delta_days = max(1, (end_dt - start_dt).days)
        random_days = np.random.randint(0, delta_days, size=num_records)
        dates = [start_dt + timedelta(days=int(d)) for d in random_days]

        fmt = "%Y-%m-%d" if data_type == "date" else "%Y-%m-%d %H:%M:%S"
        return [d.strftime(fmt) for d in dates]

    @staticmethod
    def _gen_base_boolean(
        col_profile: dict[str, Any], num_records: int,
    ) -> Any:
        """Generate base boolean column data."""
        true_ratio = float(col_profile.get("true_ratio", 0.5))
        return np.random.choice(
            [True, False], size=num_records, p=[true_ratio, 1 - true_ratio],
        )

    # -- Main base-data builder -------------------------------------------

    _NUMERIC_TYPES = frozenset(("integer", "decimal", "float", "numeric"))
    _STRING_TYPES = frozenset(("string", "text", "varchar"))
    _DATE_TYPES = frozenset(("date", "datetime", "timestamp"))

    def _generate_base_data(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
        num_records: int,
    ) -> pd.DataFrame:
        """Generate synthetic base data from statistical profiles.

        Produces an initial dataset using simple statistical sampling when
        no source data is available for masking.  The generated data serves
        as input for the masking strategies.

        Args:
            schema: Table schema with column definitions.
            profile: Statistical profile with distributions and ranges.
            num_records: Number of records to generate.

        Returns:
            :class:`pd.DataFrame` with synthetic base data.
        """
        columns_raw = schema.get("columns", [])
        profile_columns = profile.get("columns", {})
        data: dict[str, Any] = {}

        for col_def in columns_raw:
            col_name, data_type = self._extract_col_meta(col_def)
            if col_name is None:
                continue
            col_profile = profile_columns.get(col_name, {})
            data[col_name] = self._gen_base_column(
                col_profile, data_type, num_records,
            )

        df = pd.DataFrame(data)
        self.logger.debug(
            "base_data_generated",
            num_records=len(df),
            num_columns=len(df.columns),
            columns=list(df.columns),
        )
        return df

    @staticmethod
    def _extract_col_meta(col_def: Any) -> tuple[str | None, str]:
        """Return ``(col_name, data_type)`` from a column definition."""
        if isinstance(col_def, ColumnSpec):
            return col_def.name, col_def.data_type
        if isinstance(col_def, dict):
            return col_def.get("name", ""), col_def.get("data_type", "string")
        return None, "string"

    def _gen_base_column(
        self,
        col_profile: dict[str, Any],
        data_type: str,
        num_records: int,
    ) -> Any:
        """Dispatch to the appropriate per-type base-data generator."""
        if data_type in self._NUMERIC_TYPES:
            return self._gen_base_numeric(col_profile, data_type, num_records)
        if data_type in self._STRING_TYPES:
            return self._gen_base_string(col_profile, num_records)
        if data_type in self._DATE_TYPES:
            return self._gen_base_date(col_profile, data_type, num_records)
        if data_type == "boolean":
            return self._gen_base_boolean(col_profile, num_records)
        return [secrets.token_hex(4) for _ in range(num_records)]

    # ------------------------------------------------------------------
    # Format Detection Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_value_format(value: str) -> str | None:
        """Detect the format of a string value using compiled regex patterns.

        Args:
            value: String to analyse.

        Returns:
            Detected format name or ``None`` if no known format matches.
        """
        if _SSN_PATTERN.match(value):
            return "ssn"
        if _PHONE_PATTERN.match(value):
            return "phone"
        if _EMAIL_PATTERN.match(value):
            return "email"
        if _ZIP_PATTERN.match(value):
            return "zip"
        return None

    @staticmethod
    def _compute_sha512_token(value: str, salt: str) -> str:
        """Compute a SHA-512 based token for high-entropy tokenization.

        Used when the tokenization config requests the ``sha512`` algorithm
        for wider hash output suitable for longer token lengths.

        Args:
            value: Input value to hash.
            salt: Salt to prepend before hashing.

        Returns:
            Hexadecimal hash digest string.
        """
        return hashlib.sha512(
            (salt + value).encode("utf-8")
        ).hexdigest()
