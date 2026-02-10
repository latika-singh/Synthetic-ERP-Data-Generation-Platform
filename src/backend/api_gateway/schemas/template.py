"""Pydantic v2 request/response validation models for generation template CRUD operations.

This module defines the data validation schemas used by the Template Library (Screen S-003)
and Generation Wizard for managing reusable generation configurations. Templates encapsulate
ERP module targeting, generation method selection, table configurations, and parameter
presets that can be browsed, filtered, and applied to new generation jobs.

Models:
    TemplateCategory: Enum for the four initial ERP modules plus custom category.
    TemplateTableConfig: Per-table generation configuration within a template.
    TemplateParameters: Generation parameter presets (batch size, quality, format).
    TemplateRequest: Request model for creating/updating generation templates.
    TemplateResponse: Response model with template metadata, versioning, and usage stats.
    TemplateListResponse: Paginated list response for template catalog browsing.

Usage Example:
    >>> from src.backend.api_gateway.schemas.template import (
    ...     TemplateRequest, TemplateResponse, TemplateCategory
    ... )
    >>> request = TemplateRequest(
    ...     name="SAP Financial GL Entries",
    ...     category=TemplateCategory.FINANCIAL,
    ...     erp_type="sap",
    ...     generation_method="statistical",
    ...     tables=[{"table_name": "BKPF", "record_count": 50000}],
    ... )
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TemplateCategory(StrEnum):
    """Enumeration of template categories aligned with initial ERP modules (C-005).

    Maps to the four ERP modules supported in the initial release plus a custom
    category for user-defined template classifications.

    Attributes:
        FINANCIAL: Financial Accounting module (GL entries, invoices, payments).
        HR: Human Resources module (employee records, payroll, benefits).
        SALES: Sales & Distribution module (orders, customers, pricing).
        MATERIALS: Material Management module (inventory, purchase orders, vendors).
        CUSTOM: User-defined custom category for non-standard templates.
    """

    FINANCIAL = "financial_accounting"
    HR = "hr"
    SALES = "sales_distribution"
    MATERIALS = "material_management"
    CUSTOM = "custom"


class TemplateTableConfig(BaseModel):
    """Per-table generation configuration within a template.

    Defines the target table name, default record count, optional column-level
    overrides for generation rules, and whether foreign key relationships
    should be preserved during data generation.

    Attributes:
        table_name: Fully qualified or short name of the target ERP table.
        record_count: Default number of records to generate for this table.
            Must be between 1 and 10,000,000 inclusive.
        column_overrides: Optional dictionary mapping column names to
            column-specific generation rules (e.g., value ranges, patterns,
            distribution types, or fixed values).
        preserve_relationships: Whether to maintain foreign key relationships
            with other tables during generation. Defaults to True for
            referential integrity compliance.
    """

    table_name: str = Field(
        ...,
        min_length=1,
        description="Target table name",
    )
    record_count: int = Field(
        ...,
        gt=0,
        le=10_000_000,
        description="Default record count for this table (1 to 10,000,000)",
    )
    column_overrides: dict[str, Any] | None = Field(
        default=None,
        description="Column-specific generation rules mapping column names to override configs",
    )
    preserve_relationships: bool = Field(
        default=True,
        description="Maintain FK relationships with other tables during generation",
    )


class TemplateParameters(BaseModel):
    """Generation parameter presets for a template.

    Encapsulates the default batch processing size, minimum quality threshold,
    output format, and optional custom business rules that are applied when
    a generation job is launched from this template.

    Attributes:
        batch_size: Number of records processed per generation batch.
            Valid range: 1,000 to 100,000. Default is 10,000.
        quality_threshold: Minimum acceptable composite quality score.
            Uses the weighted formula: 40% statistical + 30% business rules
            + 30% referential integrity. Range 0.0 to 1.0, default 0.95.
        output_format: Default output format for generated data.
            Must be one of: 'sql', 'csv', 'json', 'parquet'.
        custom_rules: Optional dictionary of custom business rules that
            override or supplement the standard generation logic.
    """

    batch_size: int = Field(
        default=10000,
        ge=1000,
        le=100000,
        description="Records per generation batch (1,000 to 100,000)",
    )
    quality_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Minimum quality score (0.0 to 1.0, default 0.95 per R-009)",
    )
    output_format: str = Field(
        default="csv",
        description="Default output format: sql, csv, json, parquet",
    )
    custom_rules: dict[str, Any] | None = Field(
        default=None,
        description="Custom business rules for generation logic overrides",
    )

    @field_validator("output_format")
    @classmethod
    def validate_output_format(cls, value: str) -> str:
        """Validate that output_format is one of the supported export formats.

        Args:
            value: The output format string to validate.

        Returns:
            The validated output format string in lowercase.

        Raises:
            ValueError: If the format is not one of sql, csv, json, parquet.
        """
        allowed_formats = {"sql", "csv", "json", "parquet"}
        normalized = value.strip().lower()
        if normalized not in allowed_formats:
            raise ValueError(
                f"Invalid output_format '{value}'. "
                f"Must be one of: {', '.join(sorted(allowed_formats))}"
            )
        return normalized


class TemplateRequest(BaseModel):
    """Request model for creating or updating a generation template.

    Captures all configuration needed to define a reusable generation template
    including the target ERP module, generation method, table configurations,
    parameter presets, and visibility settings. Used by POST/PUT endpoints
    on /api/v1/templates.

    Attributes:
        name: Human-readable template name (1-255 characters).
        description: Optional detailed description (up to 2000 characters).
        category: Template category corresponding to an ERP module or custom.
        erp_type: Target ERP system type for schema compatibility.
        generation_method: Data generation strategy to employ.
        schema_id: Optional reference to a discovered schema definition.
        tables: One or more table generation configurations.
        parameters: Generation parameter presets (batch size, quality, format).
        tags: Optional list of searchable tags for template catalog filtering.
        is_public: Whether the template is visible to all tenants.
        tenant_id: Owning tenant namespace for multi-tenant isolation.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Template name (1-255 characters)",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Template description (up to 2000 characters)",
    )
    category: TemplateCategory = Field(
        ...,
        description="Template category aligned with ERP module (C-005)",
    )
    erp_type: str = Field(
        ...,
        description="Target ERP type: sap, oracle_ebs, dynamics, legacy",
    )
    generation_method: str = Field(
        ...,
        description="Generation method: ai_ml, rules_based, statistical, masking",
    )
    schema_id: str | None = Field(
        default=None,
        description="Associated schema definition ID from discovery",
    )
    tables: list[TemplateTableConfig] = Field(
        ...,
        min_length=1,
        description="Table generation configurations (at least one required)",
    )
    parameters: TemplateParameters = Field(
        default_factory=TemplateParameters,
        description="Generation parameters with defaults",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Searchable tags for template catalog filtering",
    )
    is_public: bool = Field(
        default=False,
        description="Whether template is visible to all tenants",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Owning tenant namespace for multi-tenant isolation",
    )

    model_config = ConfigDict(strict=True)

    @field_validator("erp_type")
    @classmethod
    def validate_erp_type(cls, value: str) -> str:
        """Validate that erp_type is one of the supported ERP system types.

        Enforces constraint C-005 limiting initial release to SAP, Oracle EBS,
        Microsoft Dynamics, and legacy systems.

        Args:
            value: The ERP type string to validate.

        Returns:
            The validated ERP type string in lowercase.

        Raises:
            ValueError: If the ERP type is not supported.
        """
        allowed_erp_types = {"sap", "oracle_ebs", "dynamics", "legacy"}
        normalized = value.strip().lower()
        if normalized not in allowed_erp_types:
            raise ValueError(
                f"Invalid erp_type '{value}'. "
                f"Must be one of: {', '.join(sorted(allowed_erp_types))}"
            )
        return normalized

    @field_validator("generation_method")
    @classmethod
    def validate_generation_method(cls, value: str) -> str:
        """Validate that generation_method is one of the four supported methods.

        The platform supports AI/ML (GAN/VAE), rules-based, statistical synthesis,
        and intelligent masking generation strategies.

        Args:
            value: The generation method string to validate.

        Returns:
            The validated generation method string in lowercase.

        Raises:
            ValueError: If the generation method is not supported.
        """
        allowed_methods = {"ai_ml", "rules_based", "statistical", "masking"}
        normalized = value.strip().lower()
        if normalized not in allowed_methods:
            raise ValueError(
                f"Invalid generation_method '{value}'. "
                f"Must be one of: {', '.join(sorted(allowed_methods))}"
            )
        return normalized

    @field_validator("tables")
    @classmethod
    def validate_no_duplicate_tables(
        cls, value: list[TemplateTableConfig]
    ) -> list[TemplateTableConfig]:
        """Validate that no duplicate table names exist in the configuration.

        Each table should appear at most once in the template to prevent
        conflicting generation configurations for the same target table.

        Args:
            value: List of table configurations to validate.

        Returns:
            The validated list of table configurations.

        Raises:
            ValueError: If duplicate table names are detected.
        """
        table_names = [config.table_name for config in value]
        seen: set[str] = set()
        duplicates: list[str] = []
        for name in table_names:
            normalized_name = name.strip().lower()
            if normalized_name in seen:
                duplicates.append(name)
            seen.add(normalized_name)
        if duplicates:
            raise ValueError(
                f"Duplicate table names detected: {', '.join(duplicates)}. "
                "Each table must appear only once in the template configuration."
            )
        return value


