"""Schema discovery package for the Profiling Service.

Provides ERP schema metadata extraction, foreign-key relationship
discovery, and table dependency analysis.

**Core components:**

* :class:`SchemaExtractor` — Extracts table / column metadata from ERP
  systems via connectors.
* :class:`RelationshipMapper` — Discovers FK relationships across tables
  and ERP modules.
* :class:`DependencyAnalyzer` — Computes a topological generation order
  from a dependency graph (Kahn's algorithm).

**Result models:**

* :class:`DependencyAnalysisResult` — Complete analysis output consumed by
  the Generation Engine.
* :class:`DependencyLevel` — Tables grouped by depth in the dependency tree.
* :class:`CycleInfo` — Circular dependency detection result.

**Constraint C-001:**
    No production data is accessed or stored.  All operations work
    exclusively with structural metadata (table names, column names,
    data types, relationships).

**Constraint C-005:**
    Supports four ERP types (SAP, Oracle EBS, Dynamics 365, JDBC legacy)
    and four ERP modules (Financial Accounting, HR, Sales & Distribution,
    Material Management).
"""

from profiling_service.discovery.dependency_analyzer import (
    CycleInfo,
    DependencyAnalysisResult,
    DependencyAnalyzer,
    DependencyLevel,
)
from profiling_service.discovery.relationship_mapper import RelationshipMapper
from profiling_service.discovery.schema_extractor import SchemaExtractor


__all__ = [
    "CycleInfo",
    "DependencyAnalysisResult",
    "DependencyAnalyzer",
    "DependencyLevel",
    "RelationshipMapper",
    "SchemaExtractor",
]

__version__ = "1.0.0"
