"""Shared fixture for the Hermes interface tests.

Import this module before any Hermes module. It points HOME, USERPROFILE,
LOCALAPPDATA and HERMES_HOME at one fresh temporary tree, so the Hermes CLI,
its package-manager runtime and every in-process call write only there.

Why HOME/LOCALAPPDATA too: Hermes treats any HERMES_HOME under the platform
default root (~/.hermes or %LOCALAPPDATA%/hermes) as that root, and its package
manager publishes launchers and runtimes under the platform default root. A
temporary HERMES_HOME alone is therefore not isolated.

Import also fails closed (IsolationError) before anything is written when the Hermes checkout,
the venv or the fixture base lies inside a real Hermes data root recorded
from the ORIGINAL environment, or when the Hermes package manager would borrow another root's
installs/ or tools/ (pm.environments.owning_home_root / store_root read neither HOME nor
LOCALAPPDATA, so redirecting the environment alone cannot prevent that).
"""
from __future__ import annotations

import atexit
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
PLUGIN_KEY = "session-mailbox-bundle"
TOOLSET = "session-mailbox"
TOOLS = ("session_mailbox_list_sessions", "session_mailbox_send", "session_mailbox_inbox",
         "session_mailbox_ack", "session_mailbox_message_status")
HOOKS = ("pre_llm_call", "transform_tool_result", "on_session_start")
SKILL = "hermes-peer-session-collaboration"
QUALIFIED_SKILL = f"{PLUGIN_KEY}:{SKILL}"
PROFILE = "alpha"
SID_DEFAULT = "ci-session-default"
SID_PROFILE = "ci-session-alpha"


def rmtree_force(path):
    """Remove a tree, clearing read-only bits (git objects on Windows), and assert it is gone."""
    path = Path(path)

    def clear_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        func(target)

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=clear_and_retry)
        else:
            shutil.rmtree(path, onerror=clear_and_retry)
    if path.exists():
        raise AssertionError(f"fixture tree was not removed: {path}")


class IsolationError(AssertionError):
    """Raised at import time when the fixture could reach a real Hermes data root."""


def _norm(path):
    return os.path.normcase(os.path.abspath(os.path.realpath(path)))


def _under(path, root):
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _real_hermes_roots(environ):
    """Real Hermes data roots named by the ORIGINAL environment, computed without importing Hermes.

    Mirrors hermes_constants._get_platform_default_hermes_home (LOCALAPPDATA on Windows, else
    HOME/~) and get_default_hermes_root (a HERMES_HOME outside that default is its own root;
    <root>/profiles/<name> belongs to <root>).
    """
    suffix = environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if os.name == "nt":
        local = environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path(environ.get("USERPROFILE") or Path.home()) / "AppData" / "Local"
        roots = [base / ("hermes" + suffix)]
    else:
        roots = [Path(environ.get("HOME") or Path.home()) / (".hermes" + suffix)]
    home = environ.get("HERMES_HOME", "").strip()
    if home:
        home = Path(os.path.expanduser(home))
        if not _under(home, roots[0]):
            roots.append(home.parent.parent if home.parent.name == "profiles" else home)
    return [Path(_norm(root)) for root in roots]


def _refuse(problems):
    raise IsolationError(
        "REFUSING TO RUN: this test setup could read or write a real Hermes data directory.\n  - "
        + "\n  - ".join(problems)
        + "\nRun the tests from a dedicated hermes-agent clone and venv OUTSIDE your Hermes data directory "
          "(~/.hermes, %LOCALAPPDATA%\\hermes, or $HERMES_HOME), with TMPDIR/TEMP outside it too. "
          "See README.md, 'Running the tests locally'.")


def _check_original_layout():
    """Before any redirect or write: nothing this run uses may live inside a real Hermes root."""
    import importlib.util
    problems = []
    try:
        constants = importlib.util.find_spec("hermes_constants")
    except (ImportError, ValueError):
        constants = None
    if constants is None or not constants.origin:
        problems.append("hermes_constants is not importable; install Hermes into this interpreter")
    source = Path(constants.origin).parent if constants is not None and constants.origin else None
    checks = [("Hermes source checkout (hermes_constants)", source),
              ("Python environment (sys.prefix)", Path(sys.prefix)),
              ("fixture base (SMB_CI_BASE or the temp dir)", Path(os.environ.get("SMB_CI_BASE") or tempfile.gettempdir()))]
    for label, path in checks:
        for root in REAL_ROOTS:
            if path is not None and _under(path, root):
                problems.append(f"{label} {path} is inside the real Hermes root {root}")
    if problems:
        _refuse(problems)


def _check_package_manager():
    """After the redirect: the package manager must not borrow another root's installs/ or tools/."""
    problems = []
    try:
        from pm.environments import owning_home_root, store_root
        from pm.paths import repo_root
    except ImportError as exc:
        _refuse([f"cannot import the Hermes package-manager path helpers ({exc!r}); upstream changed "
                 "pm.environments.owning_home_root/store_root or pm.paths.repo_root"])
    checkout = Path(repo_root())
    owner = owning_home_root(checkout)
    if owner is not None:
        problems.append(f"pm.environments.owning_home_root({checkout}) = {owner}: the package manager would "
                        "borrow that root's installs/ and tools/ instead of the fixture")
    store = store_root(checkout, honor_runtime_override=False)
    for root in REAL_ROOTS:
        if _under(store, root):
            problems.append(f"pm.environments.store_root({checkout}) = {store} is inside the real Hermes root {root} "
                            "(the checkout's install stamp points there)")
    if not _under(store, ROOT) and not _under(store, checkout):
        problems.append(f"pm.environments.store_root({checkout}) = {store} is outside the fixture {ROOT}")
    if problems:
        _refuse(problems)


