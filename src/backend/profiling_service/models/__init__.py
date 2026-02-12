"""Data models package for the Profiling Service.

Provides Pydantic 2.x document models for the two core MongoDB collections
used by the Profiling Service, along with a :data:`MODEL_REGISTRY` that maps
collection names to their corresponding Pydantic model classes, and a
:func:`get_model_for_collection` helper for dynamic look-ups.

Collections
-----------

* **schema_definitions** — ERP schema metadata (tables, columns, relationships,
  constraints, indexes) captured via the discovery pipeline.  See
  :mod:`profiling_service.models.schema_definition`.

* **statistical_profiles** — Per-column and per-table statistical summaries
  (distribution parameters, pattern metadata, cardinality, null rates, value
  ranges, percentiles, and correlation matrices) captured during profiling.
  See :mod:`profiling_service.models.statistical_profile`.

Registry
--------

:data:`MODEL_REGISTRY` enables callers (e.g. the API Gateway, shared
middleware, or the Generation Engine) to resolve a MongoDB collection
name to its authoritative Pydantic model at runtime without hard-coding
import paths:

    >>> from profiling_service.models import get_model_for_collection
    >>> model_cls = get_model_for_collection("schema_definitions")
    >>> model_cls.__name__
    'SchemaDefinition'

**Privacy (Constraint C-001):**
    Both models store *metadata only* — no raw production data values are ever
    persisted.  ``sample_formats`` entries are anonymised format exemplars.

**Multi-tenant Isolation (R-007):**
    Repository classes in each sub-module scope every MongoDB query to a
    ``tenant_id`` to prevent cross-tenant data access.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Schema Definition module — ERP schema metadata models
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Statistical Profile module — statistical metadata models
# ---------------------------------------------------------------------------
from profiling_service.models.statistical_profile import (
    ColumnProfile,
    CorrelationEntry,
    DataCategory,
    DistributionParameters,
    DistributionType,
    FrequencyDistribution,
    PatternMetadata,
    Percentiles,
    ProfileRequest,
    ProfileResponse,
    ProfileStatus,
    StatisticalProfile,
    StatisticalProfileRepository,
    TableProfile,
    ValueRange,
)


# ===================================================================
# Model Registry
# ===================================================================

MODEL_REGISTRY: dict[str, type] = {
    "schema_definitions": SchemaDefinition,
    "statistical_profiles": StatisticalProfile,
}
"""Mapping of MongoDB collection names to their authoritative Pydantic model
classes.

This registry is consumed by generic utility code that needs to resolve a
collection name to its corresponding Pydantic model for serialisation,
deserialisation, or validation at runtime.

Keys correspond exactly to the MongoDB collection names defined in
``shared.database.mongodb`` (``COLLECTION_SCHEMA_DEFINITIONS`` and
``COLLECTION_STATISTICAL_PROFILES``).
"""


def get_model_for_collection(collection_name: str) -> type | None:
    """Return the Pydantic model class registered for *collection_name*.

    This is a convenience wrapper around :data:`MODEL_REGISTRY` that
    provides a safe look-up returning ``None`` when the collection name
    is not recognised, rather than raising a ``KeyError``.

    Args:
        collection_name: The MongoDB collection name to look up
            (e.g. ``"schema_definitions"`` or ``"statistical_profiles"``).

    Returns:
        The Pydantic model :class:`type` if *collection_name* is in the
        registry, otherwise ``None``.

    Examples:
        >>> get_model_for_collection("schema_definitions")
        <class 'profiling_service.models.schema_definition.SchemaDefinition'>

        >>> get_model_for_collection("nonexistent") is None
        True
    """
    return MODEL_REGISTRY.get(collection_name)


# ===================================================================
# Public API
# ===================================================================

__all__: list[str] = [
    # --- Registry ---
    "MODEL_REGISTRY",
    "ColumnDataType",
    "ColumnDefinition",
    "ColumnProfile",
    "ConstraintDefinition",
    "CorrelationEntry",
    "DataCategory",
    "DistributionParameters",
    "DistributionType",
    "ERPModule",
    "ERPType",
    "FrequencyDistribution",
    "IndexDefinition",
    "PatternMetadata",
    "Percentiles",
    "ProfileRequest",
    "ProfileResponse",
    "ProfileStatus",
    "RelationshipDefinition",
    "RelationshipType",
    # --- schema_definition exports ---
    "SchemaDefinition",
    "SchemaDefinitionRepository",
    "SchemaDiscoveryRequest",
    "SchemaDiscoveryResponse",
    "SchemaStatus",
    # --- statistical_profile exports ---
    "StatisticalProfile",
    "StatisticalProfileRepository",
    "TableDefinition",
    "TableProfile",
    "ValueRange",
    "get_model_for_collection",
]
