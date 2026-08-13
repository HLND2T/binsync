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
        "auto_clone": true          // optional; false prompts before cloning a missing repo
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

import ida_kernwin
import idaapi
import idc

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
    )

    def __init__(self, binary_path: pathlib.Path, data: dict):
        self.binary_path = binary_path
        self.sidecar_path = _find_sidecar(binary_path)
        self.user = data.get("user")
        self.remote = data.get("remote") or None
        self.expected_md5 = data.get("expected_md5")
        self.force_user = bool(data.get("force_user", False))
        self.auto_clone = bool(data.get("auto_clone", False))

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


def auto_recover(controller) -> bool:
    """Connect ``controller`` to the project described by the binary's sidecar.

    Returns True if a connection was attempted, False if there was nothing to do
    (no sidecar, md5 mismatch, or the user declined a clone).
    """
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

    repo = cfg.repo_path
    if repo.exists():
        _l.info("Auto-recover: connecting to existing project %s as %s", repo, user)
        # start_ui=False: the Qt UI-updater thread must not be created here —
        # database_inited fires before IDA's Qt event loop is fully up, and
        # creating a QThread there crashes Qt6Widgets. The control panel starts
        # the UI thread lazily when it is opened.
        controller.connect(user, str(repo), init_repo=False, remote_url=None, start_ui=False)
        return True

    # local repo missing
    if not cfg.remote:
        _l.warning("Auto-recover: %s does not exist and no remote is set; skipping.", repo)
        return False

    if not cfg.auto_clone:
        answer = ida_kernwin.ask_yn(
            ida_kernwin.ASKBTN_YES | ida_kernwin.ASKBTN_NO,
            "HIDECANCEL\nBinSync project is missing. Clone it now?\n%s\nfrom\n%s" % (repo, cfg.remote),
        )
        if answer != 1:  # 1 == ASKBTN_YES
            _l.info("Auto-recover: user declined to clone %s.", repo)
            return False

    _l.info("Auto-recover: cloning %s from %s", repo, cfg.remote)
    controller.connect(user, str(repo), init_repo=False, remote_url=cfg.remote, start_ui=False)
    return True


class AutoRecoverHook(idaapi.UI_Hooks):
    """Fires auto_recover() whenever IDA finishes initializing a database.

    ``database_inited`` fires for *both* freshly-loaded input files and reopened
    databases. The IDB ``load_file``/``loader_finished`` event only runs when the
    external file loader runs, so it is skipped when IDA restores an existing
    .i64/.idb — which is why a reopened database was never auto-recovered.
    """

    def __init__(self, controller):
        idaapi.UI_Hooks.__init__(self)
        self.controller = controller
        self._attempted = set()

    def database_inited(self, is_new_database, idc_script):
        return self._on_file_loaded()

    def _on_file_loaded(self):
        if os.environ.get("BINSYNC_AUTO_RECOVER", "1") == "0":
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
