"""SQLite schema for the seal-label, mold-archive and freight-disposition layer.

The reagent service remains the sole owner of bottles / reservations / runs.
This layer only references those facts through foreign keys:

* a legal label is backed by exactly one *started* reagent run whose
  ``consumed`` reservation actually spent one portion of named seal clay;
* the per-portion sequence number points at that concrete reservation row, so
  "only a really consumed named bottle / reservation / portion can make a
  label" is enforced by data rather than by a private counter.

Design notes
------------
* ``molds`` are registered per furnace batch (one physical long mold per
  batch). The same mold segment may back many boxes, so no uniqueness on
  (mold, position) is ever declared.
* ``waste_vouchers`` record the shift's trim evidence. A gap between imprint
  segments may only skip teeth inside a voucher's interval; a gap with no
  voucher is illegal.
* Imprint reads are versioned. A new version is required whenever an old label
  gains additional read cells; old versions stay on file for the archive.
* The freight workflow tables persist every lock / break / disposition so that
  a reopen after a lost return packet recognizes broken named seals, shipped
  boxes and the remaining to-do actions.
"""

from __future__ import annotations

SEAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS molds (
    mold_id          TEXT PRIMARY KEY,
    furnace_batch    TEXT NOT NULL UNIQUE,
    -- fixed-order teeth, each '0' or '1'
    pattern          TEXT NOT NULL,
    tooth_count      INTEGER NOT NULL CHECK (tooth_count BETWEEN 1 AND 40),
    registered_day   INTEGER NOT NULL CHECK (registered_day >= 0),
    CHECK (length(pattern) = tooth_count)
);

CREATE TABLE IF NOT EXISTS mold_splice_marks (
    mold_id     TEXT NOT NULL REFERENCES molds(mold_id),
    -- mark between tooth adjacency (0 .. tooth_count-2); validated by service
    adjacency   INTEGER NOT NULL CHECK (adjacency >= 0),
    PRIMARY KEY (mold_id, adjacency)
);

-- Shift trim evidence. A voucher authorizes skipping the teeth strictly
-- between two imprint segments; the skipped count must lie in [min_skip,
-- max_skip] and be consistent with the voucher's tooth span. Cross-table
-- bounds are enforced by the archive service.
CREATE TABLE IF NOT EXISTS waste_vouchers (
    voucher_id    TEXT PRIMARY KEY,
    mold_id       TEXT NOT NULL REFERENCES molds(mold_id),
    shift_id      TEXT NOT NULL,
    first_tooth   INTEGER NOT NULL CHECK (first_tooth >= 0),
    last_tooth    INTEGER NOT NULL,
    min_skip      INTEGER NOT NULL CHECK (min_skip >= 1),
    max_skip      INTEGER NOT NULL,
    CHECK (last_tooth >= first_tooth),
    CHECK (max_skip >= min_skip)
);

CREATE TABLE IF NOT EXISTS labels (
    label_id          TEXT PRIMARY KEY,
    box_id            TEXT NOT NULL UNIQUE,
    run_id            TEXT NOT NULL REFERENCES runs(run_id),
    reservation_id    TEXT NOT NULL REFERENCES reservations(reservation_id),
    portion_seq       INTEGER NOT NULL CHECK (portion_seq >= 1),
    issued_day        INTEGER NOT NULL CHECK (issued_day >= 0),
    current_version   INTEGER NOT NULL DEFAULT 1 CHECK (current_version >= 1),
    -- one strip per (consumed reservation, ordinal portion)
    UNIQUE (reservation_id, portion_seq)
);

