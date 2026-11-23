"""Museum loan and weighing system.

Built on top of :mod:`cs` (the classification ticket library):

* :mod:`museum.weights` - finite-domain weight CSP with interval constraints,
  witness solutions and deletion-based conflict cores.
* :mod:`museum.scale`   - the weighing room: plates pinned to catalog cuts,
  multi-plate joint identification, immutable receipts.
* :mod:`museum.lineage` - named seals, split/merge batches, reweigh receipts,
  whole-trip reconciliation (double surrender, missing in-transit branches,
  duplicate write-off, one shared weight interpretation).
* :mod:`museum.wire`    - offline file-spool messages between local counters.
* :mod:`museum.counter` - a re-openable local counter (SQLite state).
"""
