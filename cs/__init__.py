"""Independent classification catalog and fact summary store.

Public entry points:

* :mod:`cs.catalog` - pure DAG algorithms (cycles, ancestors, descendants,
  coverage explanation).
* :mod:`cs.store` - SQLite-backed catalog and fact store with atomic bundles
  and cut-pinned queries.
* :mod:`cs.rights` - rights clearance over the catalog DAG (per-holder terms,
  specificity reduction, union/intersection, holds).
* :mod:`cs.exhibit` - exhibition-label feasibility (crops, signatures,
  cumulative protection, batch preference solving) and ASCII rendering.
* :mod:`cs.delivery` - public record, central masters, branch vouchers, audit.
* :mod:`cs.dataset` - loader for the offline exhibition-rights dataset.
* :mod:`cs.cli` - command line interface.
"""

from .store import (  # noqa: F401
    CatalogError,
    CatalogState,
    Committed,
    ConflictError,
    Cut,
    Fact,
    FactError,
    ProductError,
    Store,
    StoreError,
    ingest_operation,
)

__all__ = [
    "CatalogError",
    "CatalogState",
    "Committed",
    "ConflictError",
    "Cut",
    "Fact",
    "FactError",
    "ProductError",
    "Store",
    "StoreError",
    "ingest_operation",
]
