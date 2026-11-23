"""Offline file-spool message transport between local counters.

There is no network: every counter owns one SQLite database and one spool
directory.  Sending a message writes one immutable, content-addressed envelope
to the recipient's inbox; processing it is idempotent on ``message_id`` and
the sender only deletes its outbox copy after receiving an ack, so a counter
that is closed mid-handover simply re-sends the same envelope when it reopens.

The bus itself never decides whether a message is legal -- it only guarantees
at-least-once, deduplicated delivery.  The application layer (counter/HQ)
makes every authorization and conservation decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class WireError(Exception):
    """Malformed envelope or spool failure."""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def message_id_of(payload: dict[str, Any]) -> str:
    # Content-addressed: the timestamp and any pre-existing id are excluded so
    # an identical re-send keeps the same id and deduplicates downstream.
    body = {
        k: v
        for k, v in payload.items()
        if k not in {"message_id", "created_at"}
    }
    return "M-" + hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()[:20]


@dataclass
class Envelope:
    message_id: str
    sender: str
    recipient: str
    kind: str
    payload: dict[str, Any]
    created_at: float

    @classmethod
    def create(
        cls, sender: str, recipient: str, kind: str, payload: dict[str, Any]
    ) -> "Envelope":
        body = {
            "sender": sender,
            "recipient": recipient,
            "kind": kind,
            "payload": payload,
            "created_at": time.time(),
        }
        return cls(
            message_id=message_id_of(body),
            sender=sender,
            recipient=recipient,
            kind=kind,
            payload=payload,
            created_at=body["created_at"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Envelope":
        return cls(
            message_id=raw["message_id"],
            sender=raw["sender"],
            recipient=raw["recipient"],
            kind=raw["kind"],
            payload=raw["payload"],
            created_at=float(raw["created_at"]),
        )


class MessageBus:
    """One shared directory tree holding per-counter inboxes and outboxes."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, node: str, sub: str) -> Path:
        path = self.root / node / sub
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------- sending
    def send(
        self, sender: str, recipient: str, kind: str, payload: dict[str, Any]
    ) -> Envelope:
        envelope = Envelope.create(sender, recipient, kind, payload)
        outbox = self._dir(sender, "outbox")
        inbox = self._dir(recipient, "inbox")
        body = json.dumps(envelope.as_dict(), ensure_ascii=False, indent=2)
        # Idempotent: an identical re-send keeps the same file name.
        (outbox / f"{envelope.message_id}.json").write_text(body, encoding="utf-8")
        target = inbox / f"{envelope.message_id}.json"
        if not target.exists():
            target.write_text(body, encoding="utf-8")
        return envelope

    def deliver_ack(
        self, sender: str, recipient: str, message_id: str, accepted: bool, note: str = ""
    ) -> Envelope:
        return self.send(
            sender,
            recipient,
            "ack",
            {"ref_message_id": message_id, "accepted": accepted, "note": note},
        )

    # ------------------------------------------------------------- receiving
    def pending(self, node: str) -> list[Envelope]:
        inbox = self._dir(node, "inbox")
        envelopes = []
        for path in sorted(inbox.glob("*.json")):
            envelopes.append(Envelope.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return envelopes

    def outbox_unacked(self, node: str) -> list[Envelope]:
        outbox = self._dir(node, "outbox")
        return [
            Envelope.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(outbox.glob("*.json"))
        ]

    def mark_acked(self, node: str, message_id: str) -> None:
        """Remove an outbox envelope whose ack has come back."""

        outbox = self._dir(node, "outbox")
        path = outbox / f"{message_id}.json"
        if path.exists():
            path.unlink()

    def remove_inbox(self, node: str, message_id: str) -> None:
        path = self._dir(node, "inbox") / f"{message_id}.json"
        if path.exists():
            path.unlink()

    def drain(
        self,
        node: str,
        handler: Callable[[Envelope], tuple[bool, str]],
        *,
        resend_acks: bool = True,
    ) -> list[dict[str, Any]]:
        """Process every inbox envelope once, acking and removing each.

        ``handler`` returns ``(accepted, note)``.  Re-delivery of the same
        ``message_id`` after a restart is harmless: handlers are idempotent
        and duplicate acks are simply overwritten in the sender's inbox.
        """

        report = []
        for envelope in self.pending(node):
            try:
                accepted, note = handler(envelope)
            except Exception as exc:  # noqa: BLE001 - reported back to sender
                accepted, note = False, f"{type(exc).__name__}: {exc}"
            if resend_acks:
                self.deliver_ack(node, envelope.sender, envelope.message_id, accepted, note)
            self.remove_inbox(node, envelope.message_id)
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

    def collect_acks(self, node: str) -> list[dict[str, Any]]:
        """Process ack envelopes and clear the matching outbox entries."""

        out = []
        for envelope in self.pending(node):
            if envelope.kind != "ack":
                continue
            ref = envelope.payload["ref_message_id"]
            self.mark_acked(node, ref)
            self.remove_inbox(node, envelope.message_id)
            out.append(
                {
                    "ref_message_id": ref,
                    "accepted": bool(envelope.payload["accepted"]),
                    "note": envelope.payload.get("note", ""),
                }
            )
        return out
