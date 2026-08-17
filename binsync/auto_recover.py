"""Automatic BinSync project recovery on IDA file load.

When IDA loads a binary (e.g. ``engine2.dll``), this hook looks for a sidecar
JSON file named ``<binary>.binsync.json`` in the same directory as the binary
(e.g. ``engine2.dll.binsync.json``). If present, it connects the BinSync
controller to the described project, recovering the local ``.bsproj`` repo from
its git remote when the directory was deleted.

Sidecar schema::

    {
        "user": "HZDEV",            // optional; fallback when the OS user can't be resolved
        "remote": "https://...",    // remote to clone from when the local repo is missing
        "repo_path": null,          // optional; absolute or sidecar-relative path,
                                    //   default is <binary>.bsproj (same dir as the binary)
        "expected_md5": "...",      // optional; skip if it doesn't match the loaded binary's md5
        "force_user": false,        // optional; use "user" verbatim instead of the current OS user
        "auto_clone": true,         // optional; false prompts before cloning a missing repo
        "auto_sync_all": false      // optional; pull in all teammates' data automatically
                                    //   on the first-ever sync for this user + binary
    }

The clone/connect decision never depends on ``user``: the repo is cloned whenever
the local directory is missing and ``remote`` is set, regardless of which user
field is present. The connected user defaults to the current OS user (so each
team member lands on their own ``binsync/<user>`` branch); set ``force_user`` to
pin a shared identity.
"""

import getpass
import json
import logging
import os
import pathlib

import git

# IDA-only modules are imported lazily inside the functions that use them so this
# module stays importable in test/headless environments. AutoRecoverHook needs an
# idaapi.UI_Hooks base class, so fall back to a plain object when IDA is absent.
try:
    import ida_kernwin
except ImportError:
    ida_kernwin = None

try:
    import idaapi
except ImportError:
    idaapi = None

try:
    import idc
except ImportError:
    idc = None

_l = logging.getLogger(__name__)


