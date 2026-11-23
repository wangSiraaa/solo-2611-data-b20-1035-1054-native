# exhibition-rights/1 — offline dataset

A single JSON file (`dataset.json`) is the whole world for the demonstration.
No network, no external services. It covers ≤4 branches, ≤8 labels per scenario
and ≤4 compositions per label (`caps` in the file records these limits).

## Shape

```
catalog      classification-catalog/1 DAG (edges are [child, parent])
originals    named images with a rows×cols grid of cells (像格)
tickets      an image under application: original + direct_class + holders
terms        per node/holder/original/use: rects, attribution, protections
scenarios    each has a ticket, initial_public cells, and labels
labels       application_no, branch, panel, signature length, reserved cells,
             ordered compositions (curator preference) of crop blocks
```

A **term** licenses rectangles of one original, requires attribution strings,
and lists **protection groups** — cell groups that must never become fully
public. A **composition** prints one crop onto panel cells, optionally flipped,
with the attribution run contiguously along one block edge.

## Scenarios

| id | what it shows |
|----|----------------|
| `A-worked` | the worked example: cell 2 would complete a forbidden group, so only cell 3 ships |
| `C-emblem` | moving the only signature cell to a fixed emblem makes both compositions infeasible |
| `D-joint` | empty public record; two isolated, individually legal orders jointly complete a forbidden group |
| `I-preference` | whole batch must ship; application-number lexicographic preference is honoured |
| `H-identity` | a flip and another branch do not create a new original; panel cells map back to the same original coordinates |
| `M-intersection` | two holders' licensed regions intersect (H1 via B 0–3, H2 via R 0–2 → 0–2) |
| `U-union` | one holder's parallel nodes (A, B) union their regions |

Extra checks in `rights_demo.py` cover deleting `X→B` (which removes cell 3 from
the licence) and a held ticket where a registered holder has no applicable term.

## Commands

```bash
python3 rights_demo.py                                   # full walkthrough
python3 -m cs license materials/rights/dataset.json T-Z  # clearance for a ticket
python3 -m cs license materials/rights/dataset.json T-X --delete-edge X B
python3 -m cs check  materials/rights/dataset.json C-emblem
python3 -m cs solve  materials/rights/dataset.json A-worked --ascii
```

## Delivery models

The same dataset drives both, in `rights_demo.py`:

* **Central** — the archive bakes crop + signature into a final master. A
  *delivery receipt* grows the public record; a *retained sample* is recorded
  but never becomes public.
* **Branch** — each branch gets a *plate* (still carrying original-cell
  identity) and a *voucher*. The branch lays out the signature and must redeem
  the voucher before release. Redemption is checked against the whole confirmed
  shipment, so two branches holding partial slices cannot jointly complete a
  forbidden group, and a cancelled/undetermined voucher is never treated as a
  definite release.

Every shipped label's public-record entry stores the composition, crop
(original cell identity), signature cells, and the cumulative-protection
verdict, so an audit can trace a label through crop, signature and accumulated
public scope.
