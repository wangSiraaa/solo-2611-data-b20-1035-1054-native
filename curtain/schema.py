"""Curtain-side tables, kept in the same SQLite store as the reagent schema.

The measurement room's facts (stations, references, edge readings, panels,
shows) live next to the inventory tables so reference registration can check
*consumed portions* against the very reservation rows the reagent service
owns — there is no second copy of the checkout facts.

Loading lists are append-only history: once sealed, their content columns
(loaded spares, per-show arrangements, revision) never change.  Only the
``status`` / ``handover_json`` bookkeeping columns may move, so an old list
stays exactly as signed even after a reference is re-approved and a newer
list is sealed.
"""

from __future__ import annotations

import sqlite3

CURTAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS curtain_stations (
    station_id  TEXT PRIMARY KEY,
    lo          INTEGER NOT NULL,
    hi          INTEGER NOT NULL,
    CHECK (lo <= hi)
);

-- A reference reading of a bottle portion.  Registration is only accepted
-- while the bottle has more *consumed* portions than already-registered
-- references: each successfully drawn portion backs at most one reference.
CREATE TABLE IF NOT EXISTS curtain_references (
    ref_id          TEXT PRIMARY KEY,
    bottle_id       TEXT NOT NULL REFERENCES bottles(bottle_id),
    station_id      TEXT NOT NULL REFERENCES curtain_stations(station_id),
    reading         INTEGER NOT NULL,
    error           INTEGER NOT NULL CHECK (error >= 0),
    status          TEXT NOT NULL CHECK (status IN ('registered', 'revoked')),
    registered_day  INTEGER NOT NULL,
    revoked_day     INTEGER
);

CREATE TABLE IF NOT EXISTS curtain_edges (
    edge_id     TEXT PRIMARY KEY
);

-- At most one trusted reading per edge per station; re-measurement replaces.
CREATE TABLE IF NOT EXISTS curtain_edge_readings (
    edge_id     TEXT NOT NULL REFERENCES curtain_edges(edge_id),
    station_id  TEXT NOT NULL REFERENCES curtain_stations(station_id),
    reading     INTEGER NOT NULL,
    error       INTEGER NOT NULL CHECK (error >= 0),
    PRIMARY KEY (edge_id, station_id)
);

CREATE TABLE IF NOT EXISTS curtain_panels (
    panel_id    TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('old', 'spare')),
    left_edge   TEXT NOT NULL REFERENCES curtain_edges(edge_id),
    right_edge  TEXT NOT NULL REFERENCES curtain_edges(edge_id),
    reversible  INTEGER NOT NULL DEFAULT 0 CHECK (reversible IN (0, 1))
);

CREATE TABLE IF NOT EXISTS curtain_shows (
    show_id     TEXT PRIMARY KEY,
    tolerance   INTEGER NOT NULL CHECK (tolerance >= 0)
);

CREATE TABLE IF NOT EXISTS curtain_show_positions (
    show_id     TEXT NOT NULL REFERENCES curtain_shows(show_id),
    position    INTEGER NOT NULL CHECK (position >= 1),
    panel_id    TEXT NOT NULL REFERENCES curtain_panels(panel_id),
    PRIMARY KEY (show_id, position)
);

-- Which spare may replace which position of which show (准许的备布).
CREATE TABLE IF NOT EXISTS curtain_spare_permissions (
    show_id     TEXT NOT NULL REFERENCES curtain_shows(show_id),
    position    INTEGER NOT NULL CHECK (position >= 1),
    panel_id    TEXT NOT NULL REFERENCES curtain_panels(panel_id),
    PRIMARY KEY (show_id, position, panel_id)
);

-- A countersigned snapshot of every record active at signing time.  Seam
-- queries and loading lists always reference one frozen revision, so later
-- registrations / revocations never rewrite an answered question.
CREATE TABLE IF NOT EXISTS curtain_revisions (
    revision_id   TEXT PRIMARY KEY,
    created_day   INTEGER NOT NULL,
    records_json  TEXT NOT NULL,
    records_hash  TEXT NOT NULL,
    feasible      INTEGER NOT NULL CHECK (feasible IN (0, 1)),
    witness_json  TEXT,
    core_json     TEXT
);

-- One physical loading list per truck: the loaded spares plus, per show,
-- the sealed primary arrangement and one sealed contingency per possibly
-- missing spare.  Content columns are frozen by trigger once inserted.
CREATE TABLE IF NOT EXISTS curtain_loading_lists (
    list_id       TEXT PRIMARY KEY,
    revision_id   TEXT NOT NULL REFERENCES curtain_revisions(revision_id),
    sealed_day    INTEGER NOT NULL,
    loaded_json   TEXT NOT NULL,
    shows_json    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('sealed', 'departed')),
    handover_json TEXT
);

-- Physical spares already handed over to a show; a locked panel can never
-- be taken by another troupe or loaded onto another truck.
CREATE TABLE IF NOT EXISTS curtain_locks (
    panel_id    TEXT PRIMARY KEY REFERENCES curtain_panels(panel_id),
    show_id     TEXT NOT NULL,
    list_id     TEXT NOT NULL,
    locked_day  INTEGER NOT NULL
);

-- Sealed content is immutable: only status and the handover record may
-- change after insertion.
CREATE TRIGGER IF NOT EXISTS curtain_lists_content_immutable
BEFORE UPDATE ON curtain_loading_lists
WHEN OLD.revision_id <> NEW.revision_id
  OR OLD.sealed_day <> NEW.sealed_day
  OR OLD.loaded_json <> NEW.loaded_json
  OR OLD.shows_json <> NEW.shows_json
BEGIN
    SELECT RAISE(ABORT, 'sealed loading list content is immutable');
END;

CREATE TRIGGER IF NOT EXISTS curtain_lists_no_delete
BEFORE DELETE ON curtain_loading_lists
BEGIN
    SELECT RAISE(ABORT, 'sealed loading lists are retained');
END;

CREATE TRIGGER IF NOT EXISTS curtain_revisions_immutable
BEFORE UPDATE ON curtain_revisions
BEGIN
    SELECT RAISE(ABORT, 'countersigned revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS curtain_revisions_no_delete
BEFORE DELETE ON curtain_revisions
BEGIN
    SELECT RAISE(ABORT, 'countersigned revisions are retained');
END;

CREATE TRIGGER IF NOT EXISTS curtain_locks_no_update
BEFORE UPDATE ON curtain_locks
BEGIN
    SELECT RAISE(ABORT, 'handover locks are immutable');
END;

CREATE TRIGGER IF NOT EXISTS curtain_locks_no_delete
BEFORE DELETE ON curtain_locks
BEGIN
    SELECT RAISE(ABORT, 'handover locks are retained');
END;
"""


def initialize_curtain(conn: sqlite3.Connection) -> None:
    conn.executescript(CURTAIN_SCHEMA)
