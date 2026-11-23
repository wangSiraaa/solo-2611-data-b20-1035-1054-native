import unittest

from cs import exhibit


O = exhibit.Original("O", "orig", 1, 4)


def comp(cid, rect, flip=False, at=(0, 0)):
    return exhibit.Composition(
        cid, (exhibit.Block("O", exhibit.normalize_rect(rect), flip, at),)
    )


PROT = {"O": [{(0, 0), (0, 1), (0, 2)}]}


def check(c, *, panel=(1, 2), reserved=(), sig=1, region=None, public=None):
    return exhibit.check_composition(
        c,
        panel=panel,
        reserved=reserved,
        signature_lengths=[sig] * len(c.blocks),
        license_region=region if region is not None else {(0, 0), (0, 1), (0, 2), (0, 3)},
        public_cells=public or {},
        protections=PROT,
        original_ids=["O"],
    )


class SignatureTests(unittest.TestCase):
    def test_signature_must_fit_panel(self):
        # 1x2 panel, block occupies the whole panel width -> no free edge cell.
        c = comp("c", (0, 0, 0, 1))
        result = exhibit.place_signatures(c, [2], (1, 2))
        self.assertFalse(result.ok)

    def test_signature_along_edge_fits(self):
        c = comp("c", (0, 0, 0, 0))  # single cell at (0,0)
        result = exhibit.place_signatures(c, [1], (2, 2))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.placements), 1)

    def test_signature_cannot_cover_picture(self):
        c = comp("c", (0, 0, 0, 1))  # fills the 1x2 panel
        result = exhibit.place_signatures(c, [1], (1, 2))
        self.assertFalse(result.ok)

    def test_reserved_cell_blocks_signature(self):
        # only free cell is the reserved emblem -> cannot sign
        c = comp("c", (0, 0, 0, 0))
        result = exhibit.place_signatures(c, [1], (1, 2), reserved={(0, 1)})
        self.assertFalse(result.ok)


class CompositionTests(unittest.TestCase):
    def test_protection_completion_rejected(self):
        # public has 0,1; printing 2 completes {0,1,2}
        ch = check(comp("c", (0, 2, 0, 2)), public={"O": {(0, 0), (0, 1)}})
        self.assertFalse(ch.feasible)
        self.assertTrue(any("protection" in r for r in ch.reasons))

    def test_non_completing_crop_ok(self):
        ch = check(comp("c", (0, 3, 0, 3)), public={"O": {(0, 0), (0, 1)}})
        self.assertTrue(ch.feasible)

    def test_unlicensed_crop_rejected(self):
        ch = check(comp("c", (0, 3, 0, 3)), region={(0, 0), (0, 1), (0, 2)})
        self.assertFalse(ch.feasible)
        self.assertTrue(any(r.startswith("unlicensed") for r in ch.reasons))

    def test_flip_maps_to_same_original_cells(self):
        c = comp("c", (0, 2, 0, 3), flip=True)
        ch = check(c, panel=(1, 2))
        # panel (0,0) should now map to original (0,3) because of the flip
        self.assertEqual(ch.cell_map[(0, 0)], (0, 3))
        self.assertEqual(ch.cell_map[(0, 1)], (0, 2))

    def test_no_flip_maps_directly(self):
        c = comp("c", (0, 2, 0, 3))
        ch = check(c, panel=(1, 2))
        self.assertEqual(ch.cell_map[(0, 0)], (0, 2))
        self.assertEqual(ch.cell_map[(0, 1)], (0, 3))


class BatchTests(unittest.TestCase):
    def label(self, lid, app, comps, sig=1, panel=(1, 2), region=None):
        return exhibit.Label(
            id=lid,
            application_no=app,
            original="O",
            compositions=comps,
            signature_length=sig,
            license_region=region or {(0, 0), (0, 1), (0, 2), (0, 3)},
            panel=panel,
        )

    def test_worked_example_prefers_3(self):
        labels = [
            self.label(
                "L",
                1,
                [comp("c2", (0, 2, 0, 2)), comp("c3", (0, 3, 0, 3))],
            )
        ]
        res = exhibit.solve_batch(
            labels,
            initial_public={"O": {(0, 0), (0, 1)}},
            protections=PROT,
            original_ids=["O"],
        )
        self.assertEqual(res.status, "delivered")
        self.assertEqual(res.solved[0].composition.id, "c3")

    def test_batch_must_be_all_or_nothing(self):
        # first label can be satisfied, second cannot -> whole batch blocked
        labels = [
            self.label("L1", 1, [comp("c3", (0, 3, 0, 3))]),
            self.label("L2", 2, [comp("c2", (0, 2, 0, 2))], panel=(1, 1)),
        ]
        res = exhibit.solve_batch(
            labels,
            initial_public={"O": {(0, 0), (0, 1)}},
            protections=PROT,
            original_ids=["O"],
        )
        self.assertEqual(res.status, "blocked")
        self.assertEqual(res.diagnostics[0]["blocking_label"], "L2")

    def test_lexicographic_preference_respected(self):
        # two labels both prefer cell 2 but only one may take it.
        labels = [
            self.label(
                "A", 1, [comp("a2", (0, 2, 0, 2)), comp("a3", (0, 3, 0, 3))]
            ),
            self.label(
                "B", 2, [comp("b2", (0, 2, 0, 2)), comp("b3", (0, 3, 0, 3))]
            ),
        ]
        res = exhibit.solve_batch(
            labels,
            initial_public={"O": {(0, 0), (0, 1)}},
            protections=PROT,
            original_ids=["O"],
        )
        # cell 2 completes the group, so neither may take it; both take 3
        self.assertEqual(res.status, "delivered")
        self.assertEqual([s.composition.id for s in res.solved], ["a3", "b3"])


if __name__ == "__main__":
    unittest.main()
