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
             mock.patch.object(ph_doctor, "check_say_job", return_value=(True, "0123456789ab")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(False, "Hermes mobile app not connected")):
            rep = ph_doctor.report()
        self.assertFalse(rep["ready"])
        self.assertIn("gateway", rep["checks"]["gateway"]["fix"].lower())
        self.assertIn("Bot Mode", rep["checks"]["bot_chat"]["fix"])
        self.assertIn("mobile app", rep["checks"]["mobile"]["fix"])

    def test_ready_with_gateway_even_before_first_bot_chat(self):
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(False, "not created yet")), \
             mock.patch.object(ph_doctor, "check_say_job", return_value=(True, "0123456789ab")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(True, "phone paired")):
            rep = ph_doctor.report()
        self.assertTrue(rep["ready"])

    def test_missing_say_job_is_not_ready_and_names_setup(self):
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
             mock.patch.object(ph_doctor, "check_say_job", return_value=(False, "not installed")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(True, "")):
            rep = ph_doctor.report(send_test=True)
        self.assertFalse(rep["ready"])
        self.assertIn("hermes proactive setup", rep["checks"]["say_job"]["fix"])
        self.assertEqual(rep["test_message"], {"status": "skipped", "reason": "say job not ready"})

    def test_send_test_skipped_without_gateway(self):
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(False, "down")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(False, "")), \
             mock.patch.object(ph_doctor, "check_say_job", return_value=(True, "0123456789ab")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(False, "")):
            rep = ph_doctor.report(send_test=True)
        self.assertEqual(rep["test_message"]["status"], "skipped")

    def test_send_test_queues_kicks_and_waits_for_the_bot_reply(self):
        fake = mock.Mock(deliver=mock.Mock(return_value={"status": "handed", "id": "abc"}),
                         confirm=mock.Mock(side_effect=[0, 1]), delivered_ids=mock.Mock(return_value={"abc"}))
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
             mock.patch.object(ph_doctor, "check_say_job", return_value=(True, "0123456789ab")), \
             mock.patch.object(ph_doctor, "check_mobile", return_value=(True, "")), \
             mock.patch.object(ph_doctor.time, "sleep"), \
             mock.patch.dict(sys.modules, {"desk_say": fake}):
            rep = ph_doctor.report(send_test=True)
        self.assertEqual(rep["test_message"], {"status": "delivered", "id": "abc"})
        self.assertTrue(fake.deliver.call_args.kwargs["urgent"])

    def test_gateway_and_bot_chat_checks_use_public_surfaces(self):
        done = mock.Mock(stdout="\u2717 Gateway is not running\n", stderr="", returncode=1)
        with mock.patch.object(ph_doctor.subprocess, "run", return_value=done) as run:
            self.assertEqual(ph_doctor.check_gateway(Path("/nowhere")), (False, "\u2717 Gateway is not running"))
        self.assertEqual(run.call_args.args[0][1:], ["gateway", "status"])
        done.stdout = "\u2713 Gateway is running (PID: 1)\n"
        with mock.patch.object(ph_doctor.subprocess, "run", return_value=done):
            self.assertEqual(ph_doctor.check_gateway(Path("/nowhere")), (True, "running"))
        source = Path(ph_doctor.__file__).read_text()
        self.assertNotIn("bot_live_delivery", source)
        self.assertIn("open_persistence_reader", source)

    def test_gateway_check_reads_every_form_of_the_status_output(self):
        systemd = ("\u25cf hermes-gateway.service - Hermes Agent Gateway\n"
                   "     Loaded: loaded (/home/u/.config/systemd/user/hermes-gateway.service; enabled)\n"
                   "     Active: active (running) since Thu 2026-11-26 10:00:00 -03; 2h ago\n\n"
                   "\u2713 User gateway service is running\n")
        launchd = "PID\tStatus\tLabel\n123\t0\tai.hermes.gateway\n\n\u2713 User gateway service is running\n"
        multiplexed = ("\u2713 Gateway is running via the default-profile multiplexer\n"
                       "  Manage it from the default profile: hermes gateway status\n")
        manual = "\u2713 Gateway is running (PID: 4242)\n"
        for name, out in (("systemd", systemd), ("launchd", launchd), ("multiplexed", multiplexed), ("manual", manual)):
            with self.subTest(form=name):
                self.assertEqual(ph_doctor.gateway_running(out), (True, "running"))
        down = "\u25cf hermes-gateway.service - Hermes Agent Gateway\n     Active: inactive (dead)\n\u2717 Gateway is not running\n"
        self.assertEqual(ph_doctor.gateway_running(down), (False, "\u2717 Gateway is not running"))
        self.assertFalse(ph_doctor.gateway_running("")[0])

    def test_send_test_counts_a_reply_another_worker_already_confirmed(self):
        # The say job's own `next` run confirmed it first: confirm() finds nothing new, the id is delivered anyway.
        fake = mock.Mock(deliver=mock.Mock(return_value={"status": "handed", "id": "abc"}),
                         confirm=mock.Mock(return_value=0), delivered_ids=mock.Mock(return_value={"abc"}))
        with mock.patch.dict(sys.modules, {"desk_say": fake}), mock.patch.object(ph_doctor.time, "sleep"):
            self.assertEqual(ph_doctor.send_test_message(wait_s=5), {"status": "delivered", "id": "abc"})

    def test_say_job_check_needs_bot_chat_as_its_only_target(self):
        listing = mock.Mock(stdout="  0123456789ab [active]\n    Name:      Proactive \u00b7 say\n", stderr="")
        fake = mock.Mock(say_job_id=mock.Mock(return_value="0123456789ab"),
                         route_problem=mock.Mock(return_value=None))
        with mock.patch.dict(sys.modules, {"desk_say": fake}), \
                mock.patch.object(ph_doctor.subprocess, "run", return_value=listing):
            self.assertEqual(ph_doctor.check_say_job(), (True, "0123456789ab"))
            fake.route_problem.return_value = "say job delivers to local, not Bot Chat"
            ok, note = ph_doctor.check_say_job()
        self.assertFalse(ok)
        self.assertIn("not Bot Chat", note)
        fake.route_problem.assert_called_with("0123456789ab")
        self.assertIn("--deliver bot-chat", ph_doctor.FIX["say_job"])

    def test_decisions_reports_what_scheduled_runs_see(self):
        import ea_decide
        with mock.patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             mock.patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
             mock.patch.object(ph_doctor, "check_say_job", return_value=(True, "0123456789ab")), \
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
