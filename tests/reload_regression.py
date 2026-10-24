"""Same-process component upgrade regression; only temporary fixture files."""
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location('mailbox_reload_test', REPO / 'plugin.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReloadTests(unittest.TestCase):
    def test_same_process_upgrade_does_not_reuse_old_component(self):
        scratch = os.environ.get('TMPDIR')
        if not scratch and os.environ.get('HERMES_HOME'):
            scratch = str(Path(os.environ['HERMES_HOME']) / 'cache' / 'scratch')
        if scratch:
            Path(scratch).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='mailbox-reload-', dir=scratch) as tmp:
            root = Path(tmp).resolve()
            source = root / 'wake_receiver.py'
            old = 'def start_receiver(home, root, db):\n    return "old"\n'
            new = 'def start_receiver(home, root, db, *, ctx=None):\n    return ctx\n'
            source.write_text(old, encoding='utf-8')
            # This is the exact path-only key used by the installed old plugin.
            prefix = '_hermes_wake_receiver_'
            key = prefix + hashlib.sha256(str(source).encode()).hexdigest()[:16]
            spec = importlib.util.spec_from_file_location(key, source)
            assert spec is not None and spec.loader is not None
            cached = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cached)
            sys.modules[key] = cached
            try:
                plugin = load_plugin()
                source.write_text(new, encoding='utf-8')
                current = plugin._local_module(root, 'wake_receiver', prefix)
                self.assertIsNot(current, cached, 'upgrade reused the old module by path')
                self.assertEqual(current.start_receiver(root, root, None, ctx='new'), 'new')
                self.assertIs(current, plugin._local_module(root, 'wake_receiver', prefix))
                self.assertEqual(cached.start_receiver(root, root, None), 'old')
            finally:
                for name, module in list(sys.modules.items()):
                    if getattr(module, '__file__', None) == str(source):
                        sys.modules.pop(name, None)


    def test_store_upgrade_same_size_and_timestamp(self):
        # The plugin owns the only loader; the receiver gets stores injected.
        plugin = load_plugin()
        prefix = '_hermes_mailbox_'
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp).resolve()
            source = root / 'mailbox_store.py'
            source.write_text('def version(): return "old"\n', encoding='utf-8')
            stamp = source.stat()
            try:
                old = plugin._local_module(root, 'mailbox_store', prefix)
                self.assertEqual(old.version(), 'old')
                source.write_text('def version(): return "new"\n', encoding='utf-8')
                os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(source.stat().st_size, stamp.st_size)
                current = plugin._local_module(root, 'mailbox_store', prefix)
                self.assertEqual(current.version(), 'new')
                self.assertIsNot(current, old)
                self.assertIs(current, plugin._local_module(root, 'mailbox_store', prefix))
                self.assertEqual(old.version(), 'old')
            finally:
                for key, module in list(sys.modules.items()):
                    if getattr(module, '__file__', None) == str(source):
                        sys.modules.pop(key, None)

    def test_failed_import_retries_identical_bytes(self):
        from unittest.mock import patch
        plugin = load_plugin()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp).resolve()
            source = root / 'mailbox_store.py'
            source.write_text('import os\nif os.environ.get("MAILBOX_TEST_IMPORT_FAIL"): raise RuntimeError("fixture failure")\nready = True\n', encoding='utf-8')
            with patch.dict(os.environ, {'MAILBOX_TEST_IMPORT_FAIL': 'yes'}):
                with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                    plugin._local_module(root, 'mailbox_store', '_hermes_mailbox_')
            self.assertFalse(any(getattr(m, '__file__', None) == str(source) for m in sys.modules.values()))
            current = plugin._local_module(root, 'mailbox_store', '_hermes_mailbox_')
            self.assertTrue(current.ready)
            self.assertIs(current, plugin._local_module(root, 'mailbox_store', '_hermes_mailbox_'))
            for key, module in list(sys.modules.items()):
                if getattr(module, '__file__', None) == str(source):
                    sys.modules.pop(key, None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
