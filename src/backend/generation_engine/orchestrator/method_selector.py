"""Intelligent per-column generation method selector.

This module implements the core decision logic for the Synthetic ERP Data
Generation Platform's orchestrator, analyzing each column's data type,
statistical profile, generation requirements, and privacy constraints to
choose the optimal generation method independently per column.

The :class:`MethodSelector` applies a priority-ordered **decision matrix**
(:data:`METHOD_SELECTION_RULES`) that maps column characteristics to the
most appropriate generator strategy:

- **PII-flagged columns** → :attr:`GenerationMethod.MASKING` (zero-PII
  guarantee)
- **ERP-formatted fields** → :attr:`GenerationMethod.RULES` (document
  numbers, account codes, cost centres)
- **Well-fitted distributions** → :attr:`GenerationMethod.STATISTICAL`
  (parametric synthesis via SciPy/NumPy)
- **Complex multivariate patterns** → :attr:`GenerationMethod.AI_ML`
  (GAN/VAE preserving cross-column dependencies)

The primary output is a dictionary mapping table names to lists of
:class:`MethodAssignment` objects, consumed by the
:class:`~generation_engine.orchestrator.batch_processor.BatchProcessor` for
per-column generation delegation.

Supports the four initial-release ERP modules:

1. Financial Accounting (GL entries, invoices, payments)
2. Human Resources (employee records, payroll, benefits)
3. Sales & Distribution (orders, customers, pricing)
4. Material Management (inventory, purchase orders, vendors)

Usage::

    from generation_engine.orchestrator.method_selector import MethodSelector

    selector = MethodSelector()
    assignments = selector.select_methods(
        schema_definition=schema,
        statistical_profile=profile,
        generation_config=config,
    )
    summary = selector.get_selection_summary(assignments)
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from generation_engine.generators import (
    GENERATOR_REGISTRY,
    get_available_methods,
    get_generator,
)
from generation_engine.generators.base import BaseGenerator, ColumnSpec
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class GenerationMethod(str, Enum):
    """Enumeration of the four supported synthetic data generation strategies.

    Using ``str`` as a mixin enables direct string comparison and JSON
    serialisation of method assignments without custom encoders.

    Members:
        AI_ML: GAN/VAE-based generation for complex multivariate patterns.
        RULES: Business-rules engine for ERP-formatted and categorical fields.
        STATISTICAL: Distribution-fitting synthesis via SciPy/NumPy.
        MASKING: Privacy-preserving masking for PII-flagged columns.
    """

    AI_ML = "ai_ml"
    RULES = "rules"
    STATISTICAL = "statistical"
    MASKING = "masking"


# ---------------------------------------------------------------------------
# PII Column Name Patterns
# ---------------------------------------------------------------------------

PII_COLUMN_PATTERNS: Dict[str, List[str]] = {
    "ssn": [
        "ssn",
        "social_security",
        "social_security_number",
        "tax_id",
        "tax_identification",
        "taxpayer_id",
        "sin",
        "national_id",
        "national_insurance",
        "personal_id",
        "identity_number",
    ],
    "email": [
        "email",
        "e_mail",
        "email_address",
        "email_addr",
        "mail",
        "contact_email",
        "work_email",
        "personal_email",
    ],
    "phone": [
        "phone",
        "phone_number",
        "mobile",
        "mobile_number",
        "telephone",
        "tel",
        "fax",
        "fax_number",
        "cell_phone",
        "home_phone",
        "work_phone",
        "contact_phone",
    ],
    "name": [
        "first_name",
        "last_name",
        "full_name",
        "middle_name",
        "employee_name",
        "customer_name",
        "person_name",
        "contact_name",
        "given_name",
        "family_name",
        "surname",
        "maiden_name",
        "beneficiary_name",
        "spouse_name",
    ],
    "address": [
        "address",
        "street",
        "street_address",
        "address_line",
        "address_line_1",
        "address_line_2",
        "city",
        "zip",
        "zip_code",
        "postal_code",
        "postcode",
        "state",
        "province",
        "home_address",
        "mailing_address",
        "residence",
    ],
    "financial": [
        "credit_card",
        "credit_card_number",
        "card_number",
        "card_num",
        "bank_account",
        "bank_account_number",
        "account_number",
        "iban",
        "routing_number",
        "swift_code",
        "cvv",
        "debit_card",
    ],
}
"""Maps PII category names to lists of column-name substrings.

