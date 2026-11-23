"""Carbon-copy press (出证柜): colour-channel mechanics and job planning.

Mechanics
---------
Every printable field of a sheet row -- the Chinese name (``zh``), the
foreign name (``fl``) and the points (``pts``) -- is printed on one *colour
channel*.  The two name halves use the channels pre-registered with their
term pair; all points share one dedicated channel (:data:`POINTS_CHANNEL`).

Each physical form (联) is a named slip registered to one recipient unit with

* ``shows``  -- channels that leave a visible imprint on this slip, and
* ``blocks`` -- channels this slip stops outright.

One press lowers the whole master sheet onto the stack, top to bottom.  When
a field's channel reaches a slip: if the slip shows the channel, the complete
field is left on it; then, if the slip blocks the channel, no slip below
receives it.  A slip may block a channel it does not show.

Issuance contract
-----------------
Each recipient unit declares, per record, fields it *must see* and fields
*strictly forbidden*.  A forbidden field leaving even one imprint on the
unit's slip is a mis-issuance (误发) and the job is infeasible; every other
field may be carried out.  Each unit must receive exactly one of its own
registered slips (chosen from at most :data:`MAX_SPARES` spares); the whole
stack is pressed once, with one naming for the whole stack.

:func:`plan_press` decides feasibility.  On success it returns the physical
serials, the stacking order and the exact imprints every slip keeps; on
failure it points at the blocking terminology or distribution requirements.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .errors import PressError
from .sheet import MasterSheet
from .terms import TermOffice, TermPair, explain_naming, solve_namings

#: every row's points are printed on this one dedicated channel
POINTS_CHANNEL = "PTS"

FIELD_KINDS = ("zh", "fl", "pts")

MAX_RECIPIENTS = 6
MAX_SPARES = 2


# --------------------------------------------------------------------------- #
# forms and recipients
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FormSpec:
    """One named physical slip (联)."""

    serial: str
    recipient: str
    shows: FrozenSet[str]
    blocks: FrozenSet[str]

    def as_dict(self) -> dict:
        return {
            "serial": self.serial,
            "recipient": self.recipient,
            "shows": sorted(self.shows),
            "blocks": sorted(self.blocks),
        }


@dataclass(frozen=True)
class RecipientSpec:
    """One unit's distribution requirements, per record."""

    recipient: str
    must_see: FrozenSet[Tuple[str, str]]   # (record_id, field kind)
    forbid: FrozenSet[Tuple[str, str]]     # (record_id, field kind)

    @staticmethod
    def from_doc(doc: dict) -> "RecipientSpec":
        """Parse ``{"recipient": ..., "must_see": {rid: [kinds]}, "forbid": {...}}``."""
        def pairs(key) -> FrozenSet[Tuple[str, str]]:
            out = set()
            for rid, kinds in (doc.get(key) or {}).items():
                for kind in kinds:
                    if kind not in FIELD_KINDS:
                        raise PressError(
                            f"unknown field kind {kind!r} (expected one of {FIELD_KINDS})",
                            location=f"{doc.get('recipient')}:{rid}",
                        )
                    out.add((rid, kind))
            return frozenset(out)

        recipient = doc.get("recipient")
        if not (isinstance(recipient, str) and recipient.strip()):
            raise PressError("recipient must be a non-empty string", location="recipient")
        return RecipientSpec(recipient=recipient, must_see=pairs("must_see"), forbid=pairs("forbid"))


def validate_stock(recipients: Sequence[RecipientSpec], stock: Sequence[FormSpec]) -> None:
    """Enforce the scale contract and the basic sanity of the form cabinet."""
    if len(recipients) > MAX_RECIPIENTS:
        raise PressError(
            f"at most {MAX_RECIPIENTS} recipients per sheet (got {len(recipients)})",
            location="recipients",
        )
    names = [r.recipient for r in recipients]
    if len(set(names)) != len(names):
        raise PressError("each recipient unit may appear only once", location="recipients")
    for spec in recipients:
        clash = spec.must_see & spec.forbid
        if clash:
            rid, kind = sorted(clash)[0]
            raise PressError(
                f"recipient {spec.recipient!r} both requires and forbids {kind} of {rid!r}",
                location=spec.recipient,
            )
    per_recipient: Dict[str, int] = {}
    for form in stock:
        if not form.serial.strip():
            raise PressError("form serial must be a non-empty string", location="serial")
        per_recipient[form.recipient] = per_recipient.get(form.recipient, 0) + 1
    for recipient, n in sorted(per_recipient.items()):
        if n > MAX_SPARES:
            raise PressError(
                f"recipient {recipient!r} has {n} registered slips; at most "
                f"{MAX_SPARES} spares are allowed",
                location=recipient,
            )
    serials = [f.serial for f in stock]
    if len(set(serials)) != len(serials):
        raise PressError("form serials must be unique", location="serial")


