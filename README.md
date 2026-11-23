# classification-summary/1 — catalog + fact summary store

An independent catalog DAG and a fact summary library, with a single atomic
publication cut. Python 3.11, standard library only, SQLite storage.

```
cs/
  catalog.py   pure DAG algorithms: cycle check, ancestors, descendants,
               shortest witness paths, coverage explanation
  store.py     SQLite store: revisions, atomic bundles, cut-pinned queries,
               immutable products
  cli.py       command line entry point (`python -m cs`)
glass/
  geom.py      clockwise-safe bench->install rotation of sides/corners/axes
  fit.py       lap/pane sizing from survey readings (jointly feasible sizes)
  cutting.py   resumable guillotine cutting over physical stock + handoff
  store.py     glazing store: groove resolution, revisions, signings, plans
  cli.py       command line entry point (`python -m glass`)
tests/         unit tests (python -m unittest discover -s tests)
demo.py        runs materials/721 end to end and feeds consumer.py
showcase.py    walks through every required scenario on the diamond
glass_showcase.py  walks through the glazing scenarios (bead swap, baffle,
               defect, handoff, pinwheel)
```

## Quick start

```bash
# Initialize from the supplied catalog and facts
python3 -m cs --db demo.db init materials/721/catalog.json \
    --facts materials/721/facts.jsonl

# One multi-target query pinned to one cut
python3 -m cs --db demo.db query A B

# Apply the operations JSONL (B1 accepted, B_BAD rejected)
python3 -m cs --db demo.db run materials/721/operations.jsonl --out ops.json

# Query all named requests from queries.json against a single cut and export
mkdir -p out
python3 -m cs --db demo.db query --queries materials/721/queries.json --export out
python3 materials/721/consumer.py out/overlap.json
```

`python3 demo.py` writes `demo/out/before.json` and `demo/out/after.json` and
runs the provided read-only `consumer.py` against both. `python3 showcase.py`
prints the diamond, multi-label-same-parent, parent-and-child, same-name,
withdraw/reopen, cycle rejection and recovery scenarios.

## Catalog model

* Finite directed acyclic graph. An edge is `[child, parent]`; a node may have
  several parents, so the diamond `L -> A -> R`, `L -> B -> R` is legal.
* Identity is the stable node `id`. Names are display-only and may repeat
  (`A` and `B` are both named `办件` in the sample).
* A batch of edge additions/removals is judged **only on the final edge set**.
  A cycle in the final set rejects the entire batch; operation order inside
  the batch never causes a false rejection.
* The target node is always in its own coverage (path `[target]`).
* `ancestors` / `descendants` follow child→parent / reverse edges. Coverage
  explanation reports, for each direct class, whether it can reach the target
  and one deterministic shortest path. Multiple paths or parents are not
  multiplied; only a boolean and one witness path are produced.

## Fact model

A fact revision carries `ticket_id`, `revision` (integer ≥ 1), `amount`
(non-negative integer), and a non-empty set of direct class ids.

* A ticket contributes **once** to a target if any of its direct classes can
  reach that target. Several direct classes and several paths do not multiply
  the count.
* Each target reports `count`, `amount`, its contribution list, and at least
  one hit path per contribution. Zero-amount tickets still count.
* A multi-target query may include a parent and a child. Each target reports
  its own contributions; the group `union` merges **by ticket id**, counting a
  ticket once even if it matched several targets.
* Retrying the *current* revision with identical content is accepted as a
  no-op. Retrying it with different content is rejected. A new revision number
  must be strictly greater than the current one. A withdrawn ticket needs a
  higher revision to re-open.

## Consistent publication cut

Only committed state is visible.

* **Bundle** = a batch of catalog edge changes plus a batch of fact revisions.
  Validation runs against the *projected* post-bundle catalog and fact state.
  A cycle, an unknown/illegal class, or a revision conflict aborts the whole
  bundle **before any row is written**. The validation and every write happen
  inside one `BEGIN IMMEDIATE` transaction, so a rejected bundle leaves no
  trace and a reader can only see the state before or after it, never half.
* A **cut** records `catalog_revision`, `fact_revision`, and an internal
  `fact_watermark`. Fact revisions are named `F<n>`; the catalog revision is
  `G<n>` (or the initial file revision, e.g. `G1`).
* A **query** opens one `BEGIN DEFERRED` read transaction. Its snapshot is
  fixed on the first read, so every target, the union, and any export are
  resolved from the same catalog revision and the same fact watermark. Results
  are never stitched from different points in time. A query can also be
  re-run against an explicit `Cut` to read a historical snapshot
  (`_load_facts_at_seq` replays committed facts up to that watermark).
* **Products** store their cut and a full copy of the payload. Publishing
  twice with the same product id is rejected if the content differs; later
  publications create new products and never rewrite old files. The supplied
  `consumer.py` reads a product file directly.

## Revision watermarks

`catalog_revision` is a monotonic counter over committed catalog snapshots
(`G1`, `G2`, ...). `fact_revision` is a monotonic counter over fact commits
(`F1`, `F2`, ...). `fact_watermark` is the event sequence used to replay a
historical fact state. All three are stable, real identifiers of a committed
cut; two different cuts never share the same triple.

## Edge deletion and publication cost

* Each catalog change writes a full snapshot of the projected edge set into
  `catalog_commits` (`edges` JSON) and updates the live `catalog_edges` table.
  The snapshot is what makes historical cuts reproducible: deleting an edge
  never destroys the information needed to re-evaluate an older cut.
  At the stated scale (≤ 100 nodes, ≤ 300 edges) the snapshot is tiny; the cost
  is a linear rewrite of the edge set per committed catalog revision.
