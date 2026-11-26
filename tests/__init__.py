"""The whole suite runs against a throwaway Hermes home, set before any test module imports the runtime.

Runtime modules compute paths (state dir, logs) when they are imported, from the active Hermes home. Without this,
a module imported at the top of a test file outside a test's own isolation would read or write the real profile of
whoever runs the tests. Tests still isolate themselves further (OfflineTestCase, patched phconf functions).
"""
import atexit
import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix="proactive-hermes-tests-")
os.environ["HERMES_HOME"] = os.path.join(_ROOT, "hermes-home")
atexit.register(shutil.rmtree, _ROOT, True)
