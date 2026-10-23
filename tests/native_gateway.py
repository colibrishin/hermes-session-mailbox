"""Standalone native-gateway regressions; never open the user's databases.

Run directly: python -B tests/native_gateway.py
Only the live host boundary is faked; stores and PluginContext injection routing are real.
"""
import asyncio
import importlib.util
from pathlib import Path
import json
import os
import sys
import tempfile
import threading
from typing import Any, cast
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mailbox_native_receiver_test', REPO / 'wake_receiver.py')
assert spec is not None and spec.loader is not None
wake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wake)
from hermes_cli.plugins import PluginContext


class NativeGatewayTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        if not scratch and os.environ.get('HERMES_HOME'):
            scratch = str(Path(os.environ['HERMES_HOME']) / 'cache' / 'scratch')
        self.tmp = tempfile.TemporaryDirectory(prefix='mailbox-native-', dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = self.home / 'mailbox.sqlite3'
        (self.home / 'runtime').mkdir()
        (self.home / 'runtime' / 'active_sessions.json').write_text(
            json.dumps({'entries': [{'session_id': sid} for sid in
                                                 ('sender', 'recipient', 'not-owned')]}), encoding='utf-8')
        self.live = SimpleNamespace(route='agent:main:local:dm:recipient', task=None,
                                    source=SimpleNamespace(platform=__import__('gateway.config', fromlist=['Platform']).Platform.LOCAL,
                                                           chat_id='recipient'),
                                    controls=SimpleNamespace(pending={}), superseded=False,
                                    claim_gate=asyncio.Lock(), mutation_lock=asyncio.Lock())
        self.authority = SimpleNamespace(sessions={'recipient': self.live}, retiring=False, db=object())
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        ready = threading.Event()
        self.loop.call_soon(ready.set)
        self.thread.start()
        self.assertTrue(ready.wait(5), 'fixture event loop did not start')
        self.addCleanup(self.shutdown)
        self.calls = []
        self.host = SimpleNamespace(_gateway_loop=self.loop)
        self.manager = SimpleNamespace(home_path=self.home, _cli_ref=None,
                                       has_tui_message_injector=False, has_gateway_message_injector=True,
                                       _gateway_message_injector=(self.host, None))
        self.manager.inject_gateway_message = lambda **kw: self.calls.append(kw) or True
        self.ctx = SimpleNamespace(_manager=self.manager, plugin_id='session-mailbox-bundle',
                                   _gateway_injection_allowed=lambda: True)
        self.ctx.inject_message = lambda *a, **kw: PluginContext.inject_message(cast(Any, self.ctx), *a, **kw)
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('gateway.session_authorities.authority_for_home',
                                      side_effect=lambda host, home: self.authority if home == self.home else None))
        self.pending = []
        self.stack.enter_context(patch('hermes_state_runtime.list_session_admissions',
                                      side_effect=lambda *a, **kw: self.pending))
        self.receiver: Any = None

    def shutdown(self):
        if self.receiver:
            self.receiver.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    def start(self):
        self.receiver = wake.GatewayReceiver(self.ctx, self.home, self.home, self.db)
        return self.receiver

    def message(self, session='recipient', key='one'):
        receiver = self.receiver or self.start()
        row = receiver.mailbox.send(sender_profile='default', sender_session='sender',
                                    recipient_profile='default', recipient_session=session,
                                    body='UNTRUSTED BODY', key=key)
        receiver.wakes.request(row)
        return row

    def test_native_injection_exact_route_once_without_tui(self):
        row = self.message()
        self.receiver.tick()
        self.receiver.tick()
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call['session_key'], self.live.route)
        self.assertEqual(call['plugin_home'], self.home)
        self.assertIn(row['id'], call['content'])
        self.assertNotIn('UNTRUSTED BODY', call['content'])
        self.assertEqual(self.receiver.wakes.status(row['id'])['state'], 'queued')
        self.assertEqual(self.receiver.mailbox.status(row['id'])['status'], 'queued')

    def test_busy_queued_controls_compression_and_unknown_recipient_defer(self):
        row = self.message()
        self.message('not-owned', 'other')
        self.live.task = SimpleNamespace(done=lambda: False)
        self.receiver.tick()
        self.live.task = None
        self.live.controls.pending['approval'] = object()
        self.receiver.tick()
        self.live.controls.pending.clear()
        self.pending.append({'status': 'queued'})
        self.receiver.tick()
        self.pending.clear()
        self.loop.call_soon_threadsafe(lambda: None)
        async def locked_tick():
            async with self.live.claim_gate:
                self.receiver._tick_gateway()
        asyncio.run_coroutine_threadsafe(locked_tick(), self.loop).result(5)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.receiver.wakes.status(row['id'])['state'], 'pending')
        self.receiver.tick()
        self.assertEqual(len(self.calls), 1)

    def test_acknowledged_and_ambiguous_dispatch_never_repeat(self):
        row = self.message()
        self.receiver.mailbox.ack('default', 'recipient', [row['id']])
        self.receiver.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.receiver.wakes.status(row['id'])['state'], 'acknowledged')
        other = self.message(key='ambiguous')
        self.manager.inject_gateway_message = lambda **kw: (_ for _ in ()).throw(OSError('ambiguous'))
        with self.assertLogs('hermes_cli.plugins', level='WARNING'):
            self.receiver.tick()
        self.receiver.tick()
        self.assertEqual(self.receiver.wakes.status(other['id'])['state'], 'unknown')

    def test_both_runtimes_select_only_native_and_share_claim(self):
        row = self.message()
        server = SimpleNamespace(_methods={'prompt.submit': Mock()})
        with patch.dict(sys.modules, {'tui_gateway.server': server}):
            dispose = wake.start_receiver(self.home, self.home, self.db, ctx=self.ctx)
            self.assertIsNotNone(dispose)
            self.addCleanup(dispose)
            selected = wake._MONITORS[str(self.home.resolve())]
            self.assertIsInstance(selected, wake.GatewayReceiver)
            selected.tick()
            self.receiver.tick()
            self.assertEqual(len(self.calls), 1)
            server._methods['prompt.submit'].assert_not_called()
            self.assertFalse(self.receiver.wakes.claim(row['id'], 'competing-runtime'))

    def test_denied_native_permission_does_not_start(self):
        self.ctx._gateway_injection_allowed = lambda: False
        with patch.dict(sys.modules, {'tui_gateway.server': None}):
            self.assertIsNone(wake.start_receiver(self.home, self.home, self.db, ctx=self.ctx))
        self.assertFalse(self.db.exists())

    def test_wrong_profile_and_retiring_owner_defer(self):
        row = self.message()
        with patch('gateway.session_authorities.authority_for_home', return_value=None):
            self.receiver.tick()
        self.authority.retiring = True
        self.receiver.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.receiver.wakes.status(row['id'])['state'], 'pending')

    def test_plugin_bootstrap_uses_native_host_without_tui(self):
        self.manager.has_gateway_message_injector = False
        self.manager._gateway_message_injector = None
        spec = importlib.util.spec_from_file_location('mailbox_plugin_bootstrap_test', REPO / 'plugin.py')
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        self.ctx.get_config = lambda key: None
        self.ctx.register_skill = lambda *a, **kw: None
        self.ctx.register_hook = lambda *a, **kw: None
        disposers = []
        self.ctx.on_unload = disposers.append
        with patch.dict(sys.modules, {'tui_gateway.server': None}), \
                patch.object(plugin, 'get_hermes_home', return_value=self.home), \
                patch.object(plugin, 'register_tools'), \
                patch.object(plugin, '_local_module', return_value=wake):
            plugin.register(self.ctx)
        self.assertEqual(len(disposers), 1)
        self.addCleanup(disposers[0])
        self.assertIsInstance(wake._MONITORS[str(self.home.resolve())], wake.GatewayReceiver)

    def test_start_and_unload_without_tui_module(self):
        with patch.dict(sys.modules, {'tui_gateway.server': None}):
            dispose = wake.start_receiver(self.home, self.home, self.db, ctx=self.ctx)
            self.assertIsNotNone(dispose)
            self.assertIsNone(wake.start_receiver(self.home, self.home, self.db, ctx=self.ctx))
            receiver = wake._MONITORS[str(self.home.resolve())]
            dispose()
            self.assertFalse(receiver.thread.is_alive())
            self.assertNotIn(str(self.home.resolve()), wake._MONITORS)

    def test_cold_start_waits_for_host_without_creating_databases(self):
        self.manager.has_gateway_message_injector = False
        self.manager._gateway_message_injector = None
        with patch.dict(sys.modules, {'tui_gateway.server': None}):
            dispose = wake.start_receiver(self.home, self.home, self.db, ctx=self.ctx)
            self.assertIsNotNone(dispose)
            self.addCleanup(dispose)
            self.assertFalse(self.db.exists())
            receiver = wake._MONITORS[str(self.home.resolve())]
            self.manager.has_gateway_message_injector = True
            self.manager._gateway_message_injector = (self.host, None)
            self.receiver = receiver
            self.message()
            receiver.tick()
            self.assertEqual(len(self.calls), 1)

    def test_real_native_dispatch_refuses_route_rotated_to_another_local_session(self):
        from gateway.config import Platform
        from gateway.run_plugin_injection import GatewayPluginInjectionMixin
        class Host(GatewayPluginInjectionMixin):
            pass
        host: Any = Host()
        host._plugin_injection_accepting = lambda: True
        host.async_session_store = SimpleNamespace(lookup_by_session_key=None)
        source = SimpleNamespace(platform=Platform.LOCAL, chat_id='new-conversation')
        entry = SimpleNamespace(origin=source, session_key=self.live.route, session_id='new-conversation')
        async def lookup(key):
            return entry
        host.async_session_store.lookup_by_session_key = lookup
        host._restored_source = lambda entry: source
        with patch('gateway.session_identity.replace_source', side_effect=lambda source: source), \
                patch('gateway.session_authorities.authority_for_home', return_value=self.authority):
            with self.assertLogs('gateway.run', level='WARNING'):
                result = asyncio.run(host._dispatch_plugin_message_injection(
                    session_key=self.live.route, content='notice', plugin_id='session-mailbox-bundle',
                    plugin_home=self.home))
        self.assertFalse(result)

    def test_rotated_or_messaging_route_never_injects(self):
        row = self.message()
        self.live.source.chat_id = 'new-conversation'
        self.receiver.tick()
        self.live.source.chat_id = 'recipient'
        self.live.source.platform = __import__('gateway.config', fromlist=['Platform']).Platform.TELEGRAM
        self.receiver.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.receiver.wakes.status(row['id'])['state'], 'pending')


if __name__ == '__main__':
    unittest.main(verbosity=2)
