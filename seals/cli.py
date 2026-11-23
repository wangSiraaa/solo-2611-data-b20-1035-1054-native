"""CLI entry points for the seal-label / mold / freight layer."""

from __future__ import annotations

import argparse
import sys

from reagent.cli import _emit, _load_json
from reagent.errors import ReagentError
from reagent.schema import connect, initialize

from .archive import ArchiveService
from .freight import CostModel, classify_leaves, plan_dispositions, build_tree
from .labels import SealLabelService
from .schema import initialize_seals
from .workflow import WorkflowService


def _connect(args: argparse.Namespace):
    conn = connect(args.db)
    initialize(conn)
    initialize_seals(conn)
    return conn


def cmd_mold_register(args) -> dict:
    conn = _connect(args)
    result = ArchiveService(conn).register_mold(
        args.mold_id,
        args.batch,
        args.pattern,
        args.day,
        splice_adjacencies=args.splice or [],
    )
    conn.close()
    return result


def cmd_voucher_register(args) -> dict:
    conn = _connect(args)
    result = ArchiveService(conn).register_voucher(
        args.voucher_id,
        args.mold_id,
        args.shift,
        args.first_tooth,
        args.last_tooth,
        args.min_skip,
        args.max_skip,
    )
    conn.close()
    return result


def cmd_label_issue(args) -> dict:
    conn = _connect(args)
    result = SealLabelService(conn).issue_label(
        args.label_id, args.box_id, args.run_id, args.day
    )
    conn.close()
    return result


def cmd_imprint_record(args) -> dict:
    conn = _connect(args)
    payload = _load_json(args.imprint)
    result = ArchiveService(conn).record_imprint(
        args.imprint_id,
        args.label_id,
        payload["segments"],
        args.day,
        splice_marks=payload.get("splice_marks", []),
        locating_hole=payload.get("locating_hole", "present"),
        note=payload.get("note"),
    )
    conn.close()
    return result


def cmd_reconstruct(args) -> dict:
    conn = _connect(args)
    archive = ArchiveService(conn)
    if args.label_id:
        result = archive.reconstruct_label(args.label_id)
    else:
        result = archive.reconstruct_imprint(args.imprint_id, args.version)
    conn.close()
    return result


def cmd_plan(args) -> dict:
    conn = _connect(args)
    archive = ArchiveService(conn)
    document = _load_json(args.plan_doc)
    root, _by_id = build_tree(document["nodes"])
    costs = CostModel(
        sealed_return=document["costs"]["sealed_return"],
        unseal=document["costs"]["unseal"],
        leaf_charge=int(document["costs"]["leaf_charge"]),
    )
    facts = classify_leaves(
        root, archive, frozenset(document["named_batches"])
    )
    result = plan_dispositions(
        root,
        facts,
        costs,
        int(document["max_co_returned_clear"]),
        tuple(document.get("trucks", ["truck-1", "truck-2"])),
    )
    conn.close()
    return result


def cmd_workflow(args) -> dict:
    conn = _connect(args)
    workflow = WorkflowService(conn)
    if args.wf_op == "register-tree":
        result = workflow.register_tree(_load_json(args.document)["nodes"])
    elif args.wf_op == "open":
        result = workflow.open_workflow(args.workflow_id, args.day, args.mode)
    elif args.wf_op == "close":
        result = workflow.close_workflow(args.workflow_id, args.day)
    elif args.wf_op == "reopen":
        result = workflow.reopen_workflow(args.workflow_id, args.day)
    elif args.wf_op == "approve-plan":
        result = workflow.approve_plan(
            args.workflow_id, _load_json(args.document)
        )
    elif args.wf_op == "lock":
        result = workflow.lock_cabinet(args.workflow_id, args.node_id, args.day)
    elif args.wf_op == "break":
        result = workflow.break_seal(
            args.workflow_id,
            args.node_id,
            args.day,
            _load_json(args.document) if args.document else {"planned": True},
        )
    elif args.wf_op == "execute":
        result = workflow.execute_action(
            args.workflow_id, _load_json(args.document), args.day
        )
    elif args.wf_op == "recovery":
        result = workflow.recovery_state(args.workflow_id)
    elif args.wf_op == "finalize":
        result = workflow.finalize_workflow(args.workflow_id, args.day)
    else:
        raise ValueError(f"unknown workflow op {args.wf_op!r}")
    conn.close()
    return result


def build_seal_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seals", description="Seal labels, mold archive and freight planning"
    )
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("mold-register")
    p.add_argument("--mold-id", required=True)
    p.add_argument("--batch", required=True)
    p.add_argument("--pattern", required=True)
    p.add_argument("--day", required=True, type=int)
    p.add_argument("--splice", type=int, action="append")
    p.set_defaults(handler=cmd_mold_register)

    p = sub.add_parser("voucher-register")
    p.add_argument("--voucher-id", required=True)
    p.add_argument("--mold-id", required=True)
    p.add_argument("--shift", required=True)
    p.add_argument("--first-tooth", required=True, type=int)
    p.add_argument("--last-tooth", required=True, type=int)
    p.add_argument("--min-skip", required=True, type=int)
    p.add_argument("--max-skip", required=True, type=int)
    p.set_defaults(handler=cmd_voucher_register)

    p = sub.add_parser("label-issue")
    p.add_argument("--label-id", required=True)
    p.add_argument("--box-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_label_issue)

    p = sub.add_parser("imprint-record")
    p.add_argument("--imprint-id", required=True)
    p.add_argument("--label-id", required=True)
    p.add_argument("--imprint", required=True, help="JSON file with the read")
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_imprint_record)

    p = sub.add_parser("reconstruct")
    p.add_argument("--imprint-id")
    p.add_argument("--label-id")
    p.add_argument("--version", type=int)
    p.set_defaults(handler=cmd_reconstruct)

    p = sub.add_parser("plan")
    p.add_argument("--plan-doc", required=True)
    p.set_defaults(handler=cmd_plan)

    p = sub.add_parser("workflow")
    p.add_argument("--wf-op", required=True)
    p.add_argument("--workflow-id")
    p.add_argument("--node-id")
    p.add_argument("--day", type=int)
    p.add_argument("--mode", default="cabinet_first")
    p.add_argument("--document")
    p.set_defaults(handler=cmd_workflow)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_seal_parser()
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
