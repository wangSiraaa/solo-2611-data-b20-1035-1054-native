"""Independent CLI entry points for the curtain measurement service.

Shares the SQLite file with the reagent service (``python3 -m reagent``);
both sides read the same reservation rows.
"""

from __future__ import annotations

import argparse
import json
import sys

from reagent.errors import ReagentError
from reagent.schema import connect, initialize

from .schema import initialize_curtain
from .service import CurtainService


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _service(args: argparse.Namespace) -> CurtainService:
    conn = connect(args.db)
    initialize(conn)
    initialize_curtain(conn)
    return CurtainService(conn)


def cmd_setup(args: argparse.Namespace) -> dict:
    service = _service(args)
    return service.setup(_load_json(args.document), args.day)


def cmd_register_ref(args: argparse.Namespace) -> dict:
    return _service(args).register_reference(
        args.ref_id, args.bottle, args.station, args.reading, args.error, args.day
    )


def cmd_revoke_ref(args: argparse.Namespace) -> dict:
    return _service(args).revoke_reference(args.ref_id, args.day)


def cmd_reapprove_ref(args: argparse.Namespace) -> dict:
    return _service(args).reapprove_reference(args.ref_id, args.day)


def cmd_countersign(args: argparse.Namespace) -> dict:
    return _service(args).countersign(args.day)


def cmd_revision(args: argparse.Namespace) -> dict:
    return _service(args).get_revision(args.revision)


def cmd_seam(args: argparse.Namespace) -> dict:
    return _service(args).seam_query(args.revision, args.edge_a, args.edge_b)


def cmd_plan(args: argparse.Namespace) -> dict:
    available = args.available if args.available else None
    return _service(args).plan(args.show, args.revision, available=available)


def cmd_seal_list(args: argparse.Namespace) -> dict:
    return _service(args).seal_loading_list(
        args.list_id, args.revision, args.show, args.loaded, args.day
    )


def cmd_get_list(args: argparse.Namespace) -> dict:
    return _service(args).get_list(args.list_id)


def cmd_depart(args: argparse.Namespace) -> dict:
    return _service(args).depart(args.list_id, args.day)


def cmd_onsite(args: argparse.Namespace) -> dict:
    return _service(args).onsite(args.list_id, args.present)


def cmd_handover(args: argparse.Namespace) -> dict:
    return _service(args).handover(
        args.list_id, args.show, args.arrangement, args.present, args.day
    )


def cmd_locks(args: argparse.Namespace) -> dict:
    return {"locks": _service(args).locks()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curtain",
        description="Curtain measurement reconciliation and tour service",
    )
    parser.add_argument("--db", required=True, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="load stations/edges/panels/shows document")
    p.add_argument("--document", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_setup)

    p = sub.add_parser("register-ref", help="register a bottle reference reading")
    p.add_argument("--ref-id", required=True)
    p.add_argument("--bottle", required=True)
    p.add_argument("--station", required=True)
    p.add_argument("--reading", required=True, type=int)
    p.add_argument("--error", required=True, type=int)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_register_ref)

    p = sub.add_parser("revoke-ref", help="withdraw a reference")
    p.add_argument("--ref-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_revoke_ref)

    p = sub.add_parser("reapprove-ref", help="re-approve a revoked reference")
    p.add_argument("--ref-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_reapprove_ref)

    p = sub.add_parser("countersign", help="countersign the active records")
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_countersign)

    p = sub.add_parser("revision", help="show a countersigned revision")
    p.add_argument("--revision", required=True)
    p.set_defaults(handler=cmd_revision)

    p = sub.add_parser("seam", help="signed min/max difference between two edges")
    p.add_argument("--revision", required=True)
    p.add_argument("--edge-a", required=True)
    p.add_argument("--edge-b", required=True)
    p.set_defaults(handler=cmd_seam)

    p = sub.add_parser("plan", help="minimum-swap arrangement for a show")
    p.add_argument("--show", required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--available", action="append", default=None,
                   help="restrict usable spares; repeat per spare")
    p.set_defaults(handler=cmd_plan)

    p = sub.add_parser("seal-list", help="seal a physical loading list")
    p.add_argument("--list-id", required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--show", action="append", required=True)
    p.add_argument("--loaded", action="append", default=[])
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_seal_list)

    p = sub.add_parser("get-list", help="show a sealed loading list")
    p.add_argument("--list-id", required=True)
    p.set_defaults(handler=cmd_get_list)

    p = sub.add_parser("depart", help="mark the truck departed with this list")
    p.add_argument("--list-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_depart)

    p = sub.add_parser("onsite", help="hangable options for the present spares")
    p.add_argument("--list-id", required=True)
    p.add_argument("--present", action="append", default=[])
    p.set_defaults(handler=cmd_onsite)

    p = sub.add_parser("handover", help="commit a sealed arrangement on site")
    p.add_argument("--list-id", required=True)
    p.add_argument("--show", required=True)
    p.add_argument("--arrangement", required=True,
                   help="'primary' or 'missing:<spare>'")
    p.add_argument("--present", action="append", default=[])
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_handover)

    p = sub.add_parser("locks", help="list panels locked by handovers")
    p.set_defaults(handler=cmd_locks)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except ReagentError as exc:
        _emit({"error_type": exc.error_type, "error": str(exc)})
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