def _find_sidecar(binary_path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(str(binary_path) + ".binsync.json")


class AutoRecoverConfig:
    __slots__ = (
        "sidecar_path",
        "binary_path",
        "repo_path",
        "user",
        "remote",
        "expected_md5",
        "force_user",
        "auto_clone",
        "auto_sync_all",
    )

    def __init__(self, binary_path: pathlib.Path, data: dict):
        self.binary_path = binary_path
        self.sidecar_path = _find_sidecar(binary_path)
        self.user = data.get("user")
        self.remote = data.get("remote") or None
        self.expected_md5 = data.get("expected_md5")
        self.force_user = bool(data.get("force_user", False))
        self.auto_clone = bool(data.get("auto_clone", False))
        self.auto_sync_all = bool(data.get("auto_sync_all", False))

        raw_repo = data.get("repo_path")
        if raw_repo:
            repo_path = pathlib.Path(raw_repo)
            if not repo_path.is_absolute():
                repo_path = binary_path.parent / repo_path
            self.repo_path = repo_path
        else:
            # default mirrors the UI's speculation: <binary>.bsproj next to the binary
            self.repo_path = binary_path.with_suffix(".bsproj")


def _resolve_user(cfg: AutoRecoverConfig) -> str:
    if cfg.force_user and cfg.user:
        return cfg.user

    try:
        os_user = getpass.getuser()
    except Exception:
        os_user = None

    return os_user or cfg.user or "user"


def _is_first_sync(client) -> bool:
    """True if the master user has no real artifacts committed for this binary yet.

    The master user's branch is ``binsync/<user>`` and is created from the shared
    ``binsync/__root__`` branch (see Client._get_or_init_user_branch). That root branch
    already carries the "Root commit" that initialized the repo, so iter_commits on the
    user branch *includes* that ancestor — we must only count the commits the user added
    *on top of* the root branch, i.e. ``binsync/__root__..binsync/<user>``.

    As long as that set is empty (branch not created yet) or carries only the updater
    thread's first ``commit_and_update_states("User created")`` (see
    controller.updater_routine), the user has produced no artifacts for this binary and
    this is their first-ever sync — the right moment to auto ``sync_all`` teammates'
    data.

    This is a pure git-history check: no state file to keep in sync, works across
    machines/sessions, and is naturally scoped per (user, binary).
    """
    from binsync.core.client import BINSYNC_ROOT_BRANCH

    master_branch = f"binsync/{client.master_user}"
    try:
        # only commits reachable from the user branch but NOT from the root branch
        commits = list(client.repo.iter_commits(f"{BINSYNC_ROOT_BRANCH}..{master_branch}"))
    except git.GitCommandError:
        # the user branch (or root branch) does not exist yet (brand-new repo/user) —
        # there is nothing on top of root, so this is the very first sync
        return True
    except Exception:
        return False
    return len(commits) == 0 or (len(commits) == 1 and commits[0].message.strip() == "User created")


def _maybe_schedule_auto_sync_all(controller) -> None:
    """Schedule a first-time sync_all on the worker thread, if configured and applicable.

    Runs inside the connect() path of auto_recover, synchronously right after
    controller.connect() returns. The updater thread's first "User created" commit
    may or may not have happened yet; either way _is_first_sync stays correct because
    it only returns True while the master branch carries no more than that single
    initial commit (or no branch at all).

    sync_all(user=X) fills data *from* user X (see controller.sync_all), so a first-time
    sync must pull from every non-master user, not the master user itself. Each sync_all
    must NOT run inline here: database_inited fires before IDA's Qt event loop is up, and
    the fill_* calls touch the GUI. schedule_job dispatches to the push scheduler's
    worker thread instead.
    """
    if controller.headless:
        return
    if not controller.client:
        return
    if not _is_first_sync(controller.client):
        _l.debug("Auto-sync-all: master user already has artifacts, skipping first-time sync.")
        return

    master_user = controller.client.master_user
    try:
        other_users = [u for u in controller.usernames() if u != master_user]
    except Exception:
        _l.exception("Auto-sync-all: failed to enumerate users, skipping.")
        return

    if not other_users:
        _l.info("Auto-sync-all: no other users in the project yet, nothing to sync.")
        return

    _l.info("Auto-sync-all: first sync for %s, scheduling sync_all from %s...",
            master_user, other_users)
    # NOTE: schedule_job dispatches to the push scheduler's worker thread, which runs
    # under the same client git locks as the updater thread's pull/push, so concurrent
    # execution is safe (this is the same path the activity table's "Sync-All" uses).
    # If a user branch was not yet fetched, get_state() checkout fails silently and that
    # user's data is simply skipped for this first sync — the user can still Sync manually.
    for user in other_users:
        controller.schedule_job(controller.sync_all, user=user)


def auto_recover(controller) -> bool:
    """Connect ``controller`` to the project described by the binary's sidecar.

    Returns True if a connection was attempted, False if there was nothing to do
    (no sidecar, md5 mismatch, or the user declined a clone).
    """
    if idc is None:
        _l.debug("Auto-recover: IDA modules unavailable (non-IDA environment), skipping.")
        return False

    input_path = idc.get_input_file_path()
    if not input_path:
        _l.debug("Auto-recover: no input file path yet, skipping.")
        return False

    binary_path = pathlib.Path(input_path)
    sidecar = _find_sidecar(binary_path)
    if not sidecar.exists():
        return False

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _l.warning("Auto-recover: failed to read %s: %s", sidecar, e)
        return False

    cfg = AutoRecoverConfig(binary_path, data)
    user = _resolve_user(cfg)

    # optional md5 sanity check
    if cfg.expected_md5:
        md5_buf = idc.retrieve_input_file_md5()
        if md5_buf is not None:
            current_md5 = md5_buf.hex()
            if current_md5 != cfg.expected_md5:
                _l.warning(
                    "Auto-recover: %s md5 mismatch (expected %s, got %s); skipping.",
                    binary_path.name, cfg.expected_md5, current_md5,
                )
                return False

    def _connect_and_maybe_sync():
        # start_ui=False: the Qt UI-updater thread must not be created here —
        # database_inited fires before IDA's Qt event loop is fully up, and
        # creating a QThread there crashes Qt6Widgets. The control panel starts
        # the UI thread lazily when it is opened.
        controller.connect(user, str(repo), init_repo=False, remote_url=remote_url, start_ui=False)
        if cfg.auto_sync_all:
            _maybe_schedule_auto_sync_all(controller)

    repo = cfg.repo_path
    if repo.exists():
        _l.info("Auto-recover: connecting to existing project %s as %s", repo, user)
        remote_url = None
        _connect_and_maybe_sync()
        return True

    # local repo missing
    if not cfg.remote:
        _l.warning("Auto-recover: %s does not exist and no remote is set; skipping.", repo)
        return False

    if not cfg.auto_clone:
        if ida_kernwin is None:
            _l.warning("Auto-recover: %s does not exist and IDA dialog module unavailable; skipping.", repo)
            return False
        answer = ida_kernwin.ask_yn(
            ida_kernwin.ASKBTN_YES | ida_kernwin.ASKBTN_NO,
            "HIDECANCEL\nBinSync project is missing. Clone it now?\n%s\nfrom\n%s" % (repo, cfg.remote),
        )
        if answer != 1:  # 1 == ASKBTN_YES
            _l.info("Auto-recover: user declined to clone %s.", repo)
            return False

    _l.info("Auto-recover: cloning %s from %s", repo, cfg.remote)
    remote_url = cfg.remote
    _connect_and_maybe_sync()
    return True


class AutoRecoverHook(idaapi.UI_Hooks if idaapi is not None else object):
    """Fires auto_recover() whenever IDA finishes initializing a database.

    ``database_inited`` fires for *both* freshly-loaded input files and reopened
    databases. The IDB ``load_file``/``loader_finished`` event only runs when the
    external file loader runs, so it is skipped when IDA restores an existing
    .i64/.idb — which is why a reopened database was never auto-recovered.
    """

    def __init__(self, controller):
        if idaapi is not None:
            idaapi.UI_Hooks.__init__(self)
        self.controller = controller
        self._attempted = set()

    def database_inited(self, is_new_database, idc_script):
        return self._on_file_loaded()

    def _on_file_loaded(self):
        if os.environ.get("BINSYNC_AUTO_RECOVER", "1") == "0":
            return 0

        if idc is None:
            return 0

        md5_buf = idc.retrieve_input_file_md5()
        if md5_buf is None:
            return 0

        md5 = md5_buf.hex()
        # Don't re-connect for the same binary in a single session.
        if md5 in self._attempted:
            return 0

        if auto_recover(self.controller):
            self._attempted.add(md5)

        return 0
