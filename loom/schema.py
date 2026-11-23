"""SQLite schema for the weaving room (shuttles, looms, weave jobs).

The loom tables live in the same database as the reagent inventory and share
its connection / transaction helpers. They never keep a private copy of
portion counts:

* portions only enter a shuttle through a *load*, and every load is backed by
  a real reagent run whose ``reservations`` rows the inventory service owns;
* a shuttle's remaining portions are derived from its append-only event
  ledger (``shuttle_events``) on every read, the same way bottle allocation is
  derived from reservation rows.

The event ledger is the audit trail demanded by the task: with no weave job
in sight one can still ask for a shuttle's remaining thread, its side, its
machine, and the exact install / pass / carry / handoff receipts that made it
so. The ledger is append-only and every receipt id is unique, so replaying a
receipt returns the stored outcome instead of moving the shuttle twice.
"""

from __future__ import annotations

LOOM_SCHEMA = """
CREATE TABLE IF NOT EXISTS looms (
    loom_id        TEXT PRIMARY KEY,
    -- current head side; flips exactly when a pass (one woven row) completes
    head_side      TEXT NOT NULL CHECK (head_side IN ('left', 'right')),
    created_day    INTEGER NOT NULL
);

-- A physical shuttle. Core/color/factory side are fixed for its whole life;
-- machine/side/state are just the materialized tip of shuttle_events.
CREATE TABLE IF NOT EXISTS shuttles (
    shuttle_id      TEXT PRIMARY KEY,
    core_id         TEXT NOT NULL,
    color           TEXT NOT NULL,
    factory_side    TEXT NOT NULL CHECK (factory_side IN ('left', 'right')),
    loom_id         TEXT REFERENCES looms(loom_id),
    side            TEXT CHECK (side IS NULL OR side IN ('left', 'right')),
    -- stored: registered (not yet loaded), loaded, empty, unloaded
    state           TEXT NOT NULL CHECK (state IN (
                         'registered', 'loaded', 'empty', 'unloaded')),
    created_day     INTEGER NOT NULL,
    UNIQUE (core_id)
);

-- Append-only ledger. Receipt ids are globally unique, hence replays are
-- idempotent; triggers below make the table insert-only.
CREATE TABLE IF NOT EXISTS shuttle_events (
    event_id        TEXT PRIMARY KEY,
    shuttle_id      TEXT NOT NULL REFERENCES shuttles(shuttle_id),
    event_type      TEXT NOT NULL CHECK (event_type IN (
                         'load', 'unload', 'carry', 'pass', 'handoff')),
    day             INTEGER NOT NULL,
    loom_id         TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('left', 'right')),
    portions_delta  INTEGER NOT NULL,
    other_loom_id   TEXT,
    load_id         TEXT,
    run_id          TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);

-- One 装梭 business request, in its two natural phases. The run id is also
-- its reservation holder inside the reagent service.
CREATE TABLE IF NOT EXISTS loom_loads (
    load_id          TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL UNIQUE,
    shuttle_id       TEXT NOT NULL REFERENCES shuttles(shuttle_id),
    loom_id          TEXT NOT NULL REFERENCES looms(loom_id),
    bottle_id        TEXT NOT NULL,
    color            TEXT NOT NULL,
    portions         INTEGER NOT NULL CHECK (portions > 0),
    reserve_day      INTEGER NOT NULL,
    -- reserved -> loaded (start ok) / failed (start invalid or short)
    state            TEXT NOT NULL CHECK (state IN (
                          'reserved', 'loaded', 'failed')),
    terminal_day     INTEGER,
    terminal_json    TEXT
);

CREATE TABLE IF NOT EXISTS weave_jobs (
    job_id           TEXT PRIMARY KEY,
    loom_id          TEXT NOT NULL REFERENCES looms(loom_id),
    pattern_json     TEXT NOT NULL,
    ticket_budget    INTEGER NOT NULL CHECK (ticket_budget >= 0),
    tickets_used     INTEGER NOT NULL DEFAULT 0 CHECK (tickets_used >= 0),
    -- head side snapshotted when the tape was planned and verified
    initial_head_side TEXT NOT NULL CHECK (initial_head_side IN ('left', 'right')),
    rows_done        INTEGER NOT NULL DEFAULT 0,
    -- planned -> running -> completed / failed / void
    state            TEXT NOT NULL CHECK (state IN (
                          'planned', 'running', 'completed', 'failed', 'void')),
    failure_reason   TEXT,
    created_day      INTEGER NOT NULL,
    finished_day     INTEGER,
    product_json     TEXT
);

-- At most one unfinished weave job per loom.
CREATE UNIQUE INDEX IF NOT EXISTS weave_jobs_one_active_idx
    ON weave_jobs(loom_id)
    WHERE state IN ('planned', 'running');

CREATE TABLE IF NOT EXISTS weave_steps (
    job_id           TEXT NOT NULL REFERENCES weave_jobs(job_id),
    step_index       INTEGER NOT NULL CHECK (step_index >= 0),
    row_index        INTEGER,
    kind             TEXT NOT NULL CHECK (kind IN ('carry', 'pass')),
    shuttle_id       TEXT NOT NULL REFERENCES shuttles(shuttle_id),
    side             TEXT NOT NULL CHECK (side IN ('left', 'right')),
    color            TEXT,
    planned_event_id TEXT NOT NULL,
    event_id         TEXT UNIQUE REFERENCES shuttle_events(event_id),
    status           TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'done', 'skipped')),
    detail_json      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (job_id, step_index)
);

-- Structural capacity: at most six shuttles on one machine.
CREATE TRIGGER IF NOT EXISTS shuttles_loom_capacity_insert
BEFORE INSERT ON shuttles
WHEN NEW.loom_id IS NOT NULL
     AND (SELECT COUNT(*) FROM shuttles WHERE loom_id = NEW.loom_id) >= 6
BEGIN
    SELECT RAISE(ABORT, 'loom already holds the maximum of 6 shuttles');
END;

CREATE TRIGGER IF NOT EXISTS shuttles_loom_capacity_update
BEFORE UPDATE OF loom_id ON shuttles
WHEN NEW.loom_id IS NOT NULL
     AND (SELECT COUNT(*) FROM shuttles
          WHERE loom_id = NEW.loom_id AND shuttle_id <> OLD.shuttle_id) >= 6
BEGIN
    SELECT RAISE(ABORT, 'loom already holds the maximum of 6 shuttles');
END;

-- The event ledger is append-only: no updates, no deletes, ever.
CREATE TRIGGER IF NOT EXISTS shuttle_events_no_update
BEFORE UPDATE ON shuttle_events
BEGIN
    SELECT RAISE(ABORT, 'shuttle event ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS shuttle_events_no_delete
BEFORE DELETE ON shuttle_events
BEGIN
    SELECT RAISE(ABORT, 'shuttle event ledger is append-only');
END;

-- Weave step rows are frozen once executed; a finished job never moves again.
CREATE TRIGGER IF NOT EXISTS weave_steps_done_immutable
BEFORE UPDATE ON weave_steps
WHEN OLD.status = 'done' AND NEW.status <> 'done'
BEGIN
    SELECT RAISE(ABORT, 'an executed weave step is immutable');
END;

CREATE TRIGGER IF NOT EXISTS weave_jobs_terminal_immutable
BEFORE UPDATE ON weave_jobs
WHEN OLD.state IN ('completed', 'failed', 'void')
BEGIN
    SELECT RAISE(ABORT, 'terminal weave job is immutable');
END;
"""
