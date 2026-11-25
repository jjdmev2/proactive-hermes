"""Catalog rule 11: credentials come through Hermes (agent.secret_scope.get_secret), never from files.

Guards the whole plugin source: no .env parsing, no himalaya config, no keyring, no token files, and the three
runtime paths that need a secret (decision filter, mail push) go through phconf.secret. The plugin never wakes the
phone itself, and the mobile bridge is touched in one function only.
"""
import builtins
import io
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runtime"))
import ea_decide  # noqa: E402
import phconf  # noqa: E402

try:  # `discover -s tests -t .` imports the package; plain `discover -s tests` does not.
    from .test_desk import reminders
except ImportError:
    from test_desk import reminders

# Code that reads another tool's credentials or a dotenv file. Prose may name these; code may not.
FORBIDDEN = [
    (r"""["'][^"'\n]*\.env["']""", "a .env path"),
    (r"config\.toml", "himalaya's config.toml"),
    (r"himalayarc", "himalaya's ~/.himalayarc"),
    (r"HIMALAYA_CONFIG", "himalaya's config path"),
    (r"\bimport\s+tomllib\b|\bimport\s+toml\b", "a TOML parser (another tool's config)"),
    (r"\bimport\s+keyring\b|\bkeyring\.", "the system keyring"),
    (r"google_token\.json", "the google-workspace token file"),
    (r"auth\.json", "Hermes's auth.json"),
    (r"load_dotenv|dotenv_values|load_env_file|build_profile_secret_scope", "a dotenv loader"),
]


def plugin_code() -> list[Path]:
    files = [ROOT / "__init__.py", ROOT / "cli.py", ROOT / "jobs.py", *sorted((ROOT / "runtime").glob("*.py"))]
    return [f for f in files if f.is_file()]


