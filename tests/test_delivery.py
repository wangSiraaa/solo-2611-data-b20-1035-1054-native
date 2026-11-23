import json
import os
import tempfile
import unittest

from cs import delivery, exhibit


O = exhibit.Original("O", "orig", 1, 4)
PROT = {"O": [{(0, 0), (0, 1), (0, 2)}]}


def comp(cid, cell):
    return exhibit.Composition(
        cid, (exhibit.Block("O", (0, cell, 0, cell), False, (0, 0)),)
    )


def feasible_check(cid="c"):
    return exhibit.CompositionCheck(composition_id=cid, feasible=True)


class PublicRecordTests(unittest.TestCase):
    def test_show_extends_public_and_is_irreversible(self):
        record = delivery.PublicRecord()
        c = comp("c", 2)
        record.show(label_id="L", composition=c, check=feasible_check())
        self.assertTrue(record.is_public("O", (0, 2)))
        record.withdraw(label_id="L")
        # withdrawing never un-shows cells
        self.assertTrue(record.is_public("O", (0, 2)))

    def test_nonpublic_record_does_not_extend_public(self):
        record = delivery.PublicRecord()
        record.record_nonpublic(label_id="L", kind="misprint", detail="bad print")
        self.assertEqual(record.cells, {})
        record.record_nonpublic(label_id="L", kind="retained", detail="archive")
        self.assertEqual(record.cells, {})

    def test_show_rejects_infeasible(self):
        record = delivery.PublicRecord()
        bad = exhibit.CompositionCheck("c", feasible=False, reasons=["x"])
        with self.assertRaises(delivery.DeliveryError):
            record.show(label_id="L", composition=comp("c", 0), check=bad)


class VoucherTests(unittest.TestCase):
    def test_isolated_redemptions_legal_joint_illegal(self):
        for cells in ({(0, 0), (0, 1)}, {(0, 2)}):
            record = delivery.PublicRecord()
            book = delivery.VoucherBook()
            book.issue(voucher_id="v", label_id="L", branch="b",
                       original="O", cells=cells)
            delivery.redeem_batch(book, ["v"], record=record, protections=PROT)
            self.assertTrue(record.cells["O"])

        record = delivery.PublicRecord()
        book = delivery.VoucherBook()
        book.issue(voucher_id="a", label_id="A", branch="b1",
                   original="O", cells={(0, 0), (0, 1)})
        book.issue(voucher_id="b", label_id="B", branch="b2",
                   original="O", cells={(0, 2)})
        with self.assertRaises(delivery.DeliveryError):
            delivery.redeem_batch(book, ["a", "b"], record=record, protections=PROT)
        self.assertEqual(record.cells, {})
        self.assertEqual([v.status for v in book.vouchers.values()], ["issued", "issued"])

    def test_cancelled_voucher_cannot_redeem(self):
        book = delivery.VoucherBook()
        book.issue(voucher_id="v", label_id="L", branch="b",
                   original="O", cells={(0, 0)})
        book.cancel("v")
        record = delivery.PublicRecord()
        with self.assertRaises(delivery.DeliveryError):
            delivery.redeem_batch(book, ["v"], record=record, protections=PROT)

    def test_unknown_voucher_rejected(self):
        book = delivery.VoucherBook()
        record = delivery.PublicRecord()
        with self.assertRaises(delivery.DeliveryError):
            delivery.redeem_batch(book, ["nope"], record=record, protections=PROT)


class PersistentStoreTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_survives_reopen(self):
        with delivery.DeliveryStore.open(self.path) as store:
            store.show(label_id="L", composition=comp("c", 3), check=feasible_check())
            store.issue(voucher_id="v", label_id="L2", branch="b",
                        original="O", cells={(0, 3)})
        with delivery.DeliveryStore.open(self.path) as store:
            self.assertTrue(store.record.is_public("O", (0, 3)))
            self.assertIn("v", store.book.vouchers)
            self.assertEqual(store.book.vouchers["v"].status, "issued")

    def test_persistent_joint_guard(self):
        with delivery.DeliveryStore.open(self.path) as store:
            store.issue(voucher_id="a", label_id="A", branch="b1",
                        original="O", cells={(0, 0), (0, 1)})
            store.issue(voucher_id="b", label_id="B", branch="b2",
                        original="O", cells={(0, 2)})
            with self.assertRaises(delivery.DeliveryError):
                store.redeem_batch(["a", "b"], protections=PROT)
            self.assertEqual(store.record.cells, {})

    def test_audit_trace(self):
        with delivery.DeliveryStore.open(self.path) as store:
            store.show(label_id="L", composition=comp("c", 3), check=feasible_check())
            audit = store.audit("L")
            self.assertEqual(audit["events"][0]["kind"], "shown")
            self.assertEqual(audit["public_cells"].get("O", []), [(0, 3)])


if __name__ == "__main__":
    unittest.main()