When a column name (lowercased, with spaces replaced by underscores)
contains any of these substrings, it is classified as PII of the
corresponding type and automatically routed to the masking generator.
"""


# ---------------------------------------------------------------------------
# ERP Column Name Patterns
# ---------------------------------------------------------------------------

ERP_COLUMN_PATTERNS: Dict[str, Dict[str, str]] = {
    # --- SAP ERP field naming conventions ---
    "sap": {
        "bukrs": "company_code",
        "belnr": "document_number",
        "gjahr": "fiscal_year",
        "monat": "fiscal_period",
        "kunnr": "customer_id",
        "lifnr": "vendor_id",
        "matnr": "material_number",
        "werks": "plant_code",
        "kostl": "cost_center",
        "aufnr": "order_number",
        "vbeln": "sales_order",
        "ebeln": "purchase_order",
        "pernr": "employee_id",
        "saknr": "gl_account",
        "hkont": "account_code",
        "waers": "currency_code",
        "menge": "quantity",
        "dmbtr": "amount_local",
        "wrbtr": "amount_document",
        "blart": "document_type",
        "bldat": "document_date",
        "budat": "posting_date",
        "lgort": "storage_location",
        "bsart": "purchasing_doc_type",
        "ekgrp": "purchasing_group",
        "ekorg": "purchasing_org",
        "vkorg": "sales_org",
        "vtweg": "distribution_channel",
        "spart": "division",
        "ktopl": "chart_of_accounts",
    },
    # --- Oracle E-Business Suite naming conventions ---
    "oracle": {
        "segment1": "account_code",
        "segment2": "cost_center",
        "segment3": "account_code",
        "invoice_id": "document_number",
        "invoice_num": "document_number",
        "po_header_id": "purchase_order",
        "po_number": "purchase_order",
        "vendor_id": "vendor_id",
        "vendor_site_id": "vendor_id",
        "customer_id": "customer_id",
        "customer_number": "customer_id",
        "organization_id": "company_code",
        "org_id": "company_code",
        "set_of_books_id": "company_code",
        "ledger_id": "company_code",
        "period_name": "fiscal_period",
        "gl_date": "posting_date",
        "item_id": "material_number",
        "inventory_item_id": "material_number",
        "person_id": "employee_id",
        "assignment_id": "employee_id",
        "order_number": "sales_order",
        "header_id": "document_number",
        "line_id": "document_number",
    },
    # --- Microsoft Dynamics 365 naming conventions ---
    "dynamics": {
        "accountnumber": "account_code",
        "ordernumber": "sales_order",
        "purchaseordernumber": "purchase_order",
        "invoicenumber": "document_number",
        "customernumber": "customer_id",
        "vendornumber": "vendor_id",
        "employeenumber": "employee_id",
        "journalnumber": "document_number",
        "vouchernumber": "document_number",
        "itemnumber": "material_number",
        "dimensionvalue": "cost_center",
        "financialdimension": "cost_center",
        "ledgeraccount": "account_code",
        "mainaccount": "account_code",
        "companycode": "company_code",
        "fiscalperiod": "fiscal_period",
        "fiscalyear": "fiscal_year",
        "transactiondate": "posting_date",
        "salesordernumber": "sales_order",
        "purchaserequisition": "purchase_order",
        "workeridentifier": "employee_id",
    },
    # --- Generic ERP column naming conventions ---
    "generic": {
        "document_number": "document_number",
        "doc_number": "document_number",
        "doc_num": "document_number",
        "document_id": "document_number",
        "account_code": "account_code",
        "account_id": "account_code",
        "gl_account": "account_code",
        "general_ledger": "account_code",
        "cost_center": "cost_center",
        "cost_center_code": "cost_center",
        "profit_center": "cost_center",
        "material_number": "material_number",
        "material_code": "material_number",
        "material_id": "material_number",
        "item_number": "material_number",
        "item_code": "material_number",
        "purchase_order": "purchase_order",
        "po_number": "purchase_order",
        "po_id": "purchase_order",
        "sales_order": "sales_order",
        "so_number": "sales_order",
        "order_id": "sales_order",
        "order_number": "sales_order",
        "employee_id": "employee_id",
        "emp_id": "employee_id",
        "emp_number": "employee_id",
        "staff_id": "employee_id",
        "worker_id": "employee_id",
        "personnel_number": "employee_id",
        "company_code": "company_code",
        "company_id": "company_code",
        "org_code": "company_code",
        "vendor_code": "vendor_id",
        "vendor_number": "vendor_id",
        "supplier_id": "vendor_id",
        "customer_code": "customer_id",
        "customer_number": "customer_id",
        "cust_id": "customer_id",
        "fiscal_year": "fiscal_year",
        "fiscal_period": "fiscal_period",
        "posting_date": "posting_date",
        "document_date": "posting_date",
        "currency": "currency_code",
        "currency_code": "currency_code",
        "plant": "plant_code",
        "plant_code": "plant_code",
        "warehouse": "storage_location",
        "storage_location": "storage_location",
    },
}
"""Maps ERP system identifiers to dictionaries of column-name → field-type.

