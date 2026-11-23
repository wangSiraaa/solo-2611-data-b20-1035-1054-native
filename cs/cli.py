#!/usr/bin/env python3
"""Command line interface for the classification catalog and fact store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .store import (
    CatalogError,
    ConflictError,
    FactError,
    ProductError,
    Store,
    StoreError,
)


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return out


def _open(args: argparse.Namespace) -> Store:
    return Store.open(args.db)


def cmd_init(args: argparse.Namespace) -> int:
    data = _read_json(args.catalog)
    with _open(args) as store:
        state = store.init_catalog(data, request_id=args.request_id)
        # Optionally seed facts from a JSONL file in one atomic commit.
        if args.facts:
            rows = _read_jsonl(args.facts)
            facts = [
                {
                    "ticket_id": r["ticket_id"],
                    "revision": int(r["revision"]),
                    "amount": int(r["amount"]),
                    "classes": list(r.get("classes", [])),
                }
                for r in rows
            ]
            committed = store.apply_fact_revisions(facts, request_id=args.fact_request_id)
            _dump(
                {
                    "catalog_revision": state.revision,
                    "facts_applied": len(committed.facts),
                    "cut": committed.cut.as_dict(),
                }
            )
        else:
            _dump({"catalog_revision": state.revision, "cut": store.current_cut().as_dict()})
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    operation = _read_json(args.operation)
    with _open(args) as store:
        committed = store.submit(operation)
        _dump(committed.as_dict())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Apply an operations JSONL file in order, stopping at the first reject."""

    results = []
    with _open(args) as store:
        for index, operation in enumerate(_read_jsonl(args.operations), 1):
            request_id = operation.get("request_id")
            try:
                committed = store.submit(operation)
                results.append(
                    {
                        "line": index,
                        "request_id": request_id,
                        "accepted": True,
                        "result": committed.as_dict(),
                    }
                )
            except StoreError as exc:
                results.append(
                    {
                        "line": index,
                        "request_id": request_id,
                        "accepted": False,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                if args.stop_on_error:
                    break
        record = {
            "operations": results,
            "cut": store.current_cut().as_dict(),
        }
        if args.out:
            Path(args.out).write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        _dump(record)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    targets = args.targets
    if args.queries:
        data = _read_json(args.queries)
        requests = data.get("requests", data if isinstance(data, list) else [])
        all_results = []
        with _open(args) as store:
            # One query run = one committed cut.  All named requests in this
            # file (and any export) are pinned to the same cut, so results are
            # never stitched together from different points in time.
            pinned = store.current_cut()
            for request in requests:
                result = store.query(
                    request["targets"], name=request.get("name"), snapshot=pinned
                )
                if args.export:
                    payload = {
                        "format": "classification-summary/1",
                        "cut": result["cut"],
                        "groups": result["groups"],
                        "union": result["union"],
                    }
                    if request.get("name") is not None:
                        payload["name"] = request["name"]
                    out_path = Path(args.export) / f"{request.get('name') or 'query'}.json"
                    out_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n",
                        encoding="utf-8",
                    )
                all_results.append(result)
        _dump({"results": all_results})
        return 0
    with _open(args) as store:
        result = store.query(targets, name=args.name)
        if args.export:
            payload = {
                "format": "classification-summary/1",
                "cut": result["cut"],
                "groups": result["groups"],
                "union": result["union"],
            }
            if args.name is not None:
                payload["name"] = args.name
            Path(args.export).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        _dump(result)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    with _open(args) as store:
        result = store.publish(
            args.targets, name=args.name, product_id=args.product_id
        )
        out = Path(args.out) if args.out else None
        if out:
            out.write_text(
                json.dumps(result["payload"], ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        _dump(
            {
                "product_id": result["product_id"],
                "created_at": result["created_at"],
                "cut": result["payload"]["cut"],
                "out": str(out) if out else None,
            }
        )
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    classes = args.classes
    with _open(args) as store:
        result = store.explain_target(args.target, classes)
        _dump(result)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.product:
            _dump(store.get_product(args.product))
            return 0
        _dump(
            {
                "cut": store.current_cut().as_dict(),
                "catalog": store.load_catalog_state().as_json(),
                "ancestors": {
                    nid: store.ancestors(nid)
                    for nid in sorted(store.load_catalog_state().nodes)
                },
                "descendants": {
                    nid: store.descendants(nid)
                    for nid in sorted(store.load_catalog_state().nodes)
                },
                "products": store.list_products(),
            }
        )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    with _open(args) as store:
        _dump({"history": store.history(), "cut": store.current_cut().as_dict()})
    return 0


# ------------------------------------------------------ exhibition rights ----


def _load_dataset(args: argparse.Namespace):
    from . import dataset

    return dataset.load(args.dataset)


def cmd_license(args: argparse.Namespace) -> int:
    ds = _load_dataset(args)
    edges = ds.edges
    if args.delete_edge:
        child, parent = args.delete_edge
        edges = [e for e in edges if e != (child, parent)]
    clearance = ds.license_for(args.ticket, edges=edges)
    _dump(clearance.as_dict())
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from . import dataset as dsmod

    ds = _load_dataset(args)
    scen = ds.scenario(args.scenario)
    pub = dsmod.initial_public(ds, scen)
    labels_out = []
    for label in ds.label_objects(scen):
        labels_out.append(
            {
                "label": label.id,
                "compositions": [
                    dsmod.check_composition(ds, label, comp, pub).as_dict()
                    for comp in label.compositions
                ],
            }
        )
    _dump({"scenario": args.scenario, "labels": labels_out})
    return 0


def cmd_solve(args: argparse.Namespace) -> int:
    from . import dataset as dsmod
    from . import exhibit

    ds = _load_dataset(args)
    scen = ds.scenario(args.scenario)
    res = dsmod.batch(ds, scen)
    out = {
        "scenario": args.scenario,
        "status": res.status,
        "order": res.order,
        "solved": [s.as_dict() for s in res.solved],
        "diagnostics": res.diagnostics,
        "public_after": {o: [list(c) for c in cells] for o, cells in res.public_after.items()},
    }
    if args.ascii and res.delivered:
        for item in res.solved:
            out.setdefault("ascii", []).append(
                {
                    "label": item.label_id,
                    "art": exhibit.render_check(
                        item.composition, item.check,
                        panel=_label_panel(ds, scen, item.label_id),
                        reserved=_label_reserved(ds, scen, item.label_id),
                        originals=ds.originals,
                    ),
                }
            )
    _dump(out)
    return 0


def _raw_label(scen: dict[str, Any], label_id: str) -> dict[str, Any]:
    return next(l for l in scen["labels"] if l["id"] == label_id)


def _label_panel(ds, scen, label_id):
    raw = _raw_label(scen, label_id)
    return (int(raw["panel"][0]), int(raw["panel"][1]))


def _label_reserved(ds, scen, label_id):
    return {tuple(c) for c in _raw_label(scen, label_id).get("reserved", [])}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cs",
        description="Classification catalog, fact store, cut-pinned summaries.",
    )
    parser.add_argument("--db", default="classification.db", help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialize catalog and optional facts")
    p.add_argument("catalog", help="classification-catalog/1 JSON file")
    p.add_argument("--facts", help="facts JSONL file")
    p.add_argument("--request-id", default="init-catalog")
    p.add_argument("--fact-request-id", default="init-facts")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("submit", help="submit one operation JSON file")
    p.add_argument("operation")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("run", help="apply an operations JSONL file in order")
    p.add_argument("operations")
    p.add_argument("--out", help="write the full result JSON here")
    p.add_argument(
        "--stop-on-error",
        action="store_true",
        help="stop at the first rejected operation",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("query", help="run a multi-target query at one cut")
    p.add_argument("targets", nargs="*", help="target node ids")
    p.add_argument("--queries", help="queries.json with named requests")
    p.add_argument("--name", help="name for a single query")
    p.add_argument("--export", help="write classification-summary/1 JSON to a file or directory")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("publish", help="query and persist an immutable product")
    p.add_argument("targets", nargs="+")
    p.add_argument("--name")
    p.add_argument("--product-id")
    p.add_argument("--out", help="write the product payload to this file")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("explain", help="explain coverage for direct classes")
    p.add_argument("target")
    p.add_argument("classes", nargs="+")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("show", help="show current cut, catalog, products")
    p.add_argument("--product", help="show one stored product")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("history", help="show committed events")
    p.set_defaults(func=cmd_history)

    # -- exhibition rights ------------------------------------------------
    p = sub.add_parser("license", help="clear rights for one ticket at the dataset cut")
    p.add_argument("dataset", help="exhibition-rights dataset JSON")
    p.add_argument("ticket", help="ticket id")
    p.add_argument(
        "--delete-edge", nargs=2, metavar=("CHILD", "PARENT"),
        help="evaluate against a catalog with this edge removed",
    )
    p.set_defaults(func=cmd_license)

    p = sub.add_parser("check", help="check every composition of a scenario")
    p.add_argument("dataset")
    p.add_argument("scenario")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("solve", help="solve a scenario's batch preferences")
    p.add_argument("dataset")
    p.add_argument("scenario")
    p.add_argument("--ascii", action="store_true", help="include panel art for shipped labels")
    p.set_defaults(func=cmd_solve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (StoreError, CatalogError, FactError, ConflictError, ProductError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
