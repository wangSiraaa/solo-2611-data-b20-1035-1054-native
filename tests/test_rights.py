import unittest

from cs import rights


def term(node, holder, rects, attribution, protections, original="O", use="u"):
    return rights.Term.from_dict(
        {
            "node": node,
            "holder": holder,
            "original": original,
            "use": use,
            "rects": rects,
            "attribution": attribution,
            "protections": protections,
        }
    )


class GeometryTests(unittest.TestCase):
    def test_normalize_rect_orders_corners(self):
        self.assertEqual(rights.normalize_rect([2, 3, 0, 1]), (0, 1, 2, 3))

    def test_rect_cells(self):
        self.assertEqual(
            rights.rect_cells([0, 0, 0, 2]), {(0, 0), (0, 1), (0, 2)}
        )

    def test_cells_to_rects_roundtrip(self):
        cells = {(0, 0), (0, 1), (1, 0), (1, 1), (1, 2)}
        rects = rights.cells_to_rects(cells)
        recovered = set()
        for rect in rects:
            recovered |= rights.rect_cells(rect)
        self.assertEqual(recovered, cells)

    def test_mirror(self):
        self.assertEqual(rights.mirror_cell((0, 0), 4), (0, 3))
        self.assertEqual(rights.mirror_rect((0, 0, 0, 1), 4), (0, 2, 0, 3))


class ReductionTests(unittest.TestCase):
    def test_specific_node_drops_strict_ancestor(self):
        edges = [("X", "B"), ("B", "R"), ("X", "R")]
        kept, dropped = rights.reduce_applicable(["R", "B"], edges)
        self.assertEqual(kept, ["B"])
        self.assertEqual(dropped, ["R"])

    def test_parallel_nodes_all_kept(self):
        edges = [("L", "A"), ("L", "B"), ("A", "R"), ("B", "R")]
        kept, dropped = rights.reduce_applicable(["A", "B"], edges)
        self.assertEqual(kept, ["A", "B"])
        self.assertEqual(dropped, [])


class ClearanceTests(unittest.TestCase):
    def test_specificity_picks_deeper_licence(self):
        # X -> B -> R and X -> R; B licenses 0..3, R licenses 0..2.
        edges = [("X", "B"), ("B", "R"), ("X", "R")]
        terms = [
            term("R", "H", [[0, 0, 0, 2]], ["c"], []),
            term("B", "H", [[0, 0, 0, 3]], ["c"], []),
        ]
        c = rights.clear(
            ticket_id="T", direct_class="X", original="O", use="u",
            edges=edges, terms=terms, holders=["H"],
        )
        self.assertEqual(c.status, "cleared")
        self.assertEqual(c.holders[0].kept_nodes, ["B"])
        self.assertEqual(c.holders[0].dropped_ancestors, ["R"])
        self.assertEqual(sorted(c.region), [(0, 0), (0, 1), (0, 2), (0, 3)])

    def test_after_edge_deletion_licence_shrinks(self):
        terms = [
            term("R", "H", [[0, 0, 0, 2]], ["c"], []),
            term("B", "H", [[0, 0, 0, 3]], ["c"], []),
        ]
        edges = [("X", "B"), ("B", "R"), ("X", "R")]
        before = rights.clear(
            ticket_id="T", direct_class="X", original="O", use="u",
            edges=edges, terms=terms, holders=["H"],
        )
        edges2 = [e for e in edges if e != ("X", "B")]
        after = rights.clear(
            ticket_id="T", direct_class="X", original="O", use="u",
            edges=edges2, terms=terms, holders=["H"],
        )
        self.assertEqual(sorted(before.region), [(0, 0), (0, 1), (0, 2), (0, 3)])
        self.assertEqual(sorted(after.region), [(0, 0), (0, 1), (0, 2)])

    def test_parallel_nodes_union_region(self):
        edges = [("L", "A"), ("L", "B"), ("A", "R"), ("B", "R")]
        terms = [
            term("A", "H", [[0, 0, 0, 0]], ["top"], []),
            term("B", "H", [[1, 0, 1, 1]], ["bottom"], []),
        ]
        c = rights.clear(
            ticket_id="T", direct_class="L", original="O", use="u",
            edges=edges, terms=terms, holders=["H"],
        )
        self.assertEqual(c.holders[0].kept_nodes, ["A", "B"])
        self.assertEqual(
            sorted(c.region), [(0, 0), (1, 0), (1, 1)]
        )
        # attribution from both parallel nodes, de-duplicated
        self.assertEqual(c.attribution, ["top", "bottom"])

    def test_duplicate_attribution_not_repeated(self):
        edges = [("L", "A"), ("L", "B"), ("A", "R"), ("B", "R")]
        terms = [
            term("A", "H", [[0, 0, 0, 0]], ["same", "a"], []),
            term("B", "H", [[0, 0, 0, 0]], ["same", "b"], []),
        ]
        c = rights.clear(
            ticket_id="T", direct_class="L", original="O", use="u",
            edges=edges, terms=terms, holders=["H"],
        )
        self.assertEqual(c.attribution, ["same", "a", "b"])

    def test_cross_holder_region_intersection(self):
        # H1 licenses 0..3 through B, H2 licenses 0..2 through R.
        edges = [("X", "B"), ("B", "R"), ("X", "R")]
        terms = [
            term("R", "H1", [[0, 0, 0, 2]], ["a"], [[(0, 0), (0, 1)]]),
            term("B", "H1", [[0, 0, 0, 3]], ["a"], [[(0, 0), (0, 1)]]),
            term("R", "H2", [[0, 0, 0, 2]], ["b"], [[(0, 1), (0, 2)]]),
        ]
        c = rights.clear(
            ticket_id="T", direct_class="X", original="O", use="u",
            edges=edges, terms=terms, holders=["H1", "H2"],
        )
        self.assertEqual(sorted(c.region), [(0, 0), (0, 1), (0, 2)])
        self.assertEqual(c.attribution, ["a", "b"])
        # protection requirements from both holders are kept
        self.assertEqual(
            [list(g) for g in c.protections],
            [[(0, 0), (0, 1)], [(0, 1), (0, 2)]],
        )

    def test_missing_holder_holds(self):
        edges = [("X", "R")]
        terms = [term("R", "H1", [[0, 0, 0, 0]], ["a"], [])]
        c = rights.clear(
            ticket_id="T", direct_class="X", original="O", use="u",
            edges=edges, terms=terms, holders=["H1", "H2"],
        )
        self.assertEqual(c.status, "held")
        self.assertIn("H2", c.held_holders)

    def test_wrong_use_is_ignored(self):
        edges = [("X", "R")]
        terms = [term("R", "H", [[0, 0, 0, 0]], ["a"], [], use="other")]
        c = rights.clear(
            ticket_id="T", direct_class="X", original="O", use="u",
            edges=edges, terms=terms, holders=["H"],
        )
        self.assertEqual(c.status, "held")


if __name__ == "__main__":
    unittest.main()