Column names are matched case-insensitively.  The ``"generic"`` category
covers common ERP column names that are not system-specific.
"""


# ---------------------------------------------------------------------------
# Known ERP field types that trigger rules-based generation
# ---------------------------------------------------------------------------

_KNOWN_ERP_FORMAT_TYPES: frozenset[str] = frozenset(
    {
        "document_number",
        "account_code",
        "cost_center",
        "material_number",
        "purchase_order",
        "sales_order",
        "employee_id",
        "company_code",
        "vendor_id",
        "customer_id",
        "plant_code",
        "storage_location",
        "currency_code",
        "fiscal_year",
        "fiscal_period",
        "posting_date",
    }
)


# ---------------------------------------------------------------------------
# Pydantic Data Models
# ---------------------------------------------------------------------------


class ColumnAnalysis(BaseModel):
    """Comprehensive characterisation of a single column for method selection.

    Aggregates schema metadata, statistical profile summaries, PII flags,
    ERP classification, and cross-column dependency metrics.  Produced by
    :meth:`MethodSelector._analyze_column` and consumed by
    :meth:`MethodSelector._select_method_for_column`.

    Attributes:
        column_name: Column identifier as it appears in the table schema.
        data_type: Logical data type (integer, float, string, date, …).
        is_primary_key: ``True`` when the column participates in the PK.
        is_foreign_key: ``True`` when the column is a foreign key.
        is_nullable: ``True`` when the column accepts ``NULL`` values.
        has_format_pattern: Whether the column has a known format pattern
            (e.g. SAP document number ``XXXXXXXXXX``).
        format_pattern: Human-readable description of the format or a
            regex pattern string, if available.
        has_distribution_fit: ``True`` when the statistical profile
            includes a good parametric distribution fit for this column.
        distribution_type: Name of the fitted distribution (e.g.
            ``"normal"``, ``"lognormal"``, ``"poisson"``).
        distribution_goodness_of_fit: Kolmogorov–Smirnov test p-value for
            the fitted distribution; higher means better fit.
        has_lookup_values: ``True`` when the column has a small, finite
            set of valid values (categorical / enumerated).
        unique_value_count: Number of distinct values observed in the
            source profile.
        total_value_count: Total number of non-null values in the source
            profile.
        cardinality_ratio: ``unique_value_count / total_value_count``.
        is_pii_flagged: ``True`` when the column is identified as
            containing personally identifiable information.
        pii_type: Category of PII (``"ssn"``, ``"email"``, ``"phone"``,
            ``"name"``, ``"address"``, ``"financial"``), or ``None``.
        erp_module: The ERP module context (e.g.
            ``"financial_accounting"``, ``"hr"``).
        erp_field_type: ERP-specific field classification (e.g.
            ``"document_number"``, ``"account_code"``), or ``None``.
        has_cross_field_dependencies: ``True`` when the column has strong
            statistical dependencies with other columns.
        correlation_strength: Maximum absolute Pearson / Cramér's V
            correlation with any other column in the same table.
    """

    column_name: str
    data_type: str = Field(
        default="string",
        description="Logical data type of the column.",
    )
    is_primary_key: bool = False
    is_foreign_key: bool = False
    is_nullable: bool = False
    has_format_pattern: bool = False
    format_pattern: Optional[str] = None
    has_distribution_fit: bool = False
    distribution_type: Optional[str] = None
    distribution_goodness_of_fit: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="KS test p-value for the fitted distribution.",
    )
    has_lookup_values: bool = False
    unique_value_count: int = Field(default=0, ge=0)
    total_value_count: int = Field(default=0, ge=0)
    cardinality_ratio: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="unique_value_count / total_value_count (0.0 if no data).",
    )
    is_pii_flagged: bool = False
    pii_type: Optional[str] = None
    erp_module: Optional[str] = None
    erp_field_type: Optional[str] = None
    has_cross_field_dependencies: bool = False
    correlation_strength: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Maximum absolute correlation with other columns.",
    )


class MethodAssignment(BaseModel):
    """Maps a single column to its selected generation method.

    Produced by :meth:`MethodSelector._select_method_for_column` and
    consumed by the
    :class:`~generation_engine.orchestrator.batch_processor.BatchProcessor`
    to delegate per-column generation to the appropriate strategy.

    Attributes:
        column_name: The column to generate.
        method: Selected generation method enum value.
        confidence: Confidence score (0.0–1.0) reflecting how well the
            column characteristics match the chosen method.
        reason: Human-readable, auditable explanation for the selection.
        method_config: Optional method-specific configuration overrides
            forwarded to the concrete generator.
        fallback_method: Alternative method to try if the primary fails.
        is_override: ``True`` when the user explicitly specified this
            method, bypassing the automatic selection logic.
        is_pii: ``True`` when the column is flagged as containing PII.
    """

    column_name: str
    method: GenerationMethod
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence in method selection (0.0–1.0).",
    )
    reason: str = Field(
        default="",
        description="Human-readable explanation for the method selection.",
    )
    method_config: Optional[Dict[str, Any]] = None
    fallback_method: Optional[GenerationMethod] = None
    is_override: bool = False
    is_pii: bool = False


# ---------------------------------------------------------------------------
# Method Selection Rules — Priority-Ordered Decision Matrix
# ---------------------------------------------------------------------------

METHOD_SELECTION_RULES: List[Dict[str, Any]] = [
    {
        "priority": 1,
        "name": "pii_masking",
        "description": "PII-flagged columns routed to masking for zero-PII guarantee",
        "condition": "is_pii_flagged",
        "method": GenerationMethod.MASKING,
        "confidence": 1.0,
    },
    {
        "priority": 2,
        "name": "erp_formatted_fields",
        "description": "ERP-formatted fields with business rules constraints",
        "condition": "has_erp_format",
        "method": GenerationMethod.RULES,
        "confidence": 0.95,
    },
    {
        "priority": 3,
        "name": "low_cardinality_categorical",
        "description": "Low-cardinality categorical best served by lookup rules",
        "condition": "low_cardinality",
        "method": GenerationMethod.RULES,
        "confidence": 0.9,
    },
    {
        "priority": 4,
        "name": "well_fitted_distribution",
        "description": "Good distribution fit for statistical synthesis",
        "condition": "good_distribution_fit",
        "method": GenerationMethod.STATISTICAL,
        "confidence": 0.85,
    },
    {
        "priority": 5,
        "name": "high_correlation",
        "description": "Strong multivariate correlation preserved by AI/ML",
        "condition": "high_correlation",
        "method": GenerationMethod.AI_ML,
        "confidence": 0.8,
    },
    {
        "priority": 6,
        "name": "date_timestamp",
        "description": "Date/timestamp column with business calendar rules",
        "condition": "is_date_type",
        "method": GenerationMethod.RULES,
        "confidence": 0.85,
    },
    {
        "priority": 7,
        "name": "boolean_column",
        "description": "Boolean column, simple lookup generation",
        "condition": "is_boolean",
        "method": GenerationMethod.RULES,
        "confidence": 0.9,
    },
    {
        "priority": 8,
        "name": "numeric_with_range",
        "description": "Numeric column with known range for statistical synthesis",
        "condition": "numeric_with_range",
        "method": GenerationMethod.STATISTICAL,
        "confidence": 0.75,
    },
    {
        "priority": 9,
        "name": "high_cardinality_text",
        "description": "High-cardinality text column for AI/ML generation",
        "condition": "high_cardinality_text",
        "method": GenerationMethod.AI_ML,
        "confidence": 0.7,
    },
    {
        "priority": 10,
        "name": "default_fallback",
        "description": "Default fallback to statistical synthesis",
        "condition": "always",
        "method": GenerationMethod.STATISTICAL,
        "confidence": 0.5,
    },
]
"""Priority-ordered decision matrix for generation method selection.

Rules are evaluated in ascending priority order (1 = highest).  The
**first** matching rule determines the generation method.  Each rule
dictionary contains:

- ``priority`` — evaluation order (lower number = higher priority).
- ``name`` — machine-readable rule identifier.
- ``description`` — human-readable summary.
- ``condition`` — symbolic condition name evaluated by
  :meth:`MethodSelector._evaluate_rule_condition`.