CREATE TABLE IF NOT EXISTS label_attempts (
    scan_id       TEXT PRIMARY KEY,
    box_id        TEXT NOT NULL,
    run_id        TEXT,
    day           INTEGER NOT NULL CHECK (day >= 0),
    outcome       TEXT NOT NULL CHECK (outcome IN (
                       'issued', 'duplicate', 'reserved_only',
                       'expired_unstarted', 'insufficient', 'released')),
    label_id      TEXT REFERENCES labels(label_id),
    detail        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imprint_versions (
    imprint_id        TEXT NOT NULL,
    version           INTEGER NOT NULL CHECK (version >= 1),
    label_id          TEXT NOT NULL REFERENCES labels(label_id),
    -- one string per segment; each character is '0', '1' or '?'
    segments_json     TEXT NOT NULL,
    -- JSON list of local adjacency indices carrying a splice mark
    splice_marks_json TEXT NOT NULL,
    -- 'present' means the locating hole exists: forward reading only.
    -- 'missing' means the whole hole tore off: reverse reading is allowed.
    locating_hole     TEXT NOT NULL CHECK (locating_hole IN
                          ('present', 'missing')),
    recorded_day      INTEGER NOT NULL CHECK (recorded_day >= 0),
    note              TEXT,
    PRIMARY KEY (imprint_id, version),
    UNIQUE (label_id, version)
);

CREATE TABLE IF NOT EXISTS imprint_reconstructions (
    imprint_id      TEXT NOT NULL,
    version         INTEGER NOT NULL,
    label_id        TEXT NOT NULL,
    mold_id         TEXT NOT NULL REFERENCES molds(mold_id),
    direction       TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
    placement_json  TEXT NOT NULL,
    vouchers_json   TEXT NOT NULL,
    PRIMARY KEY (imprint_id, version, mold_id, direction, placement_json),
    FOREIGN KEY (imprint_id, version)
        REFERENCES imprint_versions(imprint_id, version)
);

-- ---------------------------------------------------------------------------
-- Freight station: packing tree (declared once, then frozen)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS freight_nodes (
    node_id        TEXT PRIMARY KEY,
    parent_id      TEXT REFERENCES freight_nodes(node_id),
    kind           TEXT NOT NULL CHECK (kind IN ('cabinet', 'leaf')),
    depth          INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 2),
    customs_locked INTEGER NOT NULL CHECK (customs_locked IN (0, 1)),
    -- only set for leaves
    label_id       TEXT REFERENCES labels(label_id),
    UNIQUE (parent_id, node_id)
);

CREATE TABLE IF NOT EXISTS freight_workflows (
    workflow_id        TEXT PRIMARY KEY,
    mode               TEXT NOT NULL CHECK (mode IN
                           ('cabinet_first', 'box_first')),
    state              TEXT NOT NULL CHECK (state IN
                           ('open', 'closed', 'finalized')),
    created_day        INTEGER NOT NULL,
    closed_day         INTEGER,
    finalized_day      INTEGER
);

-- cabinet_first: locks and break receipts per cabinet
CREATE TABLE IF NOT EXISTS cabinet_locks (
    lock_id          TEXT PRIMARY KEY,
    workflow_id      TEXT NOT NULL REFERENCES freight_workflows(workflow_id),
    node_id          TEXT NOT NULL REFERENCES freight_nodes(node_id),
    state            TEXT NOT NULL CHECK (state IN
                         ('locked_intact', 'broken', 'sealed_returned')),
    locked_day       INTEGER NOT NULL,
    broken_day       INTEGER,
    break_receipt    TEXT,
    returned_day     INTEGER
);

-- box_first: named responsibilities attached to leaf boxes, and their merges
CREATE TABLE IF NOT EXISTS box_holds (
    hold_id          TEXT PRIMARY KEY,
    workflow_id      TEXT NOT NULL REFERENCES freight_workflows(workflow_id),
    label_id         TEXT NOT NULL REFERENCES labels(label_id),
    state            TEXT NOT NULL CHECK (state IN
                         ('held', 'merged', 'released')),
    created_day      INTEGER NOT NULL,
    merged_into      TEXT REFERENCES cabinet_locks(lock_id),
    released_day     INTEGER
);

CREATE TABLE IF NOT EXISTS cabinet_merges (
    merge_id         TEXT PRIMARY KEY,
    workflow_id      TEXT NOT NULL REFERENCES freight_workflows(workflow_id),
    node_id          TEXT NOT NULL REFERENCES freight_nodes(node_id),
    state            TEXT NOT NULL CHECK (state IN
                         ('pending', 'sealed_returned', 'withdrawn')),
    created_day      INTEGER NOT NULL,
    sealed_day       INTEGER,
    withdrawn_day    INTEGER
);

-- Every executed disposition, keyed so a replayed packet can never apply the
-- same leaf twice (the same box can never both ship to the customer and hang
-- on a return cart).
CREATE TABLE IF NOT EXISTS leaf_dispositions (
    disposition_id   TEXT PRIMARY KEY,
    workflow_id      TEXT NOT NULL REFERENCES freight_workflows(workflow_id),
    label_id         TEXT NOT NULL REFERENCES labels(label_id),
    action           TEXT NOT NULL CHECK (action IN
                         ('released', 'detained')),
    via              TEXT NOT NULL CHECK (via IN
                         ('leaf_charge', 'inside_opened_cabinet',
                          'inside_sealed_cabinet')),
    lock_id          TEXT REFERENCES cabinet_locks(lock_id),
    day              INTEGER NOT NULL,
    UNIQUE (workflow_id, label_id)
);
"""


def initialize_seals(conn) -> None:
    conn.executescript(SEAL_SCHEMA)
