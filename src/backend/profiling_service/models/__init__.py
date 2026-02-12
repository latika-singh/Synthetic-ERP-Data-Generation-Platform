"""Data models package for the Profiling Service.

Provides Pydantic 2.x document models for the two core MongoDB collections
used by the Profiling Service:

* **schema_definitions** — ERP schema metadata (tables, columns, relationships,
  constraints, indexes) captured via the discovery pipeline.  See
  :mod:`profiling_service.models.schema_definition`.

* **statistical_profiles** — Per-column and per-table statistical summaries
  (distribution parameters, pattern metadata, cardinality, null rates, value
  ranges, percentiles, and correlation matrices) captured during profiling.
  See :mod:`profiling_service.models.statistical_profile`.

**Privacy (Constraint C-001):**
    Both models store *metadata only* — no raw production data values are ever
    persisted.  ``sample_formats`` entries are anonymised format exemplars.

**Multi-tenant Isolation (R-007):**
    Repository classes in each sub-module scope every MongoDB query to a
    ``tenant_id`` to prevent cross-tenant data access.
"""

from profiling_service.models.schema_definition import (
    ColumnDataType,
    ColumnDefinition,
    ConstraintDefinition,
    ERPModule,
    ERPType,
    IndexDefinition,
    RelationshipDefinition,
    RelationshipType,
    SchemaDefinition,
    SchemaDefinitionRepository,
    SchemaDiscoveryRequest,
    SchemaDiscoveryResponse,
    SchemaStatus,
    TableDefinition,
)
from profiling_service.models.statistical_profile import (
    ColumnProfile,
    CorrelationEntry,
    CorrelationEntry,
    DataCategory,
    DistributionParameters,
    DistributionType,
    FrequencyDistribution,
    PatternMetadata,
    Percentiles,
    ProfileStatus,
    StatisticalProfile,
    StatisticalProfileRepository,
    TableProfile,
    ValueRange,
)

__all__: list[str] = [
    # schema_definition exports
    "SchemaDefinition",
    "SchemaDefinitionRepository",
    "SchemaDiscoveryRequest",
    "SchemaDiscoveryResponse",
    "TableDefinition",
    "ColumnDefinition",
    "ConstraintDefinition",
    "RelationshipDefinition",
    "IndexDefinition",
    "ERPType",
    "ERPModule",
    "ColumnDataType",
    "RelationshipType",
    "SchemaStatus",
    # statistical_profile exports
    "StatisticalProfile",
    "StatisticalProfileRepository",
    "ColumnProfile",
    "TableProfile",
    "CorrelationEntry",
    "CorrelationEntry",
    "DistributionParameters",
    "DistributionType",
    "PatternMetadata",
    "ValueRange",
    "Percentiles",
    "FrequencyDistribution",
    "DataCategory",
    "ProfileStatus",
]

__version__: str = "1.0.0"
