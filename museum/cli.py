#!/usr/bin/env python3
"""Command line entry point for the weighing room and lineage demo.

The weighing room lives beside a :mod:`cs` ticket library database::

    python3 -m museum.cli scale-init   --cs demo/tour/ticketlib.db \\
        --scale demo/tour/scale.db --domains '{"A":[1,3],"B":[1,3],"C":[2,5]}'
    python3 -m museum.cli sign         --cs ... --scale ... \\
        --plate P0 --tickets W-ONE-A,W-ONE-B --lo 4 --hi 4
    python3 -m museum.cli compat       --scale ...
    python3 -m museum.cli check-masses --scale ... --masses '{"A":1,"B":3,"C":2}'
    python3 -m museum.cli receipts     --scale ...
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from cs.store import Store
from museum.scale import ScaleError, ScaleOffice


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _open(args: argparse.Namespace) -> tuple[Store, ScaleOffice]:
    store = Store.open(args.cs)
    office = ScaleOffice.open(args.scale, store)
    return store, office


def cmd_scale_init(args: argparse.Namespace) -> int:
    store, office = _open(args)
    try:
        version = office.declare_domains(json.loads(args.domains))
        _dump({"domain_version": version, "domains": json.loads(args.domains)})
    finally:
        store.close()
        office.close()
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    store, office = _open(args)
    try:
        result = office.sign_plate(
            args.plate, [t.strip() for t in args.tickets.split(",") if t.strip()],
            (args.lo, args.hi),
        )
        _dump(result.as_dict())
        return 0 if result.state == "signed" else 1
    finally:
        store.close()
        office.close()


def cmd_compat(args: argparse.Namespace) -> int:
    store, office = _open(args)
    try:
        _dump(office.compatibility())
    finally:
        store.close()
        office.close()
    return 0


def cmd_joint(args: argparse.Namespace) -> int:
    store, office = _open(args)
    try:
        _dump(office.joint_appraisal(args.plates))
    finally:
        store.close()
        office.close()
    return 0


def cmd_check_masses(args: argparse.Namespace) -> int:
    store, office = _open(args)
    try:
        report = office.check_mass_table(json.loads(args.masses))
        _dump(report)
        return 0 if report["supported"] else 1
    finally:
        store.close()
        office.close()


def cmd_receipts(args: argparse.Namespace) -> int:
    store, office = _open(args)
    try:
        _dump({"receipts": office.list_receipts()})
    finally:
        store.close()
        office.close()
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    store, office = _open(args)
    try:
        _dump({"history": office.history()})
    finally:
        store.close()
        office.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m museum.cli",
        description="Weighing room over the cs ticket library.",
    )
    parser.add_argument("--cs", required=True, help="cs ticket library SQLite path")
    parser.add_argument("--scale", required=True, help="weighing room SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scale-init", help="declare legal period mass domains")
    p.add_argument("--domains", required=True, help='JSON, e.g. {"A":[1,3]}')
    p.set_defaults(func=cmd_scale_init)

    p = sub.add_parser("sign", help="sign one plate")
    p.add_argument("--plate", required=True)
    p.add_argument("--tickets", required=True, help="comma-separated ticket ids")
    p.add_argument("--lo", required=True, type=int)
    p.add_argument("--hi", required=True, type=int)
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("compat", help="compatibility of all receipts + witness masses")
    p.set_defaults(func=cmd_compat)

    p = sub.add_parser("joint", help="multi-plate joint identification")
    p.add_argument("plates", nargs="+")
    p.set_defaults(func=cmd_joint)

    p = sub.add_parser("check-masses", help="verify one explicit mass table")
    p.add_argument("--masses", required=True, help='JSON, e.g. {"A":1,"B":3}')
    p.set_defaults(func=cmd_check_masses)

    p = sub.add_parser("receipts", help="list valid receipts on file")
    p.set_defaults(func=cmd_receipts)

    p = sub.add_parser("history", help="weighing room event history")
    p.set_defaults(func=cmd_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ScaleError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
