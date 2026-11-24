"""Hermetic tests for ea_decide (no network: a fake transport stands in for the Decisions API)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
os.environ["EA_STATE_DIR"] = tempfile.mkdtemp()
import ea_decide  # noqa: E402
ea_decide.STATE = __import__("pathlib").Path(os.environ["EA_STATE_DIR"])

MSGS = [{"from": "news@shop.example", "subject": "40% off"},
        {"from": "alex@client.example", "subject": "A or B by tomorrow?"},
        {"from": "bank@bank.example", "subject": "New sign-in"}]


def fake(probs):
    def transport(body, key):
        transport.body = body
        return {"model": body["model"], "answers": [
            {"type": "predicate", "name": q["name"], "probability": p} for q, p in zip(body["questions"], probs)]}
    return transport


class DecideTests(unittest.TestCase):
    def test_drops_only_clear_noise(self):
        keep, drop, info = ea_decide.triage_mail(MSGS, transport=fake([0.02, 0.95, 0.4]), key="k")
        self.assertEqual([m["subject"] for m in drop], ["40% off"])
        self.assertEqual(len(keep), 2)
        self.assertTrue(info["used"])

    def test_no_key_keeps_everything(self):
        keep, drop, info = ea_decide.triage_mail(MSGS, transport=fake([0.0, 0.0, 0.0]), key="")
        self.assertEqual((len(keep), len(drop), info["used"]), (3, 0, False))

    def test_transport_error_keeps_everything(self):
        def boom(body, key):
            raise OSError("network down")
        keep, drop, info = ea_decide.triage_mail(MSGS, transport=boom, key="k")
        self.assertEqual((len(keep), len(drop)), (3, 0))

    def test_missing_answer_is_kept(self):
        keep, drop, _ = ea_decide.triage_mail(MSGS, transport=fake([0.01]), key="k")
        self.assertEqual(len(drop), 1)
        self.assertEqual(len(keep), 2)

    def test_only_headers_are_sent(self):
        t = fake([0.5, 0.5, 0.5])
        ea_decide.triage_mail([{**m, "body": "SECRET BODY"} for m in MSGS], transport=t, key="k")
        self.assertNotIn("SECRET BODY", t.body["input"])
        self.assertEqual(t.body["model"], "gpt-6-luna")

    def test_old_output_key_still_parsed(self):
        out = ea_decide.decide("x", [{"type": "predicate", "name": "q", "instructions": "y"}],
                               transport=lambda b, k: {"output": [{"name": "q", "probability": 0.3}]}, key="k")
        self.assertEqual(out[0]["probability"], 0.3)


if __name__ == "__main__":
    unittest.main()
