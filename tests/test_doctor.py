"""Hermetic tests for ph_doctor: no gateway, no phone, no network."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
with mock.patch.dict(os.environ, HERMES_HOME=tempfile.mkdtemp()):   # never the real profile
    import ph_doctor  # noqa: E402


class DoctorTests(unittest.TestCase):
    def test_gateway_down_is_not_ready_and_explains_fix(self):
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(False, "profile authority is not ready")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(False, "not created yet")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(False, "Hermes mobile app not connected")):
            rep = ph_doctor.report()
        self.assertFalse(rep["ready"])
        self.assertIn("gateway", rep["checks"]["gateway"]["fix"].lower())
        self.assertIn("Bot Mode", rep["checks"]["bot_chat"]["fix"])
        self.assertIn("mobile app", rep["checks"]["mobile"]["fix"])

    def test_ready_with_gateway_even_before_first_bot_chat(self):
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(False, "not created yet")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(True, "phone paired")):
            rep = ph_doctor.report()
        self.assertTrue(rep["ready"])

    def test_send_test_skipped_without_gateway(self):
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(False, "down")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(False, "")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(False, "")):
            rep = ph_doctor.report(send_test=True)
        self.assertEqual(rep["test_message"]["status"], "skipped")

    def test_send_test_uses_desk_say(self):
        fake = mock.Mock(return_value={"status": "delivered", "id": "abc"})
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(True, "")), \
             mock.patch.dict(sys.modules, {"desk_say": mock.Mock(deliver=fake)}):
            rep = ph_doctor.report(send_test=True)
        self.assertEqual(rep["test_message"]["status"], "delivered")
        self.assertTrue(fake.call_args.kwargs["urgent"])

    def test_decisions_reports_what_scheduled_runs_see(self):
        import ea_decide
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(True, "")), \
             mock.patch.dict(ea_decide.phconf.CFG, decisions={"enabled": True}), \
             mock.patch.object(ea_decide.phconf, "secret", side_effect=lambda n: {"OPENAI_API_KEY": "sk-x"}.get(n, "")):
            rep = ph_doctor.report()
        dec = rep["checks"]["decisions"]
        self.assertFalse(dec["ok"])                       # OPENAI_API_KEY alone never reaches scheduled scripts
        self.assertIn("Set PROACTIVE_OPENAI_API_KEY for scheduled runs", dec["note"])
        self.assertIn("hermes plugins config proactive-hermes", dec["fix"])
        self.assertNotIn("sk-x", str(rep))


if __name__ == "__main__":
    unittest.main()
