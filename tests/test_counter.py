import shutil
import tempfile
import unittest
from pathlib import Path

from museum.counter import Counter
from museum.wire import Envelope, MessageBus

DOMAINS = {"A": [1, 3], "B": [1, 3]}
SCALE = [{"receipt_id": "P0", "counts": {"A": 1, "B": 1}, "lo": 4, "hi": 4}]


def pump(*counters: Counter) -> None:
    for _ in range(10):
        moved = False
        for counter in counters:
            if counter.run_inbox():
                moved = True
            if counter.run_acks():
                moved = True
        if not moved:
            break


class CounterNetworkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.bus = MessageBus(self.root / "spool")
        self.hq = Counter("JIA", self.root / "jia.db", self.bus, role="hq")
        self.yi = Counter("YI", self.root / "yi.db", self.bus, role="branch")
        self.bing = Counter("BING", self.root / "bing.db", self.bus, role="branch")

    def tearDown(self):
        for counter in (self.hq, self.yi, self.bing):
            counter.close()
        shutil.rmtree(self.root)

    def reopen_all(self):
        self.hq.close()
        self.yi.close()
        self.bing.close()
        hq = Counter.reopen("JIA", self.root / "jia.db", self.bus)
        yi = Counter.reopen("YI", self.root / "yi.db", self.bus)
        bing = Counter.reopen("BING", self.root / "bing.db", self.bus)
        return hq, yi, bing

    def test_dispatch_split_reweigh_return_settle(self):
        self.hq.hq_dispatch("B0", "S0", "YI", {"A": 1, "B": 1})
        pump(self.hq, self.yi, self.bing)
        self.assertEqual([s["seal_id"] for s in self.yi.held_seals()], ["S0"])

        self.yi.request_split_merge(
            "B2",
            ["S0"],
            [
                {"seal_id": "a", "holder": "YI", "contents": {"A": 1}},
                {"seal_id": "b", "holder": "BING", "contents": {"B": 1}},
            ],
        )
        pump(self.hq, self.yi, self.bing)
        self.assertEqual([s["seal_id"] for s in self.yi.held_seals()], ["a"])
        self.assertEqual([s["seal_id"] for s in self.bing.held_seals()], ["b"])
        self.assertEqual(self.yi.pending_requests(), [])

        self.yi.request_reweigh("a", (1, 1))
        self.bing.request_reweigh("b", (3, 3))
        pump(self.hq, self.yi, self.bing)
        self.yi.request_return("a")
        self.bing.request_return("b")
        pump(self.hq, self.yi, self.bing)

        report = self.hq.settle(DOMAINS, SCALE)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["weight_report"]["masses"], {"A": 1, "B": 3})

    def test_unauthorized_forward_rejected(self):
        self.hq.hq_dispatch("B0", "S0", "BING", {"A": 1})
        pump(self.hq, self.yi, self.bing)
        self.yi.request_forward("BX", "S0", "YI")
        report = self.hq.run_inbox()
        self.hq.run_acks()
        self.yi.run_acks()
        self.assertFalse(report[0]["accepted"])
        self.assertIn("not the current holder", report[0]["note"])

    def test_retransmission_is_idempotent(self):
        self.hq.hq_dispatch("B0", "S0", "YI", {"A": 1, "B": 1})
        pump(self.hq, self.yi, self.bing)
        self.yi.request_split_merge(
            "B2",
            ["S0"],
            [
                {"seal_id": "a", "holder": "YI", "contents": {"A": 1}},
                {"seal_id": "b", "holder": "BING", "contents": {"B": 1}},
            ],
        )
        # Resend before HQ processes: identical envelope, same message id.
        self.yi.resend_unacked()
        pump(self.hq, self.yi, self.bing)
        batches = [b for b in self.hq.ledger.batches if b.batch_id == "B2"]
        self.assertEqual(len(batches), 1)

    def test_restart_recovers_pending_work(self):
        self.hq.hq_dispatch("B0", "S0", "YI", {"A": 1})
        pump(self.hq, self.yi, self.bing)
        self.yi.request_split_merge(
            "BOFF",
            ["S0"],
            [{"seal_id": "d1", "holder": "YI", "contents": {"A": 1}}],
        )
        hq, yi, bing = self.reopen_all()
        self.assertEqual(len(yi.pending_requests()), 1)
        yi.resend_unacked()
        pump(hq, yi, bing)
        self.assertIn("d1", hq.ledger.seals)
        self.assertEqual(
            len([b for b in hq.ledger.batches if b.batch_id == "BOFF"]), 1
        )
        self.hq, self.yi, self.bing = hq, yi, bing

    def test_global_conflict_across_branches(self):
        self.hq.hq_dispatch("B0", "S0", "YI", {"A": 1, "B": 1})
        pump(self.hq, self.yi, self.bing)
        self.yi.request_split_merge(
            "B2",
            ["S0"],
            [
                {"seal_id": "a", "holder": "YI", "contents": {"A": 1}},
                {"seal_id": "b", "holder": "BING", "contents": {"B": 1}},
            ],
        )
        pump(self.hq, self.yi, self.bing)
        self.yi.request_reweigh("a", (1, 1))
        self.bing.request_reweigh("b", (1, 1))
        pump(self.hq, self.yi, self.bing)
        self.yi.request_return("a")
        self.bing.request_return("b")
        pump(self.hq, self.yi, self.bing)
        report = self.hq.settle(DOMAINS, SCALE)
        self.assertFalse(report["ok"])
        self.assertTrue(any(p["code"] == "weight_conflict" for p in report["problems"]))


class WireEnvelopeTests(unittest.TestCase):
    def test_message_id_is_content_addressed(self):
        payload = {"k": 1, "nested": {"x": [1, 2]}}
        e1 = Envelope.create("YI", "HQ", "forward", payload)
        e2 = Envelope.create("YI", "HQ", "forward", dict(payload))
        self.assertEqual(e1.message_id, e2.message_id)
        e3 = Envelope.create("YI", "HQ", "forward", {"k": 2, "nested": {"x": [1, 2]}})
        self.assertNotEqual(e1.message_id, e3.message_id)

    def test_drain_is_idempotent_on_message_id(self):
        root = Path(tempfile.mkdtemp())
        try:
            bus = MessageBus(root / "spool")
            bus.send("YI", "HQ", "ping", {"v": 1})
            seen = []

            def handler(env):
                seen.append(env.message_id)
                return True, "ok"

            first = bus.drain("HQ", handler)
            self.assertEqual(len(first), 1)
            # Re-deliver the same envelope and confirm the handler runs again
            # safely at the transport layer (application dedup happens in
            # Counter via processed_messages).
            bus.send("YI", "HQ", "ping", {"v": 1})
            second = bus.drain("HQ", handler)
            self.assertEqual(seen[0], seen[1])
            self.assertEqual(len(second), 1)
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
