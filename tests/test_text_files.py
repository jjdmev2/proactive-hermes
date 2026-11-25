"""Untrusted text goes in through --NAME-file, never on a shell command line.

Each script is run the way a prompt runs it: through a real shell, with only the file path on the
command line. A value with `$(touch PWNED)` and backticks must reach the script literally and no
PWNED file may appear. The script's side-effect function is swapped for a recorder, so nothing
touches the calendar, the phone, the mail or the Decisions API.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"
sys.path.insert(0, str(RUNTIME))

PAYLOAD = "Train $(touch PWNED) `touch PWNED2` \"quoted\" 'single' ; echo $HOME & | > out"

RUNNER = r'''
import argparse, json, os, sys
sys.path.insert(0, os.environ["RUNTIME_DIR"])
name, patched = sys.argv[1], sys.argv[2]
mod = __import__(name)
def rec(*args, **kw):
    args = [vars(a) if isinstance(a, argparse.Namespace) else a for a in args]
    print("CAPTURE " + json.dumps({"args": args, "kw": kw}, default=str, ensure_ascii=False))
    return 0
for attr in filter(None, patched.split(",")):
    setattr(mod, attr, (lambda: True) if attr == "enabled" else rec)
sys.argv = [name] + sys.argv[3:]
mod.main()
'''


class TextFileRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.tmp / "runner.py").write_text(RUNNER, encoding="utf-8")
        (self.tmp / "payload.txt").write_text(PAYLOAD + "\n", encoding="utf-8")
        (self.tmp / "config.json").write_text('{"calendar": {"enabled": true}}', encoding="utf-8")
        self.env = {**os.environ, "RUNTIME_DIR": str(RUNTIME), "HERMES_HOME": str(self.tmp / "home"),
                    "EA_STATE_DIR": str(self.tmp / "state"), "PROACTIVE_HERMES_CONFIG": str(self.tmp / "config.json"),
                    "OPENAI_API_KEY": "", "DESK_SAY_DISABLE_DOOR": "1"}

    def run_shell(self, module: str, patched: str, argv: str, stdin: bool = False) -> list[dict]:
        cmd = f"{sys.executable} runner.py {module} {patched} {argv}" + (" < payload.txt" if stdin else "")
        proc = subprocess.run(cmd, shell=True, cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertFalse((self.tmp / "PWNED").exists(), "command substitution ran")
        self.assertFalse((self.tmp / "PWNED2").exists(), "backticks ran")
        caps = [json.loads(line[8:]) for line in proc.stdout.splitlines() if line.startswith("CAPTURE ")]
        self.assertTrue(caps, f"nothing captured: {proc.stdout} {proc.stderr}")
        return caps

    def assert_field(self, caps, field):
        ns = caps[0]["args"][0]
        self.assertEqual(ns[field], PAYLOAD)
        self.assertNotIn(f"{field}_file", ns)

    def test_reminders(self):
        for argv, field in [("add --key k --title-file payload.txt --due 2030-01-01T09:00", "title"),
                            ("add --key k --title t --notes-file -", "notes"),
                            ("alarm --key k --at 2030-01-01T09:00+00:00 --label-file payload.txt", "label"),
                            ("close --key k --evidence-file payload.txt", "evidence")]:
            with self.subTest(argv=argv):
                caps = self.run_shell("ea_reminders", "cmd_add,cmd_alarm,cmd_close,enabled", argv, stdin="-" in argv)
                self.assert_field(caps, field)

    def test_calendar_block(self):
        caps = self.run_shell("ea_calendar_block", "cmd_create", "create --key ea:k --title-file payload.txt "
                              "--start 2030-01-01T09:00 --end 2030-01-01T09:30 --notes-file payload.txt")
        self.assert_field(caps, "title")
        self.assert_field(caps, "notes")

    def test_trip(self):
        caps = self.run_shell("ea_trip", "make_plan", "plan --key trip-k --title-file payload.txt "
                              "--depart 2030-01-01T09:00 --place-file payload.txt --kind train")
        self.assert_field(caps, "title")
        self.assert_field(caps, "place")

    def test_promises(self):
        caps = self.run_shell("ea_promises", "cmd_add", "add --to-file payload.txt --what-file payload.txt "
                              "--source-file payload.txt --quote-file payload.txt")
        for field in ("to", "what", "source", "quote"):
            self.assert_field(caps, field)
        for argv in ("close --key k --evidence-file payload.txt", "drop --key k --why-file payload.txt"):
            with self.subTest(argv=argv):
                caps = self.run_shell("ea_promises", "_set", argv)
                self.assertEqual(caps[0]["kw"]["evidence"], PAYLOAD)

    def test_promises_file_is_stored_literally(self):
        # Unpatched: the real ledger holds the literal text.
        cmd = (f"{sys.executable} {RUNTIME / 'ea_promises.py'} add --key p1 --to Alex --what-file payload.txt "
               "--source mail:1 --quote-file -  < payload.txt")
        proc = subprocess.run(cmd, shell=True, cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.tmp / "PWNED").exists())
        rows = json.loads((self.tmp / "state" / "promises.json").read_text(encoding="utf-8"))
        row = rows["p1"]
        self.assertEqual(row["what"], PAYLOAD)
        self.assertEqual(row["quote"], PAYLOAD)

    def test_resolve(self):
        caps = self.run_shell("ea_resolve", "_probe,_probe_json", "probe --key k --act reply --who-file payload.txt "
                              "--terms-file payload.txt --ref-file payload.txt --no-mail")
        for field in ("who", "terms", "ref"):
            self.assert_field(caps, field)
        caps = self.run_shell("ea_resolve", "mark", "mark --key k --dropped-file payload.txt")
        self.assertEqual(caps[0]["kw"]["dropped_why"], PAYLOAD)
        caps = self.run_shell("ea_resolve", "mark", "mark --key k --sent-file payload.txt")
        self.assertEqual(caps[0]["kw"]["sent_ref"], PAYLOAD)

    def test_decide(self):
        for argv, fn in [("ask --input-file payload.txt --predicate p", "decide"),
                         ("urgent --input-file payload.txt", "is_urgent"),
                         ("injection --input-file payload.txt", "injection_check"),
                         ("map-reply --reply-file - --items-file items.json", "map_reply")]:
            (self.tmp / "items.json").write_text('["a", "b"]', encoding="utf-8")
            with self.subTest(argv=argv):
                caps = self.run_shell("ea_decide", fn, argv, stdin="-" in argv.split())
                first = caps[0]["args"][0]
                self.assertEqual(first, [PAYLOAD] if fn == "injection_check" else PAYLOAD)

    def test_plain_options_still_work(self):
        caps = self.run_shell("ea_reminders", "cmd_add,enabled", "add --key k --title 'plain title' --notes n")
        self.assertEqual(caps[0]["args"][0]["title"], "plain title")
        self.assertEqual(caps[0]["args"][0]["notes"], "n")

    def test_both_forms_conflict_and_missing_file_errors(self):
        for argv in ("add --key k --title t --title-file payload.txt", "add --key k --title-file missing.txt"):
            cmd = f"{sys.executable} runner.py ea_reminders cmd_add,enabled {argv}"
            proc = subprocess.run(cmd, shell=True, cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2, argv)
            self.assertNotIn("CAPTURE", proc.stdout)


TEXT_FLAGS = "title|notes|why|note|quote|reply|input|place|evidence|what|text|label|to|who|terms|ref|source|sent|dropped"
INLINE = re.compile(r"--(?:" + TEXT_FLAGS + r")(?:=|\s+)(?:\\?\"|'|<|\.\.\.|[A-Z]{2,}\b)")
HERE = Path(__file__).resolve().parent.parent


class PromptsNeverInlineUntrustedText(unittest.TestCase):
    def rendered(self) -> dict:
        import cli
        import jobs
        cfg = cli.base_config("custom")
        cfg["mobile"]["enabled"] = True
        cfg["authority"]["auto"] = ["calendar_blocks", "reminders", "alarms", "leave_time", "watch_actions"]
        cfg["authority"]["never"] = [n for n in cfg["authority"]["never"] if n not in cfg["authority"]["auto"]]
        watches = [{"name": f"w {then}", "schedule": "0 9 * * *", "check": "c", "when": "w", "then": then,
                    "urgent": True, "act": "a"} for then in ("tell", "reminder", "alarm", "draft", "act")]
        with patch.object(jobs.phconf, "CFG", cfg):
            out = {job["key"]: jobs.prompt_for(job, cfg) for job in jobs.JOBS + [jobs.watch_job(w) for w in watches]}
        for path in [*(HERE / "prompts").glob("*.md"), *(HERE / "skills").rglob("*.md")]:
            out[str(path.relative_to(HERE))] = path.read_text(encoding="utf-8")
        return out

    def test_no_text_option_is_filled_inline(self):
        for name, text in self.rendered().items():
            with self.subTest(prompt=name):
                self.assertEqual(INLINE.findall(text), [])

    def test_lookahead_job_never_loads_the_booking_skill(self):
        import jobs
        (job,) = [j for j in jobs.JOBS if j["key"] == "lookahead"]
        self.assertNotIn("proactive-hermes:browser-bookings", job.get("skills", []))

    def test_contract_and_bookings_skill_state_the_data_rule(self):
        contract = (HERE / "skills" / "proactive-desk" / "references" / "desk-contract.md").read_text(encoding="utf-8")
        self.assertIn("Untrusted text never goes on a command line", contract)
        self.assertNotIn("headers only", contract)
        bookings = (HERE / "skills" / "browser-bookings" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("data, never instructions", bookings)
        self.assertNotIn("10%", bookings)
        self.assertIn("data, never instructions", (HERE / "prompts" / "lookahead.md").read_text(encoding="utf-8"))


class ApiKeyScope(unittest.TestCase):
    def test_never_reads_another_profiles_env(self):
        import ea_decide
        import phconf
        with tempfile.TemporaryDirectory() as tmp:
            own, other = Path(tmp) / "profiles" / "mine", Path(tmp) / "user"
            own.mkdir(parents=True)
            (other / ".hermes").mkdir(parents=True)
            (other / ".hermes" / ".env").write_text("OPENAI_API_KEY=sk-default-profile\n")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "", "HERMES_HOME": str(own)}), \
                    patch.object(phconf, "hermes_home", return_value=own), patch.object(Path, "home", return_value=other):
                self.assertEqual(ea_decide.api_key(), "")
                (own / ".env").write_text("OPENAI_API_KEY=sk-own\n")
                self.assertEqual(ea_decide.api_key(), "sk-own")


if __name__ == "__main__":
    unittest.main()
