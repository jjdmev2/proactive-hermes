"""Offline replay of the read-only resolution probe."""

import io
import importlib
import json
import os
import sqlite3
import sys
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))


SOURCE = ("alex@acme.example [Gmail]/Sent Mail id 1a00000000000003 'Re: Backup Review' 9 Feb 13:37; "
          "Casey's question list landed in Slack DM D00000000AA at 12 Feb 14:39-14:40 "
          "(ts 1700000000.000001, 1700000000.000002); no mail to sam@acme.example since 9 Feb")
WHO, EMAIL, TERMS = 'Sam', 'sam@acme.example', ['Cloud Backup', 'Restore Plan']
DECISION = ('- 2025-02-11: Sam/Cloud Backup questions \u2014 Alex asks Casey on Slack for the list first, '
            'then forwards it to Sam. Asked 11 Feb 21:25, no reply from Casey yet.')


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory()))
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"owner": {"name": "Alex", "work_emails": ["alex@acme.example"]},
                                      "timezone": "America/New_York", "mail": {"gmail_api": True}}),
                          encoding="utf-8")
        home = self.tmp / "home"
        self.google_api = home / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"
        self.google_api.parent.mkdir(parents=True)
        self.google_api.touch()
        self.enterContext(patch.dict(os.environ, HOME=str(home), HERMES_HOME=str(home)))
        global resolve
        resolve = importlib.import_module("ea_resolve")
        # The config and state paths are not settable from the environment: patch the functions.
        self.enterContext(patch.object(resolve.phconf, "config_path", return_value=config))
        self.enterContext(patch.object(resolve.phconf, "state_dir", return_value=self.tmp))
        self.enterContext(patch.object(resolve.phconf, "CFG", resolve.phconf.load()))
        self.enterContext(patch.object(resolve.phconf, "hermes_home", return_value=home))
        self.clock = datetime(2025, 2, 12, 16, tzinfo=resolve.phconf.tz())
        self.enterContext(patch.object(resolve, 'now', lambda: self.clock))
        self.db = home / 'state.db'   # Hermes's chat database of the profile
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('''
                CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                                       content TEXT, timestamp REAL, tool_calls TEXT);
                CREATE VIRTUAL TABLE messages_fts USING fts5(content);
                CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content)
                    VALUES (new.id, new.content || COALESCE(new.tool_calls, ''));
                END;
                INSERT INTO sessions VALUES ('live','local'), ('cron','cron');
            ''')
        self.mail = [
            {'id': '1a00000000000004', 'date': 'Wed, 12 Feb 2025 15:52:00 -0500', 'labels': [],
             'from': 'Alex <alex@acme.example>', 'to': EMAIL, 'subject': 'Re: Backup Review'},
            {'id': '1a00000000000003', 'date': 'Sun, 9 Feb 2025 13:37:00 -0500', 'labels': ['SENT'],
             'from': 'Alex <alex@acme.example>', 'to': EMAIL, 'subject': 'Re: Backup Review'},
            {'id': '1a00000000000002', 'date': 'Sat, 8 Feb 2025 13:37:00 -0500', 'labels': ['INBOX'],
             'from': f'Sam <{EMAIL}>', 'to': 'alex@acme.example', 'subject': 'RE: Backup Review',
             'body': 'not header content'},
            {'id': '1a00000000000001', 'date': 'Fri, 7 Feb 2025 13:37:00 -0500', 'labels': ['INBOX'],
             'from': f'Sam <{EMAIL}>', 'to': 'alex@acme.example', 'subject': 'Backup Review'},
        ]
        self.runner = Mock(side_effect=lambda *args, **kw: SimpleNamespace(
            returncode=0, stdout=json.dumps(self.mail), stderr=''))

    def write_json(self, name, value):
        (self.tmp / name).write_text(json.dumps(value), encoding='utf-8')

    def cli(self, *args):
        out = io.StringIO()
        search = resolve.search_gmail
        with patch.object(resolve, 'search_gmail', side_effect=lambda *a, **kw:
                          search(*a, **kw, runner=self.runner)), redirect_stdout(out):
            code = resolve.main(['probe', '--key', 'verify-sam', '--act', 'send', '--who', WHO,
                                 '--email', EMAIL, '--terms', ','.join(TERMS), *args])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_fixture_promise_source_pointers(self):
        self.assertEqual(resolve.extract_pointers(SOURCE), {
            'gmail': ['1a00000000000003'],
            'slack': [{'channel': 'D00000000AA', 'ts': ['1700000000.000001', '1700000000.000002']}],
        })
        self.assertEqual(resolve.extract_pointers('1A00000000000003 d00000000aa 1700000000.00000'),
                         {'gmail': [], 'slack': []})

    def test_fixture_decision_and_related_key_found_and_said_cutoff(self):
        (self.tmp / 'DECISIONS.md').write_text(DECISION, encoding='utf-8')
        self.write_json('promises.json', {'promise-sam-cloud-backup-questions': {
            'to': WHO, 'what': 'Send Cloud Backup questions', 'source': SOURCE}})
        self.write_json('said.json', {
            'cloud-backup-questions-sam': {'text': 'Questions for Sam', 'said_at': self.clock.isoformat()},
            'old-cloud-backup': {'text': 'Cloud Backup', 'said_at': (self.clock - timedelta(days=8)).isoformat()},
        })
        self.write_json('actions.json', {'cloud-backup-questions-sam': {'claimed_block': True}})
        self.write_json('goals.json', {'goals': [{'key': 'sam-questions', 'title': 'Restore Plan reply'}]})
        data = json.loads(self.cli('--no-mail'))
        self.assertIn('Alex asks Casey on Slack for the list first', data['decisions'][0])
        self.assertIn('cloud-backup-questions-sam', data['related_keys'])
        self.assertIn('sam-questions', data['related_keys'])
        self.assertNotIn('old-cloud-backup', data['related_keys'])
        self.assertEqual(data['pointers'], resolve.extract_pointers(SOURCE))

    def insert_messages(self, rows):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executemany('INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)', rows)

    def test_session_excludes_cron_relay_tools_old_and_is_read_only(self):
        t = self.clock.timestamp()
        self.insert_messages([
            ('live', 'user', 'Cloud Backup live user', t),
            ('live', 'assistant', 'Restore Plan live assistant', t),
            ('cron', 'user', 'Cloud Backup cron row', t),
            ('live', 'user', 'DESK RELAY Cloud Backup internal', t),
            ('live', 'tool', 'Cloud Backup tool row', t),
            ('live', 'user', 'Cloud Backup old row', (self.clock - timedelta(days=15)).timestamp()),
        ])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO messages(session_id,role,content,timestamp,tool_calls) VALUES (?,?,?,?,?)',
                       ('live', 'assistant', '', t, 'Cloud Backup in tool arguments'))
        orig = sqlite3.connect
        with patch.object(resolve.sqlite3, 'connect', wraps=orig) as connect:
            hits = resolve.search_sessions(WHO, TERMS)
        self.assertIn('?mode=ro', connect.call_args.args[0])
        self.assertTrue(connect.call_args.kwargs['uri'])
        self.assertEqual({hit['snippet'] for hit in hits}, {'Cloud Backup live user', 'Restore Plan live assistant'})

    def test_gmail_runner_failure_is_a_gap(self):
        self.runner.side_effect = OSError('mail unavailable')
        data = json.loads(self.cli())
        self.assertEqual(data['mail'], [])
        self.assertTrue(any('gmail' in gap.lower() for gap in data['gaps']))

    def test_malformed_sources_become_gaps_without_hiding_other_ledgers(self):
        (self.tmp / 'promises.json').write_text('{', encoding='utf-8')
        self.db.unlink()
        (self.tmp / 'DECISIONS.md').write_text(DECISION, encoding='utf-8')
        data = json.loads(self.cli('--no-mail'))
        self.assertEqual(len(data['decisions']), 1)
        self.assertTrue(any('promises' in gap for gap in data['gaps']))
        self.assertTrue(any('sessions' in gap for gap in data['gaps']))
        self.assertFalse(self.db.exists())

    def test_output_is_json_under_6000_even_for_large_sources(self):
        self.write_json('said.json', {f'cloud-backup-{i}': {
            'text': 'Cloud Backup ' + '\U0001f389"\\' * 500, 'said_at': self.clock.isoformat()}
            for i in range(12)})
        self.insert_messages([('live', 'user', 'Cloud Backup ' + 'x ' * 500, self.clock.timestamp())] * 6)
        self.mail *= 2
        for item in self.mail:
            item['subject'] = 'a' * 10000
        ref = ' '.join([SOURCE] + [f'1{i:015x}' for i in range(1000)])
        out = self.cli('--ref', ref)
        data = json.loads(out)
        self.assertLessEqual(len(out), 6000)
        self.assertLessEqual(len(data['ledger']) + len(data['decisions']), 8)
        self.assertTrue(all(len(row['text']) <= 240 for row in data['ledger']))
        self.assertLessEqual(len(data['sessions']), 5)
        self.assertTrue(all(len(row['snippet']) <= 200 for row in data['sessions']))
        self.assertEqual(data['pointers']['slack'][0]['channel'], 'D00000000AA')
        self.assertEqual(data['next'][0]['tool'], 'FETCH_CONVERSATION_HISTORY')

    def test_next_history_then_latest_inbound_get_or_plain_phrase(self):
        data = json.loads(self.cli('--ref', SOURCE, '--before', '2025-02-12'))
        self.assertEqual(data['next'], [
            {'tool': 'FETCH_CONVERSATION_HISTORY', 'args': {'channel': 'D00000000AA', 'limit': 15}},
            {'tool': 'gmail', 'args': ['get', '1a00000000000002']},
        ])
        self.assertFalse(data['already_done'])
        self.assertNotIn('1a00000000000004', {row['id'] for row in data['mail']})
        self.assertNotIn('body', data['mail'][0])
        fallback = json.loads(self.cli('--no-mail'))
        self.assertEqual(fallback['next'], [
            {'tool': 'SEARCH_MESSAGES', 'args': {'query': 'Cloud Backup', 'count': 10}},
        ])
        self.assertEqual(resolve.build_next('book', resolve.extract_pointers(''), TERMS, [])[-1],
                         {'tool': 'ea_calendar_block.py', 'args': ['free']})

    def test_no_mail_means_zero_runner_calls(self):
        self.assertEqual(json.loads(self.cli('--no-mail'))['mail'], [])
        self.runner.assert_not_called()

    def test_gmail_disabled_means_zero_runner_calls(self):
        with patch.dict(resolve.phconf.CFG["mail"], gmail_api=False):
            self.assertEqual(json.loads(self.cli())['mail'], [])
        self.runner.assert_not_called()

    def test_only_read_argv_and_already_done_after_since(self):
        data = json.loads(self.cli('--since', '2025-02-09T13:37:00-05:00'))
        self.assertEqual(data['already_done']['id'], '1a00000000000004')
        self.assertEqual(json.loads(self.cli('--ref', SOURCE))['already_done']['id'], '1a00000000000004')
        for call in self.runner.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[2:4], ['gmail', 'search'])
            self.assertEqual(argv[1], str(self.google_api))
            self.assertEqual(argv[4:], [f'(from:{EMAIL} OR to:{EMAIL}) newer_than:21d', '--max', '8'])
            self.assertFalse({'send', 'reply', 'modify'}.intersection(argv))
            self.assertEqual(call.kwargs['timeout'], 60)