class NoCredentialFilesTests(unittest.TestCase):
    def test_no_dotenv_himalaya_keyring_or_token_reads_in_code(self):
        hits = []
        for path in plugin_code():
            for no, line in enumerate(path.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                for pattern, what in FORBIDDEN:
                    if re.search(pattern, code):
                        hits.append(f"{path.relative_to(ROOT)}:{no}: {what}: {line.strip()[:100]}")
        # Docstrings are prose: only flag lines that also look like code that opens or parses something.
        hits = [h for h in hits if re.search(r"open\(|read_text|read_bytes|Path\(|/ [\"']|load\(|import|keyring\.", h)]
        self.assertEqual(hits, [])

    def test_skills_and_prompts_never_tell_the_agent_to_read_credential_files(self):
        texts = [*(ROOT / "skills").rglob("*.md"), *(ROOT / "prompts").glob("*.md")]
        bad = re.compile(r"(cat|read|open|grep|source)\b[^\n]{0,40}(\.env\b|config\.toml|keyring|google_token)", re.I)
        hits = [f"{p.relative_to(ROOT)}: {m.group(0)}" for p in texts for m in bad.finditer(p.read_text())]
        self.assertEqual(hits, [])

    def test_secret_names_are_declared_in_the_manifest(self):
        manifest = (ROOT / "plugin.yaml").read_text()
        for name in ["OPENAI_API_KEY", "PROACTIVE_OPENAI_API_KEY", *(f"PROACTIVE_MAIL_{n}_PASSWORD" for n in range(1, 7))]:
            self.assertIn(name, manifest)
        self.assertRegex(manifest, r'decisions_api_key:\n    type: secret\n    env: PROACTIVE_OPENAI_API_KEY\n'
                                   r'    label: "OpenAI key for the decision filter \(optional\)"')
        for n in range(1, 7):
            self.assertRegex(manifest, rf"mail_{n}_password:\n    type: secret\n    env: PROACTIVE_MAIL_{n}_PASSWORD")
            self.assertIn(f"mail_{n}_address:", manifest)
        self.assertNotIn("Recommended for Juan", manifest)


class GuardedOpen:
    """Fails the test on any attempt to open a .env file or himalaya's config."""

    def __init__(self, test):
        self.test, self.real_open, self.real_read = test, builtins.open, Path.read_text

    def __enter__(self):
        test, real_open, real_read = self.test, self.real_open, self.real_read

        def check(path):
            name = str(path)
            if name.endswith(".env") or "himalaya" in name or name.endswith("google_token.json"):
                test.fail(f"credential file read: {name}")

        def guarded_open(file, *a, **k):
            check(file)
            return real_open(file, *a, **k)

        def guarded_read(self_path, *a, **k):
            check(self_path)
            return real_read(self_path, *a, **k)

        self.patches = [patch.object(builtins, "open", guarded_open), patch.object(Path, "read_text", guarded_read),
                        patch.object(io, "open", guarded_open)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def fake_scope(get_secret):
    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = get_secret
    agent.secret_scope = scope
    return patch.dict(sys.modules, {"agent": agent, "agent.secret_scope": scope})


class SecretRouteTests(unittest.TestCase):
    def test_phconf_secret_uses_hermes_get_secret(self):
        asked = []
        with fake_scope(lambda name, default=None: asked.append(name) or " sk-x "):
            self.assertEqual(phconf.secret("OPENAI_API_KEY"), "sk-x")
        self.assertEqual(asked, ["OPENAI_API_KEY"])

    def test_phconf_secret_fails_open_when_hermes_refuses(self):
        class UnscopedSecretError(RuntimeError):
            pass

        def refuse(name, default=None):
            raise UnscopedSecretError(name)

        with fake_scope(refuse):
            self.assertEqual(phconf.secret("PROACTIVE_MAIL_1_PASSWORD"), "")
        with fake_scope(lambda name, default=None: None):
            self.assertEqual(phconf.secret("OPENAI_API_KEY"), "")

    def test_decision_key_comes_from_get_secret_never_a_file(self):
        with GuardedOpen(self), fake_scope(lambda name, default=None: {"OPENAI_API_KEY": "sk-1"}.get(name)):
            self.assertEqual(ea_decide.api_key(), "sk-1")
        with GuardedOpen(self), fake_scope(lambda name, default=None: None):
            self.assertEqual(ea_decide.api_key(), "")       # fail open: no key, no filter

    def test_no_phone_wake_and_no_relay_secrets(self):
        self.assertFalse(hasattr(reminders, "wake"))
        manifest = (ROOT / "plugin.yaml").read_text()
        for path in [*plugin_code(), ROOT / "plugin.yaml"]:
            self.assertNotIn("HERMES_MOBILE_PUSH", path.read_text(), path.name)
        self.assertNotIn("HERMES_MOBILE_PUSH", manifest)

    def test_decision_key_prefers_the_plugin_key_then_openai_fallback(self):
        both = {"PROACTIVE_OPENAI_API_KEY": "sk-plugin", "OPENAI_API_KEY": "sk-provider"}
        with GuardedOpen(self), fake_scope(lambda name, default=None: both.get(name)):
            self.assertEqual(ea_decide.api_key(), "sk-plugin")
            self.assertEqual(ea_decide.key_status()[0], True)
        only_provider = {"OPENAI_API_KEY": "sk-provider"}
        with GuardedOpen(self), fake_scope(lambda name, default=None: only_provider.get(name)):
            self.assertEqual(ea_decide.api_key(), "sk-provider")          # fallback where Hermes shows it
            ok, note = ea_decide.key_status()
            self.assertFalse(ok)                                          # scheduled scripts will not see it
            self.assertIn("Set PROACTIVE_OPENAI_API_KEY for scheduled runs", note)
            self.assertNotIn("sk-", note)

    def test_mobile_bridge_is_touched_in_one_function_only(self):
        hits = []
        for path in plugin_code():
            for no, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"bridge\.db|bridge\.py|open_store", line.split("#", 1)[0]):
                    hits.append(f"{path.name}:{no}")
        # ea_reminders.bridge() builds the paths; enabled() and the trip planner only check bridge.py exists.
        allowed = {"ea_reminders.py", "ea_trip.py"}
        self.assertTrue(all(h.split(":")[0] in allowed for h in hits), hits)
        src = (ROOT / "runtime" / "ea_reminders.py").read_text()
        body = src[src.index("def bridge("):src.index("@state_lock(LEDGER)\ndef ledger_set")]
        self.assertIn('"bridge.db"', body)
        self.assertIn('"bridge.py"', body)
        self.assertIn("open_store", body)
        for mod in ("ea_health.py", "ea_trip.py", "ph_doctor.py"):
            text = (ROOT / "runtime" / mod).read_text()
            self.assertNotIn("sqlite3.connect", text, mod)

    def test_mail_push_never_opens_credential_files(self):
        import mail_idle
        settings = {"mail_1_address": "a@gmail.com"}
        with GuardedOpen(self), fake_scope(lambda name, default=None: {"PROACTIVE_MAIL_1_PASSWORD": "pw"}.get(name)):
            (acct,), skipped = mail_idle.load_accounts(settings)
        self.assertEqual((acct.host, acct.password(), skipped), ("imap.gmail.com", "pw", {}))


if __name__ == "__main__":
    unittest.main()
