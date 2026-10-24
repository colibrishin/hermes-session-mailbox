"""Behavioral session discovery regressions on isolated SQLite metadata."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('listing_store', ROOT / 'mailbox_store.py')
assert spec and spec.loader
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class ListingTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        if not scratch and os.environ.get('HERMES_HOME'):
            scratch = str(Path(os.environ['HERMES_HOME']) / 'cache' / 'scratch')
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        with closing(sqlite3.connect(self.home / 'state.db')) as db:
            db.execute('CREATE TABLE sessions(id TEXT,title TEXT,source TEXT,started_at REAL,last_activity_at REAL)')
            db.executemany('INSERT INTO sessions VALUES (?,?,?,?,?)', [
                ('a-old','Older chat','local',10,15),
                ('z-new','Newest chat','local',5,40),
                ('b-worker','Worker','subagent',50,60)])
            db.commit()
        (self.home / 'runtime').mkdir()
        (self.home / 'runtime' / 'active_sessions.json').write_text(json.dumps({'entries':[
            {'session_id':'b-worker','surface':'subagent','updated_at':60},
            {'session_id':'a-old','surface':'gateway:local','updated_at':15}]}))
        self.box = store.Mailbox(self.home / 'mailbox.sqlite3', self.home)

    def test_default_discovers_recent_chats_compactly(self):
        result = self.box.list_sessions()
        self.assertEqual([r['session'] for r in result['sessions']], ['z-new','a-old'])
        self.assertEqual(set(result['sessions'][0]), {'profile','session','title'})

    def test_filter_sort_before_pagination_and_search(self):
        page = self.box.list_sessions(limit=1)
        self.assertTrue(page['has_more'])
        second = self.box.list_sessions(limit=1, offset=page['next_offset'])
        self.assertEqual(second['sessions'][0]['session'], 'a-old')
        result = self.box.list_sessions(query='NEWEST')
        self.assertEqual([r['session'] for r in result['sessions']], ['z-new'])

    def test_optional_subagents_and_details(self):
        result = self.box.list_sessions(include_subagents=True, detailed=True)
        self.assertEqual(result['sessions'][0]['session'], 'b-worker')
        liveness = {row['session']: row['liveness'] for row in result['sessions']}
        self.assertEqual(liveness, {'b-worker': 'unverified_registry_hint', 'z-new': 'not_in_registry',
                                    'a-old': 'unverified_registry_hint'})
        for row in result['sessions']:
            self.assertFalse({'stored_session_id', 'verified_alive', 'active_hint'} & set(row), row)

    def test_registry_activity_survives_null_or_stale_stored_activity(self):
        for activity in (None, 10):
            with closing(sqlite3.connect(self.home / 'state.db')) as db:
                db.execute('UPDATE sessions SET last_activity_at=? WHERE id=?', (activity, 'a-old'))
                db.commit()
            registry = self.home / 'runtime' / 'active_sessions.json'
            registry.write_text(json.dumps({'entries': [
                {'session_id': 'a-old', 'surface': 'gateway:local', 'updated_at': 100}]}))
            result = self.box.list_sessions()
            self.assertEqual(result['sessions'][0]['session'], 'a-old', activity)

    def test_registry_only_and_invalid_arguments(self):
        result = self.box.list_sessions(include_recent=False)
        self.assertEqual([r['session'] for r in result['sessions']], ['a-old'])
        for args in ({'include_subagents':1}, {'detailed':'yes'}, {'query':3}):
            with self.assertRaises(ValueError):
                self.box.list_sessions(**args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