- ``method`` — the :class:`GenerationMethod` to assign when matched.
- ``confidence`` — confidence score (0.0–1.0) for the assignment.
"""


# ---------------------------------------------------------------------------
# MethodSelector
# ---------------------------------------------------------------------------


class MethodSelector:
    """Intelligent per-column generation method selector.

    Analyses each column in a schema definition against its statistical
    profile and privacy annotations, then applies the priority-ordered
    :data:`METHOD_SELECTION_RULES` decision matrix to select the optimal
    generation method per column.

    The selection is **deterministic** — identical inputs always produce
    identical outputs — and every decision is logged with a human-readable
    rationale for audit and compliance reporting.

    Args:
        config: Optional configuration dictionary for custom rule
            overrides and tuning parameters.  Supported keys:

            - ``"correlation_threshold"`` (float): Override the default
              0.7 threshold for routing to AI/ML.  Default ``0.7``.
            - ``"distribution_p_value_threshold"`` (float): Override the
              default 0.05 KS p-value threshold.  Default ``0.05``.
            - ``"low_cardinality_max"`` (int): Override the maximum
              unique-value count for categorical rule routing.
              Default ``100``.
            - ``"high_cardinality_text_ratio"`` (float): Override the
              cardinality ratio threshold for high-cardinality text.
              Default ``0.5``.

    Example::

        selector = MethodSelector(config={"correlation_threshold": 0.8})
        assignments = selector.select_methods(schema, profile, gen_config)
    """

    # Configurable thresholds with sensible defaults
    _DEFAULT_CORRELATION_THRESHOLD: float = 0.7
    _DEFAULT_DISTRIBUTION_P_VALUE_THRESHOLD: float = 0.05
    _DEFAULT_LOW_CARDINALITY_MAX: int = 100
    _DEFAULT_HIGH_CARDINALITY_TEXT_RATIO: float = 0.5

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._logger = get_logger(__name__)
        self._config: Dict[str, Any] = config if config is not None else {}

        # Configurable thresholds — no hardcoded values for PII detection
        self._correlation_threshold: float = self._config.get(
            "correlation_threshold",
            self._DEFAULT_CORRELATION_THRESHOLD,
        )
        self._distribution_p_value_threshold: float = self._config.get(
            "distribution_p_value_threshold",
            self._DEFAULT_DISTRIBUTION_P_VALUE_THRESHOLD,
        )
        self._low_cardinality_max: int = self._config.get(
            "low_cardinality_max",
            self._DEFAULT_LOW_CARDINALITY_MAX,
        )
        self._high_cardinality_text_ratio: float = self._config.get(
            "high_cardinality_text_ratio",
            self._DEFAULT_HIGH_CARDINALITY_TEXT_RATIO,
        )

        # Load generator capabilities from the GENERATOR_REGISTRY
        self._generator_capabilities: Dict[str, Dict[str, Any]] = {}
        self._load_generator_capabilities()

        # Cache of method assignments keyed by table name
        self._assignment_cache: Dict[str, List[MethodAssignment]] = {}

        self._logger.info(
            "method_selector_initialised",
            available_methods=get_available_methods(),
            correlation_threshold=self._correlation_threshold,
            distribution_p_value_threshold=self._distribution_p_value_threshold,
            low_cardinality_max=self._low_cardinality_max,
        )

    # ------------------------------------------------------------------
    # Generator Capability Loading
    # ------------------------------------------------------------------

    def _load_generator_capabilities(self) -> None:
        """Load capabilities from every registered generator.

        Iterates over :data:`GENERATOR_REGISTRY`, instantiates each
        generator via :func:`get_generator`, and calls
        :meth:`~BaseGenerator.get_capabilities` to populate the internal
        capability map used during rule evaluation.
        """
        registry: Dict[str, type] = GENERATOR_REGISTRY  # type: ignore[assignment]
        for method_name in registry:
            try:
                instance: BaseGenerator = get_generator(method_name)
                capabilities: Dict[str, Any] = instance.get_capabilities()
                self._generator_capabilities[method_name] = capabilities
                self._logger.debug(
                    "generator_capability_loaded",
                    method=method_name,
                    supported_types=capabilities.get("supported_column_types", []),
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "generator_capability_load_failed",
                    method=method_name,
                    error=str(exc),
                )
                self._generator_capabilities[method_name] = {
                    "name": method_name,
                    "supported_column_types": [],
                    "best_for": [],
                    "supports_gpu": False,
                    "error": str(exc),
                }

    # ------------------------------------------------------------------
    # Public API — Main Entry Point
    # ------------------------------------------------------------------

    def select_methods(
        self,
        schema_definition: Dict[str, Any],
        statistical_profile: Dict[str, Any],
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, List[MethodAssignment]]:
        """Analyse all tables and columns and return per-column method assignments.

        This is the primary entry point consumed by the orchestrator.  For
        each table listed in *schema_definition*, every column is
        individually analysed and assigned a generation method via the
        priority-ordered decision matrix.

        Args:
            schema_definition: Schema dictionary containing table
                definitions.  Expected structure::

                    {
                        "tables": {
                            "<table_name>": {
                                "columns": [
                                    {"name": "col1", "data_type": "integer", ...},
                                    ...
                                ],
                                "erp_module": "financial_accounting",
                                ...
                            }
                        }
                    }

                Alternatively, the top-level keys may directly be table
                names (flat schema format).

            statistical_profile: Statistical profile dictionary captured
                by the Profiling Service.  Expected structure::

                    {
                        "tables": {
                            "<table_name>": {
                                "columns": {
                                    "<col_name>": {
                                        "distribution": {...},
                                        "unique_count": 42,
                                        "total_count": 10000,
                                        "correlations": {...},
                                        ...
                                    }
                                }
                            }
                        }
                    }

            generation_config: Optional user-provided configuration that
                may contain explicit per-column or per-table method
                overrides.  Expected structure::

                    {
                        "method_overrides": {
                            "<table_name>": {
                                "<col_name>": "ai_ml"
                            }
                        },
                        "default_method": "statistical"
                    }

        Returns:
            Dictionary mapping table names to ordered lists of
            :class:`MethodAssignment` objects — one per column.
        """
        if generation_config is None:
            generation_config = {}

        result: Dict[str, List[MethodAssignment]] = {}

        # Normalise schema: support both {"tables": {…}} and flat {table: {…}}
        tables_dict: Dict[str, Any] = schema_definition.get(
            "tables", schema_definition
        )

        # Normalise profile: support both {"tables": {…}} and flat {table: {…}}
        profile_tables: Dict[str, Any] = statistical_profile.get(
            "tables", statistical_profile
        )

        for table_name, table_def in tables_dict.items():
            # Skip non-dict entries (metadata keys like "version", "name")
            if not isinstance(table_def, dict):
                continue

            columns: List[Dict[str, Any]] = table_def.get("columns", [])
            if not columns:
                self._logger.warning(
                    "table_has_no_columns",
                    table=table_name,
                )
                continue

            erp_module: Optional[str] = table_def.get("erp_module")
            table_profile: Dict[str, Any] = profile_tables.get(table_name, {})

            table_assignments: List[MethodAssignment] = []

            for column_def in columns:
                # Analyse the column
                analysis: ColumnAnalysis = self._analyze_column(
                    column_def=column_def,
                    table_profile=table_profile,
                    erp_module=erp_module,
                )

                # Select the optimal generation method
                assignment: MethodAssignment = self._select_method_for_column(
                    analysis
                )

                table_assignments.append(assignment)

                self._logger.debug(
                    "column_method_assigned",
                    table=table_name,
                    column=assignment.column_name,
                    method=assignment.method.value,
                    confidence=assignment.confidence,
                    reason=assignment.reason,
                    is_pii=assignment.is_pii,
                )

            # Apply user overrides if any
            table_assignments = self._apply_user_overrides(
                assignments=table_assignments,
                generation_config=generation_config,
                table_name=table_name,
            )

            result[table_name] = table_assignments

        # Cache for subsequent lookups
        self._assignment_cache = result

        # Log summary
        summary = self.get_selection_summary(result)
        self._logger.info(
            "method_selection_complete",
            tables_analyzed=summary["tables_analyzed"],
            total_columns=summary["total_columns"],
            method_distribution=summary["method_distribution"],
            pii_columns=summary["pii_columns"],
            override_columns=summary["override_columns"],
            average_confidence=round(summary["average_confidence"], 4),
        )

        return result

    # ------------------------------------------------------------------
    # Column Analysis
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_column_spec(column_def: Dict[str, Any]) -> Optional[ColumnSpec]:
        """Attempt to parse a raw column dictionary into a :class:`ColumnSpec`.

        Uses the Pydantic model defined in the base generator module for
        validated column schema parsing.  Returns ``None`` when the column
        definition cannot be coerced into a valid :class:`ColumnSpec`
        (e.g. unknown data type).

        Args:
            column_def: Raw column descriptor dictionary.

        Returns:
            A validated :class:`ColumnSpec` or ``None`` on parse failure.
        """
        try:
            return ColumnSpec(**column_def)
        except Exception:  # noqa: BLE001
            return None

    def _analyze_column(
        self,
        column_def: Dict[str, Any],
        table_profile: Dict[str, Any],
        erp_module: Optional[str] = None,
    ) -> ColumnAnalysis:
        """Build a comprehensive :class:`ColumnAnalysis` for a single column.

        Merges schema metadata (data type, nullability, PK/FK status) with
        statistical profile information (distributions, cardinality,
        correlations) and applies PII and ERP field-type detection heuristics.

        When the column definition matches the :class:`ColumnSpec` schema,
        a validated spec is produced to extract structured metadata;
        otherwise the method falls back to raw dictionary access.

        Args:
            column_def: Column descriptor dictionary from the schema
                definition.  Expected keys: ``"name"``, ``"data_type"``,
                ``"primary_key"``, ``"foreign_key"``, ``"nullable"``,
                ``"constraints"``.
            table_profile: Statistical profile dictionary for the parent
                table.  Expected to contain a ``"columns"`` key mapping
                column names to per-column profile data.
            erp_module: Optional ERP module context string.

        Returns:
            A fully populated :class:`ColumnAnalysis` instance.
        """
        # Attempt validated ColumnSpec parsing for structured metadata
        col_spec: Optional[ColumnSpec] = self._parse_column_spec(column_def)

        # Use validated ColumnSpec when available for structured access
        if col_spec is not None:
            col_name = col_spec.name
            data_type = col_spec.data_type.lower()
            is_primary_key = col_spec.primary_key
            is_nullable = col_spec.nullable
            is_foreign_key = col_spec.foreign_key is not None
        else:
            col_name = column_def.get("name", "")
            data_type = column_def.get("data_type", "string").lower()
            is_primary_key = column_def.get("primary_key", False)
            is_nullable = column_def.get("nullable", False)
            fk_raw = column_def.get("foreign_key")
            is_foreign_key = bool(fk_raw)

        # Extract column-level profile data
        col_profiles: Dict[str, Any] = table_profile.get("columns", {})
        col_profile: Dict[str, Any] = col_profiles.get(col_name, {})

        # --- Distribution fit ---
        dist_info: Dict[str, Any] = col_profile.get("distribution", {})
        distribution_type: Optional[str] = dist_info.get("type")
        goodness_of_fit: float = float(dist_info.get("p_value", 0.0))
        has_distribution_fit: bool = (
            distribution_type is not None and goodness_of_fit > 0.0
        )

        # --- Cardinality ---
        unique_count: int = int(col_profile.get("unique_count", 0))
        total_count: int = int(col_profile.get("total_count", 0))
        cardinality_ratio: float = 0.0
        if total_count > 0:
            cardinality_ratio = min(unique_count / total_count, 1.0)

        has_lookup_values: bool = (
            unique_count > 0 and unique_count <= self._low_cardinality_max
        )

        # --- Format patterns ---
        format_pattern: Optional[str] = col_profile.get("format_pattern")
        constraints: Optional[Dict[str, Any]] = column_def.get("constraints")
        if format_pattern is None and constraints is not None:
            format_pattern = constraints.get("pattern") or constraints.get("regex")
        has_format_pattern: bool = format_pattern is not None

        # --- PII detection ---
        is_pii_flagged: bool = col_profile.get("is_pii", False)
        pii_type: Optional[str] = col_profile.get("pii_type")

        # Supplement with column-name heuristic detection
        if not is_pii_flagged:
            name_pii_flag, name_pii_type = self._detect_pii_from_column_name(col_name)
            if name_pii_flag:
                is_pii_flagged = True
                pii_type = name_pii_type

        # --- ERP field type detection ---
        erp_field_type: Optional[str] = col_profile.get("erp_field_type")
        if erp_field_type is None:
            erp_field_type = self._detect_erp_field_type(col_name, erp_module)

        # If ERP field type detected and format pattern not yet set, mark it
        if erp_field_type and not has_format_pattern:
            has_format_pattern = True
            format_pattern = format_pattern or f"erp:{erp_field_type}"

        # --- Cross-column correlations ---
        correlations: Dict[str, float] = col_profile.get("correlations", {})
        correlation_strength: float = 0.0
        if correlations:
            abs_correlations = [abs(v) for v in correlations.values() if isinstance(v, (int, float))]
            if abs_correlations:
                correlation_strength = min(max(abs_correlations), 1.0)
        has_cross_field_dependencies: bool = (
            correlation_strength > self._correlation_threshold
        )

        return ColumnAnalysis(
            column_name=col_name,
            data_type=data_type,
            is_primary_key=is_primary_key,
            is_foreign_key=is_foreign_key,
            is_nullable=is_nullable,
            has_format_pattern=has_format_pattern,
            format_pattern=format_pattern,
            has_distribution_fit=has_distribution_fit,
            distribution_type=distribution_type,
            distribution_goodness_of_fit=goodness_of_fit,
            has_lookup_values=has_lookup_values,
            unique_value_count=unique_count,
            total_value_count=total_count,
            cardinality_ratio=cardinality_ratio,
            is_pii_flagged=is_pii_flagged,
            pii_type=pii_type,
            erp_module=erp_module,
            erp_field_type=erp_field_type,
            has_cross_field_dependencies=has_cross_field_dependencies,
            correlation_strength=correlation_strength,
        )

    # ------------------------------------------------------------------
    # Method Selection per Column
    # ------------------------------------------------------------------

    def _select_method_for_column(
        self,
        analysis: ColumnAnalysis,
    ) -> MethodAssignment:
        """Apply the decision matrix to select a generation method for one column.

        Evaluates :data:`METHOD_SELECTION_RULES` in priority order.  The
        **first** rule whose condition matches the :class:`ColumnAnalysis`
        determines the assigned method.  A fallback method
        (:attr:`GenerationMethod.STATISTICAL`) is always assigned as a
        safety net.

        Args:
            analysis: The :class:`ColumnAnalysis` for the target column.

        Returns:
            A :class:`MethodAssignment` with method, confidence, reason,
            fallback, and PII status populated.
        """
        for rule in METHOD_SELECTION_RULES:
            matched, reason = self._evaluate_rule_condition(rule, analysis)
            if matched:
                method: GenerationMethod = rule["method"]
                confidence: float = rule["confidence"]

                # Determine fallback — use STATISTICAL unless primary is already STATISTICAL
                fallback: Optional[GenerationMethod] = None
                if method != GenerationMethod.STATISTICAL:
                    fallback = GenerationMethod.STATISTICAL
                elif method == GenerationMethod.STATISTICAL:
                    fallback = GenerationMethod.RULES

                return MethodAssignment(
                    column_name=analysis.column_name,
                    method=method,
                    confidence=confidence,
                    reason=reason,
                    fallback_method=fallback,
                    is_pii=analysis.is_pii_flagged,
                    method_config=self._build_method_config(rule, analysis),
                )

        # Absolute fallback — should never reach here due to "always" rule
        return MethodAssignment(
            column_name=analysis.column_name,
            method=GenerationMethod.STATISTICAL,
            confidence=0.5,
            reason="No specific rule matched, defaulting to statistical synthesis",
            fallback_method=GenerationMethod.RULES,
            is_pii=analysis.is_pii_flagged,
        )

    def _evaluate_rule_condition(
        self,
        rule: Dict[str, Any],
        analysis: ColumnAnalysis,
    ) -> Tuple[bool, str]:
        """Evaluate a single rule's condition against a column analysis.

        Args:
            rule: A rule dictionary from :data:`METHOD_SELECTION_RULES`.
            analysis: The column analysis to test against.

        Returns:
            A ``(matched, reason)`` tuple where *matched* is ``True`` if
            the rule fires and *reason* is the human-readable rationale.
        """
        condition: str = rule["condition"]

        if condition == "is_pii_flagged":
            if analysis.is_pii_flagged:
                pii_label = analysis.pii_type or "unknown"
                return (
                    True,
                    f"Column flagged as PII ({pii_label}), routed to masking "
                    f"for zero-PII guarantee",
                )
            return (False, "")

        if condition == "has_erp_format":
            if (
                analysis.has_format_pattern
                and analysis.erp_field_type is not None
                and analysis.erp_field_type in _KNOWN_ERP_FORMAT_TYPES
            ):
                pattern_label = analysis.format_pattern or analysis.erp_field_type
                return (
                    True,
                    f"ERP-formatted field ({pattern_label}) with business "
                    f"rules constraints",
                )
            return (False, "")

        if condition == "low_cardinality":
            if (
                analysis.has_lookup_values
                and analysis.unique_value_count <= self._low_cardinality_max
            ):
                return (
                    True,
                    f"Low-cardinality categorical ({analysis.unique_value_count} "
                    f"unique values), best served by lookup rules",
                )
            return (False, "")

        if condition == "good_distribution_fit":
            if (
                analysis.has_distribution_fit
                and analysis.distribution_goodness_of_fit
                > self._distribution_p_value_threshold
            ):
                dist_label = analysis.distribution_type or "unknown"
                return (
                    True,
                    f"Good distribution fit ({dist_label}, "
                    f"p={analysis.distribution_goodness_of_fit:.4f})",
                )
            return (False, "")

        if condition == "high_correlation":
            if analysis.correlation_strength > self._correlation_threshold:
                return (
                    True,
                    f"Strong multivariate correlation "
                    f"({analysis.correlation_strength:.2f}), AI/ML preserves "
                    f"complex dependencies",
                )
            return (False, "")

        if condition == "is_date_type":
            if analysis.data_type in ("date", "datetime", "timestamp"):
                return (True, "Date column with business calendar rules")
            return (False, "")

        if condition == "is_boolean":
            if analysis.data_type == "boolean":
                return (True, "Boolean column, simple lookup generation")
            return (False, "")

        if condition == "numeric_with_range":
            if analysis.data_type in ("integer", "float", "decimal"):
                return (
                    True,
                    "Numeric column with known range, statistical distribution "
                    "synthesis",
                )
            return (False, "")

        if condition == "high_cardinality_text":
            if (
                analysis.data_type in ("string", "text")
                and analysis.cardinality_ratio > self._high_cardinality_text_ratio
            ):
                return (
                    True,
                    "High-cardinality text column, AI/ML for realistic value "
                    "generation",
                )
            return (False, "")

        if condition == "always":
            return (
                True,
                "No specific rule matched, defaulting to statistical synthesis",
            )

        # Unknown condition — should not happen with well-defined rules
        self._logger.warning(
            "unknown_rule_condition",
            condition=condition,
            rule_name=rule.get("name", "unknown"),
        )
        return (False, "")

    def _build_method_config(
        self,
        rule: Dict[str, Any],
        analysis: ColumnAnalysis,
    ) -> Optional[Dict[str, Any]]:
        """Build method-specific configuration hints based on the matching rule.

        Provides concrete generators with contextual information about
        *why* a column was routed to them.

        Args:
            rule: The matched rule from the decision matrix.
            analysis: The column analysis that triggered the rule.

        Returns:
            A configuration dictionary, or ``None`` if no special config
            is needed.
        """
        condition: str = rule["condition"]

        if condition == "is_pii_flagged":
            return {
                "pii_type": analysis.pii_type,
                "original_data_type": analysis.data_type,
            }

        if condition == "has_erp_format":
            return {
                "erp_field_type": analysis.erp_field_type,
                "format_pattern": analysis.format_pattern,
                "erp_module": analysis.erp_module,
            }

        if condition == "low_cardinality":
            return {
                "generation_strategy": "lookup",
                "unique_value_count": analysis.unique_value_count,
            }

        if condition == "good_distribution_fit":
            return {
                "distribution_type": analysis.distribution_type,
                "goodness_of_fit": analysis.distribution_goodness_of_fit,
            }

        if condition == "high_correlation":
            return {
                "correlation_strength": analysis.correlation_strength,
                "preserve_correlations": True,
            }

        if condition == "is_date_type":
            return {
                "data_type": analysis.data_type,
                "generation_strategy": "business_calendar",
            }

        if condition == "is_boolean":
            return {
                "generation_strategy": "lookup",
                "data_type": "boolean",
            }

        return None

    # ------------------------------------------------------------------
    # User Override Application
    # ------------------------------------------------------------------

    def _apply_user_overrides(
        self,
        assignments: List[MethodAssignment],
        generation_config: Dict[str, Any],
        table_name: str = "",
    ) -> List[MethodAssignment]:
        """Apply user-specified method overrides to a list of assignments.

        Users may override the automatic selection for specific columns or
        entire tables via the ``"method_overrides"`` key in
        *generation_config*.  Overrides take precedence over the decision
        matrix.

        Args:
            assignments: The automatically generated method assignments.
            generation_config: User-provided configuration containing
                potential overrides.  Expected structure::

                    {
                        "method_overrides": {
                            "<table_name>": {
                                "<col_name>": "ai_ml"
                            }
                        },
                        "default_method": "statistical"
                    }
            table_name: The current table name for lookup in overrides.

        Returns:
            Updated list of :class:`MethodAssignment` objects with
            overrides applied and ``is_override`` set to ``True`` on
            affected assignments.
        """
        method_overrides: Dict[str, Any] = generation_config.get(
            "method_overrides", {}
        )
        default_method_str: Optional[str] = generation_config.get("default_method")

        # Table-level overrides
        table_overrides: Dict[str, str] = {}
        if table_name in method_overrides:
            table_override_value = method_overrides[table_name]
            if isinstance(table_override_value, dict):
                table_overrides = table_override_value
            elif isinstance(table_override_value, str):
                # Entire table override — apply to all columns
                for assignment in assignments:
                    try:
                        override_method = GenerationMethod(table_override_value)
                    except ValueError:
                        self._logger.warning(
                            "invalid_override_method",
                            table=table_name,
                            method=table_override_value,
                        )
                        continue
                    assignment.method = override_method
                    assignment.is_override = True
                    assignment.reason = (
                        f"User override: entire table '{table_name}' set to "
                        f"{override_method.value}"
                    )
                    self._logger.info(
                        "user_override_applied",
                        table=table_name,
                        column=assignment.column_name,
                        method=override_method.value,
                        override_type="table",
                    )
                return assignments

        # Column-level overrides
        updated: List[MethodAssignment] = []
        for assignment in assignments:
            col_name = assignment.column_name

            # Check column-level override for this table
            override_method_str = table_overrides.get(col_name)

            # Check global column overrides (not under a table key)
            if override_method_str is None:
                global_col_overrides = method_overrides.get("*", {})
                if isinstance(global_col_overrides, dict):
                    override_method_str = global_col_overrides.get(col_name)

            # Apply default_method if specified and no specific override
            if override_method_str is None and default_method_str is not None:
                # Default method only applies if no automatic rule was confident
                if assignment.confidence < 0.7:
                    override_method_str = default_method_str

            if override_method_str is not None:
                try:
                    override_method = GenerationMethod(override_method_str)
                except ValueError:
                    self._logger.warning(
                        "invalid_override_method",
                        table=table_name,
                        column=col_name,
                        method=override_method_str,
                    )
                    updated.append(assignment)
                    continue

                original_method = assignment.method
                assignment.method = override_method
                assignment.is_override = True
                assignment.reason = (
                    f"User override: column '{col_name}' changed from "
                    f"{original_method.value} to {override_method.value}"
                )
                self._logger.info(
                    "user_override_applied",
                    table=table_name,
                    column=col_name,
                    original_method=original_method.value,
                    override_method=override_method.value,
                    override_type="column",
                )

            updated.append(assignment)

        return updated

    # ------------------------------------------------------------------
    # PII Detection from Column Names
    # ------------------------------------------------------------------

    def _detect_pii_from_column_name(
        self,
        column_name: str,
    ) -> Tuple[bool, Optional[str]]:
        """Detect potential PII from a column name using pattern matching.

        Normalises the column name (lowercased, whitespace/hyphens to
        underscores) and checks it against :data:`PII_COLUMN_PATTERNS`.
        Returns the first matching PII category.

        Args:
            column_name: The column name to analyse.

        Returns:
            A ``(is_pii, pii_type)`` tuple.  ``is_pii`` is ``True`` when
            a PII pattern matches; ``pii_type`` is the category string
            (e.g. ``"ssn"``, ``"email"``), or ``None`` if no match.
        """
        normalised: str = re.sub(r"[\s\-]+", "_", column_name.strip().lower())

        for pii_type, patterns in PII_COLUMN_PATTERNS.items():
            for pattern in patterns:
                # Exact match or substring match within underscored boundaries
                if normalised == pattern or f"_{pattern}" in f"_{normalised}_":
                    self._logger.debug(
                        "pii_detected_from_column_name",
                        column=column_name,
                        pii_type=pii_type,
                        matched_pattern=pattern,
                    )
                    return (True, pii_type)

        return (False, None)

    # ------------------------------------------------------------------
    # ERP Field Type Detection
    # ------------------------------------------------------------------

    def _detect_erp_field_type(
        self,
        column_name: str,
        erp_module: Optional[str] = None,
    ) -> Optional[str]:
        """Detect ERP-specific field types from column naming conventions.

        Normalises the column name and checks it against
        :data:`ERP_COLUMN_PATTERNS` for all ERP systems (SAP, Oracle,
        Dynamics) plus generic patterns.

        Args:
            column_name: The column name to analyse.
            erp_module: Optional ERP module context for more targeted
                pattern matching.

        Returns:
            An ERP field-type classification string (e.g.
            ``"document_number"``, ``"account_code"``), or ``None`` if no
            pattern matches.
        """
        normalised: str = re.sub(r"[\s\-]+", "_", column_name.strip().lower())

        # Determine search order — prioritise system-specific patterns if
        # the ERP module hints at a system, then fall back to generic.
        system_priority: List[str]
        if erp_module and "sap" in erp_module.lower():
            system_priority = ["sap", "generic", "oracle", "dynamics"]
        elif erp_module and "oracle" in erp_module.lower():
            system_priority = ["oracle", "generic", "sap", "dynamics"]
        elif erp_module and "dynamics" in erp_module.lower():
            system_priority = ["dynamics", "generic", "sap", "oracle"]
        else:
            system_priority = ["generic", "sap", "oracle", "dynamics"]

        for system in system_priority:
            patterns: Dict[str, str] = ERP_COLUMN_PATTERNS.get(system, {})
            field_type = patterns.get(normalised)
            if field_type is not None:
                self._logger.debug(
                    "erp_field_type_detected",
                    column=column_name,
                    system=system,
                    field_type=field_type,
                )
                return field_type

        return None

    # ------------------------------------------------------------------
    # Selection Summary
    # ------------------------------------------------------------------

    def get_selection_summary(
        self,
        assignments: Dict[str, List[MethodAssignment]],
    ) -> Dict[str, Any]:
        """Return summary statistics of a method assignment result set.

        Useful for logging, dashboards, and quality reports that need
        aggregate metrics without iterating over individual assignments.

        Args:
            assignments: The method assignment dictionary returned by
                :meth:`select_methods`.

        Returns:
            Dictionary containing:

            - ``total_columns`` — total number of columns assigned.
            - ``method_distribution`` — count of columns per method.
            - ``pii_columns`` — count of PII-flagged columns.
            - ``override_columns`` — count of user-overridden columns.
            - ``average_confidence`` — mean confidence across all columns.
            - ``tables_analyzed`` — number of tables processed.
        """
        total_columns: int = 0
        method_counts: Dict[str, int] = {m.value: 0 for m in GenerationMethod}
        pii_columns: int = 0
        override_columns: int = 0
        confidence_sum: float = 0.0

        for _table_name, table_assignments in assignments.items():
            for assignment in table_assignments:
                total_columns += 1
                method_counts[assignment.method.value] = (
                    method_counts.get(assignment.method.value, 0) + 1
                )
                confidence_sum += assignment.confidence
                if assignment.is_pii:
                    pii_columns += 1
                if assignment.is_override:
                    override_columns += 1

        average_confidence: float = (
            confidence_sum / total_columns if total_columns > 0 else 0.0
        )

        return {
            "total_columns": total_columns,
            "method_distribution": method_counts,
            "pii_columns": pii_columns,
            "override_columns": override_columns,
            "average_confidence": average_confidence,
            "tables_analyzed": len(assignments),
        }

    # ------------------------------------------------------------------
    # Method Rationale Lookup
    # ------------------------------------------------------------------

    def get_method_rationale(
        self,
        table_name: str,
        column_name: str,
    ) -> Optional[str]:
        """Return the selection rationale for a specific column.

        Looks up the cached assignment from the most recent
        :meth:`select_methods` call.  Useful for quality reports, audit
        logs, and compliance documentation.

        Args:
            table_name: The table containing the target column.
            column_name: The column whose rationale is requested.

        Returns:
            The human-readable reason string, or ``None`` if the column
            was not found in the cache.
        """
        table_assignments: Optional[List[MethodAssignment]] = (
            self._assignment_cache.get(table_name)
        )
        if table_assignments is None:
            self._logger.debug(
                "rationale_lookup_table_not_found",
                table=table_name,
            )
            return None

        for assignment in table_assignments:
            if assignment.column_name == column_name:
                return assignment.reason

        self._logger.debug(
            "rationale_lookup_column_not_found",
            table=table_name,
            column=column_name,
        )
        return None
