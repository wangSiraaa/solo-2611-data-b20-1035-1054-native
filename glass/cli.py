#!/usr/bin/env python3
"""Command line for the glazing workshop.

Examples
--------
    # Define a frame groove (also creates its catalog node)
    python3 -m glass --db w.db slot LEFT --color gray --grained

    # File a survey (revision 1) with bench-rotated measurements
    python3 -m glass --db w.db survey q1 --ticket TL --revision 1 \
        --class LEFT --measurements q1.json

    # Sign the slot and fetch the slip back later from anywhere
    python3 -m glass --db w.db sign SLIP-L --slot LEFT --position pane
    python3 -m glass --db w.db fetch signing SLIP-L

    # Register stock and plan a resuming cut order
    python3 -m glass --db w.db stock B1 --remaining 2 --pieces stock.json
    python3 -m glass --db w.db plan P1 --batch B1 --signing SLIP-L --signing SLIP-R
    python3 -m glass --db w.db fetch plan P1

A signing slip is returned by id, independent of where the database lives, and
`fetch signing` never recomputes it, so a later bead change cannot rewrite it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cs.store import CatalogError, StoreError
from glass import fit
from glass.cutting import PlanError
from glass.store import GlassError, GlazingStore


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dump(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_slot(args: argparse.Namespace) -> int:
    with GlazingStore.open(args.db) as gs:
        slot = gs.define_slot(
            args.slot_id, args.color, args.grained, args.description,
            name=args.name,
        )
        _dump(slot.as_dict())
    return 0


def cmd_survey(args: argparse.Namespace) -> int:
    measurements = _load(args.measurements) if args.measurements else []
    with GlazingStore.open(args.db) as gs:
        result = gs.submit_quantity(
            args.survey_id,
            args.ticket,
            args.revision,
            args.catalog_class,
            measurements,
            amount=args.amount,
        )
        _dump(result)
    return 0


def cmd_survey_batch(args: argparse.Namespace) -> int:
    with GlazingStore.open(args.db) as gs:
        _dump(gs.submit_quantities(_load(args.file)))
    return 0


def cmd_withdraw(args: argparse.Namespace) -> int:
    with GlazingStore.open(args.db) as gs:
        _dump(gs.withdraw_quantity(args.ticket, args.revision))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    with GlazingStore.open(args.db) as gs:
        if args.survey_id:
            _dump(gs.resolve_survey(args.survey_id).as_dict())
        else:
            _dump([r.as_dict() for r in gs.resolve_all()])
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    with GlazingStore.open(args.db) as gs:
        _dump(gs.sign(args.signing_id, args.slot_id, args.position))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    with GlazingStore.open(args.db) as gs:
        _dump(gs.explain_size(args.signing_id, args.width, args.height))
    return 0


def cmd_stock(args: argparse.Namespace) -> int:
    pieces = _load(args.pieces)
    with GlazingStore.open(args.db) as gs:
        _dump(gs.register_stock(args.batch_id, pieces, args.remaining))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    with GlazingStore.open(args.db) as gs:
        _dump(
            gs.plan(
                args.plan_id,
                args.batch,
                args.signing,
                minimise_cuts=args.minimise,
            )
        )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    with GlazingStore.open(args.db) as gs:
        if args.kind == "signing":
            _dump(gs.get_signing(args.obj_id))
        elif args.kind == "plan":
            _dump(gs.get_plan(args.obj_id))
        elif args.kind == "stock":
            pieces, remaining = gs.get_stock(args.obj_id)
            _dump({"batch_id": args.obj_id,
                   "pieces": [p.as_dict() for p in pieces],
                   "remaining_cuts": remaining})
        elif args.kind == "signings":
            _dump([s for s in gs.signings()])
        else:
            _dump(gs.surveys())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m glass")
    parser.add_argument("--db", default="glass.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("slot")
    p.add_argument("slot_id")
    p.add_argument("--color", required=True)
    p.add_argument("--grained", action="store_true")
    p.add_argument("--description")
    p.add_argument("--name")
    p.set_defaults(func=cmd_slot)

    p = sub.add_parser("survey")
    p.add_argument("survey_id")
    p.add_argument("--ticket", required=True)
    p.add_argument("--revision", type=int, required=True)
    p.add_argument("--class", dest="catalog_class", action="append", required=True)
    p.add_argument("--measurements")
    p.add_argument("--amount", type=int)
    p.set_defaults(func=cmd_survey)

    p = sub.add_parser("survey-batch")
    p.add_argument("file")
    p.set_defaults(func=cmd_survey_batch)

    p = sub.add_parser("withdraw")
    p.add_argument("ticket")
    p.add_argument("--revision", type=int)
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("resolve")
    p.add_argument("survey_id", nargs="?")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("sign")
    p.add_argument("signing_id")
    p.add_argument("--slot", dest="slot_id", required=True)
    p.add_argument("--position", default="single")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("explain")
    p.add_argument("signing_id")
    p.add_argument("width", type=int)
    p.add_argument("height", type=int)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("stock")
    p.add_argument("batch_id")
    p.add_argument("--pieces", required=True)
    p.add_argument("--remaining", type=int, required=True)
    p.set_defaults(func=cmd_stock)

    p = sub.add_parser("plan")
    p.add_argument("plan_id")
    p.add_argument("--batch", required=True)
    p.add_argument("--signing", action="append", required=True)
    p.add_argument("--minimise", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("fetch")
    p.add_argument("kind", choices=["signing", "signings", "plan", "stock", "surveys"])
    p.add_argument("obj_id", nargs="?")
    p.set_defaults(func=cmd_fetch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (
        StoreError, CatalogError, GlassError, fit.FitError, PlanError
    ) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