* Queries do **not** maintain a transitive closure. Reachability is a reverse
  BFS from each target over the current (or snapshot) edge set, which is O(V+E)
  per target and always reflects the committed edges — there is no stale
  closure to invalidate. For a large number of targets sharing an edge set,
  the per-target BFS is repeated; at this scale that is well within budget
  (tens of targets over 1000 tickets complete in milliseconds).
* Fact updates touch only the affected ticket rows plus append-only
  `fact_commits`. Withdrawal is an `active=0` flag update, so old fact commits
  remain available for historical replay.
* A bundle that would not change any state (no edge delta and no fact advance)
  is recorded as a no-op and creates no new revision.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers cycle detection on the final edge set, multi-parent and
multi-label counting, same-name distinct ids, cut-pinned re-reads, immutable
products, revision rules, withdraw/reopen, atomic bundle rejection (including
catalog rollback when a fact revision conflicts), and a concurrent reader
that must observe only pre- or post-bundle state.

---

# glazing workshop (`glass/`)

A frame-and-glass workshop built on the same catalog/revision machinery.  The
frame grooves are catalog nodes, a survey's *direct classes* are the grooves it
was filed under, and `cs`'s commit/cut/revision rules are reused unchanged.

## Slots, surveys and resolution

A *slot* (具名框槽) is a named groove with a colour and, for grain-bearing
stock, a requirement that grain be recorded on the survey.  A *survey* is a
revisioned, withdrawable ticket measured on the bench.  Its `amount` column is
the whole-cell reading; the per-piece measurements are separate records.

```
slot      groove id           install_position  which pane in the slot
item      opening | min_lap | max_lap | lap | corner | grain
target    side (top/right/bottom/left) or corner (TR/BR/BL/TL)
revision  signing round (>= 1); amount = whole-cell reading
rotation  quarter turns the frame had been turned clockwise on the bench
```

The bench orientation is undone before storage: sides, corners, grain axis and
opening width/height are rotated back to the installation front (a quarter turn
swaps width/height and the grain axis; a 180-degree turn keeps the grain axis,
since an arrow reversal is allowed).

A survey is filed to a groove the same way a `cs` ticket is counted: walk the
committed catalog upward from its direct classes.  The ticket resolves to that
groove once, no matter how many direct classes or paths reach it.  Zero
reachable grooves, or more than one, **cannot be cut**.

The *highest active signing round is chosen per (slot, position, item,
target)* — not per slip — so a newer slip that re-measured one side leaves the
other readings in force.  Two different readings at the same round mean the
slip must be re-taken.  Withdrawal marks a slip inactive and re-exposes the
previous round of every reading it carried, and the previous signed slip.

## Sizing and fit

For a position with opening `ow x oh`, choose integer laps `L,R,T,B` inside
their `[min_lap, max_lap]` ranges:

```
width  = ow + L + R
height = oh + T + B
```

Every corner with a 45-degree baffle reading `c` requires the sum of its two
adjacent laps `<= c`.  Because the corners couple horizontal and vertical laps,
the individually largest width and the individually largest height need not be
achievable on the same pane.  `PositionRequirement.maximal_sizes()` returns only
size pairs that are jointly feasible and Pareto-maximal; `explain(w,h)` states
how a size laps in, or exactly which lap sum or corner baffle refuses it.

`sign` records the admissible sizes, grain axis and colour at one cut.  A
signed slip is immutable and cut-pinned: a later bead change never rewrites an
old slip, and `fetch signing <id>` returns it unchanged from any database copy.

## Cutting and handoff

A *piece* is a physical rectangle with a physical grain axis and any defects; a
*cut* is one straight line from edge to edge (no kerf).  Trimmed scrap costs a
cut.  A pane must be one piece — never joined, never containing a defect.  Grain
is physical: a pane is installed with a 0 or 1 quarter turn, and an odd turn
swaps its grain axis on the front.

`plan_cuts` assigns each signing one of its legal sizes and emits a guillotine
cut order, least waste first, refining by fewest cuts when asked.  Because panes
are peeled out of whole physical pieces, the order is always executable.

If the previous shift already cut the sheet, register the stock **as it now
lies** (each remnant separately, with fracture coordinates) and the remaining
cut budget.  `resume(plan, batch, delivered=[...])` plans only the slots still
owed, from those pieces.  Two halves that were cut apart are never re-joined,
even when the untouched sheet could have yielded the pane; a same-area remnant
of the wrong shape is refused.

`guillotine_order(pieces, targets)` checks whether a full placement layout has
any straight-cut order at all.  Some layouts do not (a cyclic pinwheel), and it
returns `None` for those.

## Glazing CLI

```bash
python3 -m glass --db w.db slot LEFT --color gray --grained
python3 -m glass --db w.db survey q1 --ticket TL --revision 1 \
    --class LEFT --measurements q1.json
python3 -m glass --db w.db resolve q1
python3 -m glass --db w.db sign SLIP-L --slot LEFT --position pane
python3 -m glass --db w.db explain SLIP-L 1 2
python3 -m glass --db w.db stock B1 --remaining 1 --pieces stock.json
python3 -m glass --db w.db plan P1 --batch B1 --signing SLIP-L --signing SLIP-R
python3 -m glass --db w.db fetch signing SLIP-L   # works against any DB copy
```

`python3 glass_showcase.py` reproduces the brief's scenarios: the two
equal-looking panes and the one-bead swap that needs two slips in one commit,
the corner baffle that rejects 2×2 while accepting 1×2 and 2×1, withdrawal
restoring the previous slip, defects and multiple legal sizes, a resumable
cut order, and the pinwheel that admits no straight-cut order.
