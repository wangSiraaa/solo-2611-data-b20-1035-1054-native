"""Re-openable local counters (本地柜台) over SQLite and the file spool.

Three counters run fully offline in the demo:

* the HQ counter (总馆柜台, at the 甲 site) owns the authoritative
  :class:`~museum.lineage.LineageLedger`, the weighing room and the ticket
  library, and performs the final write-off;
* branch counters (分馆柜台, 乙/丙) keep a local seal view and drive
  forwards, split/merge handovers, reweighs and returns through messages.

Every state-changing branch action is one request envelope.  HQ validates it
against the lineage rules, appends it to the persisted ledger, and replies
with either an accepted ledger-snapshot delta or a rejection.  Requests are
deduplicated by ``message_id``; reopening a counter re-sends unacked requests
without ever moving a seal twice.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from museum.lineage.ledger import LineageError, LineageLedger
from museum.wire import Envelope, MessageBus

COUNTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_requests (
    message_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seal_view (
    seal_id TEXT PRIMARY KEY,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

HQ = "HQ"


class CounterError(Exception):
    """Illegal local counter action."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Counter:
    """One local counter.  ``role`` is ``"hq"`` or ``"branch"``."""

    def __init__(
        self,
        node_id: str,
        db_path: str | Path,
        bus: MessageBus,
        *,
        role: str = "branch",
        ledger: LineageLedger | None = None,
    ) -> None:
        if role not in {"hq", "branch"}:
            raise CounterError("role must be 'hq' or 'branch'")
        self.node_id = node_id
        self.role = role
        # Requests are always addressed to the logical recipient "HQ"; the
        # HQ counter's physical node id may differ (e.g. site "JIA").
        self.mail_names = [node_id] + (["HQ"] if role == "hq" and node_id != HQ else [])
        self.bus = bus
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(COUNTER_SCHEMA)
        self._set_meta("node_id", node_id)
        self._set_meta("role", role)
        # Only HQ holds the authoritative ledger in memory; branches keep a
        # read-only mirror rebuilt from accepted snapshots.
        self.ledger = ledger if ledger is not None and role == "hq" else (
            LineageLedger() if role == "hq" else None
        )
        if role == "hq":
            self._load_ledger()

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        if self.role == "hq" and self.ledger is not None:
            self._save_ledger()
        self.conn.close()

    def __enter__(self) -> "Counter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @classmethod
    def reopen(cls, node_id: str, db_path: str | Path, bus: MessageBus) -> "Counter":
        conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM meta WHERE key='role'").fetchone()
        role = row["value"] if row else "branch"
        conn.close()
        return cls(node_id, db_path, bus, role=role)

    # ------------------------------------------------------------ meta/log
    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _log(self, kind: str, detail: Mapping[str, Any]) -> None:
        seq_row = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM local_log").fetchone()
        self.conn.execute(
            "INSERT INTO local_log(seq, kind, detail) VALUES(?,?,?)",
            (int(seq_row["n"]), kind, _json(detail)),
        )

    def log(self) -> list[dict[str, Any]]:
        return [
            {"seq": int(r["seq"]), "kind": r["kind"], "detail": json.loads(r["detail"])}
            for r in self.conn.execute(
                "SELECT seq, kind, detail FROM local_log ORDER BY log_id"
            ).fetchall()
        ]

    # ------------------------------------------------------------ HQ ledger
    def _load_ledger(self) -> None:
        raw = self._get_meta("ledger_snapshot")
        if raw:
            self.ledger = LineageLedger.restore(json.loads(raw))

    def _save_ledger(self) -> None:
        self._set_meta("ledger_snapshot", _json(self.ledger.snapshot()))

    # ------------------------------------------------------------ idempotency
    def _already_processed(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM processed_messages WHERE message_id=?", (message_id,)
        ).fetchone()

    def _remember(self, envelope: Envelope, accepted: bool, note: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_messages"
            "(message_id, kind, accepted, note, created_at) VALUES(?,?,?,?,?)",
            (envelope.message_id, envelope.kind, 1 if accepted else 0, note, envelope.created_at),
        )

    def _remember_request(self, envelope: Envelope) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO pending_requests(message_id, kind, payload, created_at) "
            "VALUES(?,?,?,?)",
            (
                envelope.message_id,
                envelope.kind,
                _json(envelope.payload),
                envelope.created_at,
            ),
        )

    def _drop_request(self, message_id: str) -> None:
        self.conn.execute("DELETE FROM pending_requests WHERE message_id=?", (message_id,))

    def pending_requests(self) -> list[dict[str, Any]]:
        return [
            {
                "message_id": r["message_id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"],
            }
            for r in self.conn.execute(
                "SELECT * FROM pending_requests ORDER BY created_at, message_id"
            ).fetchall()
        ]

    # ------------------------------------------------------------ branch API
    def _request(self, kind: str, payload: Mapping[str, Any]) -> Envelope:
        if self.role != "branch":
            raise CounterError("only branch counters send handover requests")
        envelope = self.bus.send(self.node_id, HQ, kind, dict(payload))
        self._remember_request(envelope)
        self._log("request_sent", {"message_id": envelope.message_id, "kind": kind})
        return envelope

    def request_forward(self, batch_id: str, seal_id: str, to_branch: str) -> Envelope:
        return self._request(
            "forward",
            {
                "batch_id": batch_id,
                "seal_id": seal_id,
                "from_branch": self.node_id,
                "to_branch": to_branch,
            },
        )

    def request_split_merge(
        self,
        batch_id: str,
        old_seal_ids: Sequence[str],
        new_seals: Sequence[Mapping[str, Any]],
    ) -> Envelope:
        return self._request(
            "split_merge",
            {
                "batch_id": batch_id,
                "actor": self.node_id,
                "old_seal_ids": list(old_seal_ids),
                "new_seals": [dict(s) for s in new_seals],
                "authorized_open": True,
            },
        )

    def request_reweigh(self, seal_id: str, interval: tuple[int, int]) -> Envelope:
        return self._request(
            "reweigh",
            {
                "reweigh_id": f"RW-{self.node_id}-{seal_id}-{len(self.log()) + 1}",
                "seal_id": seal_id,
                "branch": self.node_id,
                "lo": int(interval[0]),
                "hi": int(interval[1]),
            },
        )

    def request_return(self, seal_id: str) -> Envelope:
        return self._request(
            "return", {"seal_id": seal_id, "branch": self.node_id}
        )

    # ------------------------------------------------------------ HQ dispatch
    def hq_dispatch(
        self,
        batch_id: str,
        seal_id: str,
        holder: str,
        contents: Mapping[str, int],
        coin_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> Envelope:
        """The lending library ships an original seal to a branch."""

        if self.role != "hq":
            raise CounterError("only HQ dispatches original seals")
        seal = self.ledger.dispatch(batch_id, seal_id, holder, contents, coin_ids)
        envelope = self.bus.send(
            HQ,
            holder,
            "delivery",
            {"batch_id": batch_id, "seal": seal.as_dict()},
        )
        self._remember(envelope, True, "dispatched")
        self._save_ledger()
        self._log("dispatch", {"seal_id": seal_id, "holder": holder})
        return envelope

    # ------------------------------------------------------------ message run
    def run_inbox(self) -> list[dict[str, Any]]:
        """Process every queued business envelope; safe to call after restart.

        The HQ counter drains both its physical node inbox and the logical
        ``HQ`` inbox that branch requests are addressed to.  Ack envelopes
        are left in place for :meth:`run_acks` and never reach a handler.
        """

        report = []
        for name in self.mail_names:
            for envelope in self.bus.pending(name):
                if envelope.kind == "ack":
                    continue
                try:
                    accepted, note = self._handle(envelope)
                except Exception as exc:  # noqa: BLE001 - reported back to sender
                    accepted, note = False, f"{type(exc).__name__}: {exc}"
                self.bus.deliver_ack(
                    name, envelope.sender, envelope.message_id, accepted, note
                )
                self.bus.remove_inbox(name, envelope.message_id)
                report.append(
                    {
                        "message_id": envelope.message_id,
                        "kind": envelope.kind,
                        "sender": envelope.sender,
                        "accepted": accepted,
                        "note": note,
                    }
                )
        return report

    def run_acks(self) -> list[dict[str, Any]]:
        acks = self.bus.collect_acks(self.node_id)
        for ack in acks:
            if ack["accepted"]:
                self._drop_request(ack["ref_message_id"])
            self._log(
                "ack_received",
                {"ref": ack["ref_message_id"], "accepted": ack["accepted"], "note": ack["note"]},
            )
        return acks

    def resend_unacked(self) -> list[str]:
        """Re-send requests still awaiting an ack after a crash/restart.

        Only envelopes tracked in :attr:`pending_requests` are re-sent.  The
        content hash fixes the message id, so HQ deduplicates the replay and
        no seal ever moves twice; HQ's own delivery envelopes are not branch
        requests and are never part of this retry set.
        """

        tracked = {row["message_id"] for row in self.conn.execute(
            "SELECT message_id FROM pending_requests"
        ).fetchall()}
        resent = []
        for envelope in self.bus.outbox_unacked(self.node_id):
            if envelope.message_id in tracked and envelope.recipient == HQ:
                self.bus.send(
                    envelope.sender, envelope.recipient, envelope.kind, envelope.payload
                )
                resent.append(envelope.message_id)
        return resent

    # ------------------------------------------------------------ handlers
    def _handle(self, envelope: Envelope) -> tuple[bool, str]:
        duplicate = self._already_processed(envelope.message_id)
        if duplicate is not None:
            note = f"duplicate of {envelope.message_id}; no state change"
            if self.role == "hq":
                self._reply_snapshot(envelope)
            return bool(duplicate["accepted"]), note

        if self.role == "hq":
            return self._handle_at_hq(envelope)
        return self._handle_at_branch(envelope)

    def _handle_at_branch(self, envelope: Envelope) -> tuple[bool, str]:
        if envelope.kind == "delivery":
            seal = envelope.payload["seal"]
            if seal.get("surrendered"):
                # A replayed delivery of a seal already surrendered at HQ must
                # not resurrect it in the branch's local view.
                self.close_seal_view(seal["seal_id"])
                self._remember(envelope, True, f"stale delivery of surrendered {seal['seal_id']}")
                return True, "stale delivery ignored"
            self.conn.execute(
                "INSERT OR REPLACE INTO seal_view(seal_id, detail) VALUES(?,?)",
                (seal["seal_id"], _json(seal)),
            )
            self._remember(envelope, True, f"received seal {seal['seal_id']}")
            self._log("delivery_received", {"seal_id": seal["seal_id"]})
            return True, f"held seal {seal['seal_id']}"
        if envelope.kind == "seal_closed":
            seal_id = envelope.payload["seal_id"]
            self.close_seal_view(seal_id)
            self._remember(envelope, True, f"seal {seal_id} closed at HQ")
            self._log("seal_closed", {"seal_id": seal_id})
            return True, f"seal {seal_id} closed"
        self._remember(envelope, False, f"branch cannot process {envelope.kind}")
        return False, f"branch cannot process {envelope.kind}"

    def _handle_at_hq(self, envelope: Envelope) -> tuple[bool, str]:
        payload = envelope.payload
        try:
            if envelope.kind == "forward":
                self.ledger.forward(
                    payload["batch_id"],
                    payload["seal_id"],
                    payload["from_branch"],
                    payload["to_branch"],
                )
                note = f"forwarded {payload['seal_id']} to {payload['to_branch']}"
            elif envelope.kind == "split_merge":
                self.ledger.split_merge(
                    payload["batch_id"],
                    payload["actor"],
                    payload["old_seal_ids"],
                    payload["new_seals"],
                    authorized_open=bool(payload.get("authorized_open", False)),
                )
                note = f"handover {payload['batch_id']} accepted"
            elif envelope.kind == "reweigh":
                self.ledger.reweigh(
                    payload["reweigh_id"],
                    payload["seal_id"],
                    payload["branch"],
                    (int(payload["lo"]), int(payload["hi"])),
                )
                note = f"reweigh {payload['reweigh_id']} recorded"
            elif envelope.kind == "return":
                self.ledger.return_seal(payload["seal_id"], payload["branch"])
                note = f"seal {payload['seal_id']} returned to HQ"
            elif envelope.kind == "delivery":
                # HQ receives its own delivery only as a replay artifact.
                note = "delivery replay ignored at HQ"
            else:
                raise LineageError(f"unknown message kind {envelope.kind!r}")
        except LineageError as exc:
            self._remember(envelope, False, str(exc))
            self._log("request_rejected", {"message_id": envelope.message_id, "error": str(exc)})
            return False, str(exc)

        self._remember(envelope, True, note)
        self._save_ledger()
        self._log("request_accepted", {"message_id": envelope.message_id, "kind": envelope.kind})
        self._reply_snapshot(envelope)
        return True, note

    def _reply_snapshot(self, envelope: Envelope) -> None:
        """Push the resulting seal views to every affected branch."""

        if envelope.kind == "forward":
            seal = self.ledger.seals[envelope.payload["seal_id"]]
            self.bus.send(
                HQ,
                envelope.payload["to_branch"],
                "delivery",
                {"batch_id": envelope.payload["batch_id"], "seal": seal.as_dict()},
            )
            self.bus.send(
                HQ,
                envelope.payload["from_branch"],
                "seal_closed",
                {"seal_id": seal.seal_id, "batch_id": envelope.payload["batch_id"]},
            )
        elif envelope.kind == "split_merge":
            for raw in envelope.payload["new_seals"]:
                seal = self.ledger.seals[raw["seal_id"]]
                self.bus.send(
                    HQ,
                    seal.holder,
                    "delivery",
                    {"batch_id": envelope.payload["batch_id"], "seal": seal.as_dict()},
                )
            # The actor's old seals are gone; tell every branch that held one.
            holders = {
                self.ledger.seals[sid].holder for sid in envelope.payload["old_seal_ids"]
            }
            for branch in holders | {envelope.payload["actor"]}:
                for sid in envelope.payload["old_seal_ids"]:
                    self.bus.send(
                        HQ,
                        branch,
                        "seal_closed",
                        {"seal_id": sid, "batch_id": envelope.payload["batch_id"]},
                    )
        elif envelope.kind == "return":
            seal = self.ledger.seals[envelope.payload["seal_id"]]
            self.bus.send(
                HQ,
                envelope.payload["branch"],
                "seal_closed",
                {"seal_id": seal.seal_id},
            )

    # ------------------------------------------------------------ branch views
    def held_seals(self) -> list[dict[str, Any]]:
        return [
            json.loads(r["detail"])
            for r in self.conn.execute("SELECT detail FROM seal_view ORDER BY seal_id").fetchall()
        ]

    def close_seal_view(self, seal_id: str) -> None:
        self.conn.execute("DELETE FROM seal_view WHERE seal_id=?", (seal_id,))

    # ------------------------------------------------------------ settlement
    def settle(
        self,
        mass_domains: Mapping[str, Sequence[int]],
        scale_receipts: Sequence[Mapping[str, Any]],
        *,
        proposed_masses: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        if self.role != "hq":
            raise CounterError("only HQ can settle a trip")
        report = self.ledger.reconcile(
            mass_domains, scale_receipts, proposed_masses=proposed_masses
        )
        self._log("settle", {"ok": report["ok"], "problems": report["problems"]})
        self._save_ledger()
        return report