# 1. Record the real roots from the untouched environment and refuse before creating anything.
REAL_ROOTS = _real_hermes_roots(dict(os.environ))
_check_original_layout()

# 2. Create the fixture and redirect every root Hermes, its package manager, uv and pip derive paths from.
_OWN_BASE = not os.environ.get("SMB_CI_BASE")
BASE = Path(os.environ.get("SMB_CI_BASE") or tempfile.mkdtemp(prefix="smb-ci-")).resolve()
USER_HOME = BASE / "user"
LOCAL_APPDATA = USER_HOME / "AppData" / "Local"
ROAMING_APPDATA = USER_HOME / "AppData" / "Roaming"
ROOT = (LOCAL_APPDATA / "hermes") if os.name == "nt" else (USER_HOME / ".hermes")
PROFILE_HOME = ROOT / "profiles" / PROFILE
LOCAL_APPDATA.mkdir(parents=True)
ROAMING_APPDATA.mkdir(parents=True)
if _OWN_BASE:
    # Run without tests/run.py (python -m unittest ...): remove the fixture at exit, best effort.
    # tests/run.py instead removes it from the parent process and asserts removal.
    def _remove_own_base():
        try:
            rmtree_force(BASE)
        except Exception as exc:  # Windows may still hold a handle during interpreter shutdown
            print(f"fixture cleanup failed, remove {BASE} by hand: {exc!r}", file=sys.stderr)

    atexit.register(_remove_own_base)
for _name in [name for name in os.environ if name.startswith(("HERMES_", "XDG_", "UV_", "PIP_"))]:
    os.environ.pop(_name)
os.environ.update(HOME=str(USER_HOME), USERPROFILE=str(USER_HOME), LOCALAPPDATA=str(LOCAL_APPDATA),
                  APPDATA=str(ROAMING_APPDATA), HERMES_HOME=str(ROOT), PYTHONIOENCODING="utf-8",
                  PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1", OPENAI_API_KEY="", OPENROUTER_API_KEY="",
                  ANTHROPIC_API_KEY="")
sys.dont_write_bytecode = True

# 3. With the redirect in place, prove the resolvers point at the fixture before any test writes.
_check_package_manager()


def hermes_executable():
    folder = Path(sys.executable).parent
    for name in ("hermes.exe", "hermes"):
        if (folder / name).is_file():
            return folder / name
    raise AssertionError(f"no hermes entry point next to {sys.executable}; install Hermes into this interpreter")


def check_isolation():
    """Fail before any write if Hermes would resolve the real user's data root."""
    from hermes_constants import get_default_hermes_root, get_hermes_home
    assert Path(get_hermes_home()).resolve() == ROOT, \
        f"hermes_constants.get_hermes_home() resolved {get_hermes_home()}, expected the fixture {ROOT}"
    assert Path(get_default_hermes_root()).resolve() == ROOT, \
        f"hermes_constants.get_default_hermes_root() resolved {get_default_hermes_root()}, expected {ROOT}"
    _check_package_manager()


def run_hermes(*args, timeout=900):
    """Run the real Hermes CLI non-interactively; stdin is closed so any prompt fails fast."""
    return subprocess.run([str(hermes_executable()), *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, env=dict(os.environ))


_INSTALL = {}


def ensure_installed():
    """Install the plugin from this checkout into the default home and the named profile, once."""
    if _INSTALL:
        failed = [name for name, result in _INSTALL.items() if result.returncode]
        if failed:
            raise AssertionError(f"earlier install step failed: {failed}")
        return _INSTALL
    check_isolation()
    version = run_hermes("--version", timeout=120)
    print("\n" + (version.stdout or version.stderr).strip().splitlines()[0], file=sys.stderr, flush=True)
    # Not REPO.as_uri(): Hermes passes the URL to git, and on Windows git rejects the RFC 8089 form
    # file:///C:/... as "/C:/...". file://C:/... (Windows) and file:///home/... (POSIX) both work.
    source = "file://" + REPO.as_posix()
    steps = (
        ("install-default", ("plugins", "install", source, "--enable")),
        ("profile-create", ("profile", "create", PROFILE, "--no-alias", "--no-skills")),
        ("install-profile", ("-p", PROFILE, "plugins", "install", source, "--enable")),
    )
    for name, args in steps:
        result = run_hermes(*args)
        _INSTALL[name] = result
        if result.returncode:
            raise AssertionError(f"`hermes {' '.join(args)}` exited {result.returncode}\n"
                                 f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    seed_sessions()
    return _INSTALL


def seed_sessions():
    """Create one stored session per home through the native SessionDB."""
    from hermes_state import SessionDB
    for home, session_id in ((ROOT, SID_DEFAULT), (PROFILE_HOME, SID_PROFILE)):
        with closing(SessionDB(home / "state.db")) as db:
            db.create_session(session_id, source="cli")


def read_yaml(path):
    from ruamel.yaml import YAML
    with open(path, encoding="utf-8") as handle:
        return YAML(typ="safe").load(handle) or {}


def dispatch_in(home, calls, timeout=300):
    """Run tool calls through the native registry in a fresh Hermes process for *home*."""
    env = dict(os.environ, HERMES_HOME=str(home))
    script = Path(__file__).with_name("_hermes_dispatch.py")
    result = subprocess.run([sys.executable, "-B", str(script)], input=json.dumps(calls), capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    marker = [line for line in result.stdout.splitlines() if line.startswith("@@RESULT@@")]
    if result.returncode or not marker:
        raise AssertionError(f"dispatch helper failed in {home} (exit {result.returncode})\n"
                             f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return json.loads(marker[-1][len("@@RESULT@@"):])