class TemplateResponse(BaseModel):
    """Response model for generation template data.

    Returned by GET /api/v1/templates/{id} and included in list responses.
    Contains the full template configuration along with metadata such as
    version number, usage statistics, creator information, and timestamps.
    Used by the Template Library (Screen S-003) for browsing and by the
    Generation Wizard for applying template presets.

    Attributes:
        template_id: Unique identifier for the template (MongoDB ObjectId string).
        name: Human-readable template name.
        description: Optional detailed description.
        category: Template category corresponding to an ERP module.
        erp_type: Target ERP system type.
        generation_method: Data generation strategy.
        schema_id: Optional reference to a discovered schema definition.
        tables: Table generation configurations.
        parameters: Generation parameter presets.
        tags: Searchable tags for catalog filtering.
        is_public: Whether template is visible to all tenants.
        version: Template version number (incremented on updates).
        usage_count: Number of times this template has been used.
        created_by: User ID of the template creator.
        created_at: ISO 8601 timestamp of template creation.
        updated_at: ISO 8601 timestamp of last modification.
        tenant_id: Owning tenant namespace.
    """

    template_id: str = Field(
        ...,
        description="Unique template identifier (MongoDB ObjectId)",
    )
    name: str = Field(
        ...,
        description="Template name",
    )
    description: str | None = Field(
        default=None,
        description="Template description",
    )
    category: TemplateCategory = Field(
        ...,
        description="Template category aligned with ERP module",
    )
    erp_type: str = Field(
        ...,
        description="Target ERP system type",
    )
    generation_method: str = Field(
        ...,
        description="Generation method used by this template",
    )
    schema_id: str | None = Field(
        default=None,
        description="Associated schema definition ID",
    )
    tables: list[TemplateTableConfig] = Field(
        default_factory=list,
        description="Table generation configurations",
    )
    parameters: TemplateParameters = Field(
        default_factory=TemplateParameters,
        description="Generation parameter presets",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Searchable tags for catalog filtering",
    )
    is_public: bool = Field(
        default=False,
        description="Whether template is visible to all tenants",
    )
    version: int = Field(
        default=1,
        ge=1,
        description="Template version number (incremented on updates)",
    )
    usage_count: int = Field(
        default=0,
        ge=0,
        description="Number of times this template has been used for generation",
    )
    created_by: str = Field(
        ...,
        description="User ID of the template creator",
    )
    created_at: datetime = Field(
        ...,
        description="Template creation timestamp (ISO 8601)",
    )
    updated_at: datetime = Field(
        ...,
        description="Last modification timestamp (ISO 8601)",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Owning tenant namespace for multi-tenant isolation",
    )

    model_config = ConfigDict(from_attributes=True)


class TemplateListResponse(BaseModel):
    """Paginated list response for the template catalog.

    Returned by GET /api/v1/templates with support for pagination parameters.
    Used by the Template Library (Screen S-003) to display browsable template
    catalogs with filtering and sorting capabilities.

    Attributes:
        templates: List of template response objects for the current page.
        total: Total number of templates matching the query filters.
        page: Current page number (1-indexed).
        page_size: Number of templates per page (1 to 100, default 20).
    """

    templates: list[TemplateResponse] = Field(
        default_factory=list,
        description="List of templates for the current page",
    )
    total: int = Field(
        default=0,
        ge=0,
        description="Total number of templates matching filters",
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Current page number (1-indexed)",
    )
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of templates per page",
    )