# --------------------------------------------------------------------------- #
# the press itself
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PressField:
    record_id: str
    kind: str           # "zh" | "fl" | "pts"
    channel: str
    value: object       # str for names, int for points


def sheet_fields(sheet: MasterSheet, naming: Dict[str, TermPair]) -> List[PressField]:
    """All fields one press lays down, in deterministic order."""
    fields: List[PressField] = []
    for row in sheet.rows:  # rows are already sorted by record_id
        pair = naming[row.course_id]
        fields.append(PressField(row.record_id, "zh", pair.zh_channel, pair.zh))
        fields.append(PressField(row.record_id, "fl", pair.fl_channel, pair.fl))
        fields.append(PressField(row.record_id, "pts", POINTS_CHANNEL, row.points))
    return fields


@dataclass
class PressOutcome:
    """What one press actually left on the stack."""

    imprints: Dict[str, Dict[Tuple[str, str], object]] = field(default_factory=dict)
    blocked_channels: Dict[str, List[str]] = field(default_factory=dict)

    def imprints_on(self, serial: str) -> Dict[Tuple[str, str], object]:
        return self.imprints.get(serial, {})


def simulate_press(fields: Sequence[PressField], stack: Sequence[FormSpec]) -> PressOutcome:
    """Lower all fields onto the stack, top to bottom."""
    outcome = PressOutcome()
    outcome.imprints = {f.serial: {} for f in stack}
    outcome.blocked_channels = {f.serial: [] for f in stack}
    for fld in fields:
        for form in stack:
            if fld.channel in form.shows:
                outcome.imprints[form.serial][(fld.record_id, fld.kind)] = fld.value
            if fld.channel in form.blocks:
                outcome.blocked_channels[form.serial].append(fld.channel)
                break  # slips below never see this channel
    return outcome


def check_distribution(
    outcome: PressOutcome,
    stack: Sequence[FormSpec],
    recipients: Sequence[RecipientSpec],
) -> List[dict]:
    """Audit one pressed stack against every unit's requirements.

    Any forbidden field that left an imprint is a mis-issuance
    (``forbidden_imprinted``); any required field missing from a unit's slip
    is ``must_see_missing``.
    """
    by_recipient = {r.recipient: r for r in recipients}
    violations: List[dict] = []
    for form in stack:
        spec = by_recipient.get(form.recipient)
        if spec is None:
            violations.append({
                "code": "unregistered_recipient",
                "message": f"slip {form.serial!r} belongs to unlisted recipient "
                           f"{form.recipient!r}",
                "location": form.recipient,
            })
            continue
        have = set(outcome.imprints_on(form.serial))
        for rid, kind in sorted(spec.must_see - have):
            violations.append({
                "code": "must_see_missing",
                "message": f"recipient {form.recipient!r} must see {kind} of {rid!r} "
                           f"but slip {form.serial!r} has no imprint of it",
                "location": form.recipient,
                "detail": {"record_id": rid, "field": kind, "serial": form.serial},
            })
        for rid, kind in sorted(spec.forbid & have):
            violations.append({
                "code": "forbidden_imprinted",
                "message": f"mis-issuance: forbidden {kind} of {rid!r} left an imprint "
                           f"on {form.recipient!r}'s slip {form.serial!r}",
                "location": form.recipient,
                "detail": {"record_id": rid, "field": kind, "serial": form.serial},
            })
    return violations


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
@dataclass
class PressPlan:
    """A feasible press job: one naming, one slip per unit, one stack order."""

    sheet: MasterSheet
    naming: Dict[str, TermPair]          # course_id -> chosen pair
    stack: Tuple[FormSpec, ...]          # top first
    outcome: PressOutcome

    @property
    def serials(self) -> List[str]:
        return [f.serial for f in self.stack]

    def imprints_document(self, serial: str) -> List[dict]:
        return [
            {"record_id": rid, "field": kind, "value": value}
            for (rid, kind), value in sorted(self.outcome.imprints_on(serial).items())
        ]

    def receipt_for(self, recipient: str) -> dict:
        """The receipt handed to one unit: only what its own slip shows.

        Nothing else is on it, so a confidential field can never leak back
        through a receipt -- forbidden fields never reached the slip.
        """
        for form in self.stack:
            if form.recipient == recipient:
                return {
                    "recipient": recipient,
                    "serial": form.serial,
                    "sheet_id": self.sheet.sheet_id,
                    "fields": self.imprints_document(form.serial),
                }
        raise PressError(f"recipient {recipient!r} has no slip in this plan", location=recipient)

    def as_dict(self) -> dict:
        return {
            "sheet_id": self.sheet.sheet_id,
            "naming": {
                cid: pair.as_dict() for cid, pair in sorted(self.naming.items())
            },
            "stack": [
                {"serial": f.serial, "recipient": f.recipient,
                 "shows": sorted(f.shows), "blocks": sorted(f.blocks)}
                for f in self.stack
            ],
            "imprints": {
                f.serial: self.imprints_document(f.serial) for f in self.stack
            },
            "blocked_channels": {
                serial: sorted(set(chs))
                for serial, chs in sorted(self.outcome.blocked_channels.items())
            },
        }


