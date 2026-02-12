"""Referential integrity enforcement sub-package for the Generation Engine.

This package ensures that generated synthetic data maintains valid foreign key
relationships across tables and ERP modules (Financial Accounting, HR, Sales &
Distribution, Material Management).  It provides two core capabilities:

1. **Relationship tracking and FK enforcement** via
   :class:`RelationshipManager` — manages parent-child record ID mappings
   during synthetic data generation, validates referential constraints, and
   resolves cross-module references.

2. **Table dependency graph construction with topological sorting** via
   :class:`DependencyGraph` — builds a directed acyclic graph of table
   dependencies based on FK relationships, computes generation order using
   Kahn's algorithm, detects and resolves circular references.

These components are critical for achieving the **30 % referential integrity
component** of the weighted quality scoring model
(``Q = 0.4 * statistical + 0.3 * business_rules + 0.3 * referential_integrity``).

This package operates exclusively on **schema metadata** — no production data
is accessed or stored, in compliance with Constraint C-001.

Usage::

    from generation_engine.integrity import DependencyGraph, TableNode
    from generation_engine.integrity import RelationshipManager

    graph = DependencyGraph()
    graph.build_from_schema(schema_definition)
    order = graph.get_topological_order()
"""

from __future__ import annotations


__version__: str = "1.0.0"

# ---------------------------------------------------------------------------
# ERP Module Constants (re-exported for convenience)
# ---------------------------------------------------------------------------

ERP_MODULE_FINANCIAL_ACCOUNTING: str = "financial_accounting"
ERP_MODULE_HR: str = "hr"
ERP_MODULE_SALES_DISTRIBUTION: str = "sales_distribution"
ERP_MODULE_MATERIAL_MANAGEMENT: str = "material_management"

# ---------------------------------------------------------------------------
# Public API — lazy imports via __getattr__ to avoid eagerly pulling in
# heavy dependencies (numpy, pydantic, etc.) at package import time.
# ---------------------------------------------------------------------------

__all__: list[str] = [
    # Constants
    "ERP_MODULE_FINANCIAL_ACCOUNTING",
    "ERP_MODULE_HR",
    "ERP_MODULE_MATERIAL_MANAGEMENT",
    "ERP_MODULE_SALES_DISTRIBUTION",
    "CycleResolutionStrategy",
    "DependencyEdge",
    # From dependency_graph
    "DependencyGraph",
    "ForeignKeyRelationship",
    "GeneratedKeyMapping",
    "IntegrityViolation",
    # From relationship_manager
    "RelationshipManager",
    "TableNode",
]

# Mapping from public symbol name to (module_path, attribute_name).
_DEPENDENCY_GRAPH_SYMBOLS: frozenset[str] = frozenset({
    "DependencyGraph",
    "TableNode",
    "DependencyEdge",
    "CycleResolutionStrategy",
})

_RELATIONSHIP_MANAGER_SYMBOLS: frozenset[str] = frozenset({
    "RelationshipManager",
    "ForeignKeyRelationship",
    "GeneratedKeyMapping",
    "IntegrityViolation",
})


def __getattr__(name: str) -> object:
    """Lazily import public symbols on first access.

    This avoids importing heavyweight dependencies (numpy, pydantic,
    structlog, etc.) until the symbol is actually needed, keeping
    ``import generation_engine.integrity`` virtually free.

    Args:
        name: The attribute name being accessed.

    Returns:
        The requested symbol from the appropriate submodule.

    Raises:
        AttributeError: If *name* is not a known public symbol.
    """
    if name in _DEPENDENCY_GRAPH_SYMBOLS:
        import importlib  # noqa: PLC0415

        _mod = importlib.import_module(
            "generation_engine.integrity.dependency_graph"
        )
        return getattr(_mod, name)

    if name in _RELATIONSHIP_MANAGER_SYMBOLS:
        import importlib  # noqa: PLC0415

        _mod = importlib.import_module(
            "generation_engine.integrity.relationship_manager"
        )
        return getattr(_mod, name)

    raise AttributeError(f"module 'generation_engine.integrity' has no attribute {name!r}")