@dataclass
class PressFailure:
    """Infeasible job: which terminology or distribution requirements block it."""

    sheet_id: str
    reasons: List[dict]
    naming_explanation: Optional[dict] = None

    @property
    def feasible(self) -> bool:
        return False

    def as_dict(self) -> dict:
        out = {"feasible": False, "sheet_id": self.sheet_id, "reasons": self.reasons}
        if self.naming_explanation is not None:
            out["naming_explanation"] = self.naming_explanation
        return out


def plan_press(
    office: TermOffice,
    sheet: MasterSheet,
    recipients: Sequence[RecipientSpec],
    stock: Sequence[FormSpec],
    *,
    exclude_serials: Sequence[str] = (),
    naming_limit: int = 32,
) -> "PressPlan | PressFailure":
    """Decide whether a sheet can be pressed for the given recipients.

    Searches, in deterministic order: every legal naming (terminology rules),
    every choice of one slip per unit (from their registered spares, minus
    ``exclude_serials``), and every stacking order.  Returns the first
    feasible :class:`PressPlan`, or a :class:`PressFailure` locating the
    blocking requirements.
    """
    validate_stock(recipients, stock)
    known_records = {row.record_id for row in sheet.rows}
    for spec in recipients:
        for rid, kind in sorted(spec.must_see | spec.forbid):
            if rid not in known_records:
                raise PressError(
                    f"recipient {spec.recipient!r} refers to record {rid!r} which is "
                    f"not on sheet {sheet.sheet_id}",
                    location=spec.recipient,
                )

    excluded = set(exclude_serials)
    usable: Dict[str, List[FormSpec]] = {}
    for form in sorted(stock, key=lambda f: f.serial):
        if form.serial in excluded:
            continue
        usable.setdefault(form.recipient, []).append(form)
    for spec in recipients:
        if not usable.get(spec.recipient):
            return PressFailure(
                sheet_id=sheet.sheet_id,
                reasons=[{
                    "code": "no_slip_available",
                    "message": f"recipient {spec.recipient!r} has no usable registered slip",
                    "location": spec.recipient,
                }],
            )

    namings = solve_namings(office, sheet, limit=naming_limit)
    if not namings:
        explanation = explain_naming(office, sheet)
        return PressFailure(
            sheet_id=sheet.sheet_id,
            reasons=[{
                "code": "no_legal_naming",
                "message": "the terminology office has no legal naming for this sheet; "
                           "see naming_explanation",
            }] + explanation["reasons"],
            naming_explanation=explanation,
        )

    recipient_order = sorted(recipients, key=lambda r: r.recipient)
    combos = list(itertools.product(*(usable[r.recipient] for r in recipient_order)))

    best: Optional[Tuple[int, dict, List[dict]]] = None  # (n_violations, naming, violations)
    for naming_doc in namings:
        naming = {cid: office.get_pair(pid) for cid, pid in naming_doc.items()}
        fields = sheet_fields(sheet, naming)
        for combo in combos:
            for order in itertools.permutations(combo):
                stack = tuple(order)
                outcome = simulate_press(fields, stack)
                violations = check_distribution(outcome, stack, recipients)
                if not violations:
                    return PressPlan(sheet=sheet, naming=naming, stack=stack, outcome=outcome)
                if best is None or len(violations) < best[0]:
                    best = (len(violations), naming_doc, violations)

    assert best is not None  # at least one naming and one combo existed
    _, naming_doc, violations = best
    reasons = [{
        "code": "distribution_infeasible",
        "message": "no slip choice and stacking order satisfies every recipient; "
                   "the closest attempt still violates the requirements below",
        "detail": {"closest_naming": naming_doc},
    }] + violations
    return PressFailure(sheet_id=sheet.sheet_id, reasons=reasons)


__all__ = [
    "POINTS_CHANNEL",
    "FIELD_KINDS",
    "MAX_RECIPIENTS",
    "MAX_SPARES",
    "FormSpec",
    "RecipientSpec",
    "PressField",
    "PressOutcome",
    "PressPlan",
    "PressFailure",
    "validate_stock",
    "sheet_fields",
    "simulate_press",
    "check_distribution",
    "plan_press",
]
