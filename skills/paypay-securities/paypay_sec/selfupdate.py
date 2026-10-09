"""`paypay self-update` — update the installed skill + bundled CLI in place.

Distribution is `npx skills add leeguooooo/paypay-securities` (a copy of the
skills/paypay-securities folder; the skills CLI records the folder's git tree
hash in ~/.agents/.skill-lock.json). This updater:

  * never logs in, never reads credentials, never touches ~/.paypay-sec
    (sessions, cache, snapshots, cron output all live there);
  * fetches the target ref from GitHub with git (objects are content-addressed,
    so the copied folder is checked against the target tree hash);
  * refuses to overwrite an install it cannot identify, or one with local edits
    (unless --force, which keeps a backup);
  * swaps the folder atomically, re-syncs the uv env, checks `--version` and
    offline `--help`, and restores the previous folder if anything fails.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_SLUG = "leeguooooo/paypay-securities"
REPO_URL = f"https://github.com/{REPO_SLUG}.git"
SKILL_NAME = "paypay-securities"
SKILL_SUBDIR = f"skills/{SKILL_NAME}"
MANIFEST = ".paypay-install.json"
SKILL_UPDATE_CMD = f"npx skills update {SKILL_NAME} -g -y"
REINSTALL_CMD = f"npx skills add {REPO_SLUG} --skill {SKILL_NAME} -g -y"

# Never part of the shipped tree: runtime env, bytecode, local secrets, our manifest.
_HASH_EXCLUDES = (".venv/", "__pycache__/", "*.pyc", ".env", MANIFEST)
# Of those, never carried into the new folder (regenerated).
_NO_CARRY = (".venv", "__pycache__")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class UpdateError(RuntimeError):
    pass


def skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _git(args, cwd=None, env=None, check=True) -> str:
    base = ["git", "-c", "core.autocrlf=false", "-c", "core.excludesFile=" + os.devnull,
            "-c", "advice.detachedHead=false"]
    res = subprocess.run(base + list(args), cwd=cwd, env=env, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def read_version(folder: Path) -> str | None:
    try:
        text = (folder / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else None


def local_tree(folder: Path) -> tuple[str, dict[str, str]]:
    """Git tree hash + {path: blob} of `folder`, excluding runtime/secret files.
    Uses a throwaway GIT_DIR, so nothing is written inside `folder`."""
    with tempfile.TemporaryDirectory(prefix="pp-hash-") as tmp:
        gitdir = Path(tmp) / "g"
        _git(["init", "-q", "--bare", str(gitdir)])
        (gitdir / "info").mkdir(exist_ok=True)
        (gitdir / "info" / "exclude").write_text("\n".join(_HASH_EXCLUDES) + "\n", encoding="utf-8")
        env = {**os.environ, "GIT_DIR": str(gitdir), "GIT_WORK_TREE": str(folder)}
        _git(["add", "-A", "--", "."], cwd=folder, env=env)
        tree = _git(["write-tree"], cwd=folder, env=env).strip()
        files = {}
        for line in _git(["ls-files", "-s"], cwd=folder, env=env).splitlines():
            meta, path = line.split("\t", 1)
            files[path] = meta.split()[1]
        return tree, files


def lock_path() -> Path:
    override = os.environ.get("PAYPAY_SELFUPDATE_LOCK")
    return Path(override).expanduser() if override else Path.home() / ".agents" / ".skill-lock.json"


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def detect_source(folder: Path, lock_file: Path | None = None) -> dict:
    """Identify who installed `folder`. Only a skills-CLI global install that the
    lock file vouches for is updated automatically."""
    folder = folder.resolve()
    if (folder.parent.parent / ".git").exists():
        return {"source": "git-checkout"}
    lock_file = lock_file or lock_path()
    lock = _read_lock(lock_file)
    entry = (lock or {}).get("skills", {}).get(SKILL_NAME) if isinstance(lock, dict) else None
    expected = (lock_file.parent / "skills" / SKILL_NAME)
    if (isinstance(entry, dict) and entry.get("source") == REPO_SLUG
            and expected.exists() and expected.resolve() == folder):
        return {"source": "skills-global", "lock": str(lock_file),
                "installed_tree": entry.get("skillFolderHash")}
    return {"source": "unknown"}


def _repo_url() -> str:
    return os.environ.get("PAYPAY_SELFUPDATE_REPO") or REPO_URL


def fetch_target(ref: str, workdir: Path) -> dict:
    """Shallow-fetch `ref` (branch, tag or full commit sha) into workdir/repo."""
    if not _REF_RE.match(ref) or ".." in ref:
        raise UpdateError(f"invalid ref: {ref!r}")
    repo = workdir / "repo"
    _git(["init", "-q", str(repo)])
    _git(["remote", "add", "origin", _repo_url()], cwd=repo)
    _git(["fetch", "-q", "--depth", "1", "origin", ref], cwd=repo)
    _git(["checkout", "-q", "FETCH_HEAD"], cwd=repo)
    commit = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    try:
        tree = _git(["rev-parse", f"HEAD:{SKILL_SUBDIR}"], cwd=repo).strip()
    except UpdateError:
        raise UpdateError(f"{ref} has no {SKILL_SUBDIR} folder") from None
    files = {}
    for line in _git(["ls-tree", "-r", f"HEAD:{SKILL_SUBDIR}"], cwd=repo).splitlines():
        meta, path = line.split("\t", 1)
        files[path] = meta.split()[2]
    folder = repo / SKILL_SUBDIR
    return {"ref": ref, "commit": commit, "tree": tree, "files": files,
            "folder": folder, "version": read_version(folder)}


def local_changes(folder: Path, installed_tree: str | None) -> tuple[bool, list[str], list[str]]:
    """(modified?, changed tracked files, extra untracked files) vs what was installed."""
    tree, files = local_tree(folder)
    manifest = _read_lock(folder / MANIFEST)
    if isinstance(manifest, dict) and isinstance(manifest.get("files"), dict):
        base = manifest["files"]
        changed = sorted(p for p, blob in base.items() if files.get(p) != blob and p != "uv.lock")
        extras = sorted(p for p in files if p not in base)
        return bool(changed), changed, extras
    if installed_tree and tree == installed_tree:
        return False, [], []
    # No manifest and the hash differs from what the skills CLI recorded:
    # something was edited, but we cannot say which file.
    return True, ["(unknown — folder differs from the installed version)"], []


def _carry_over(old: Path, new: Path, extras: list[str]) -> list[str]:
    """Copy local-only files (.env anywhere, plus user-added extras) into `new`."""
    carried = []
    for root, dirs, names in os.walk(old):
        dirs[:] = [d for d in dirs if d not in _NO_CARRY]
        if ".env" in names:
            carried.append(str((Path(root) / ".env").relative_to(old)))
    for rel in extras:
        if rel not in carried:
            carried.append(rel)
    for rel in carried:
        src, dst = old / rel, new / rel
        if dst.exists():
            continue  # never shadow a file the new version ships
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return carried


def _uv_verify(folder: Path) -> tuple[bool, str]:
    uv = shutil.which("uv")
    if not uv:
        return False, "uv is not on PATH"
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    sync = subprocess.run([uv, "sync", "--frozen", "--project", str(folder), "-q"],
                          env=env, capture_output=True, text=True)
    if sync.returncode != 0:
        return False, f"uv sync failed: {sync.stderr.strip()[-400:]}"
    off = {**env, "UV_OFFLINE": "1"}
    run = [uv, "run", "--frozen", "--offline", "--project", str(folder), "python", "-m", "paypay_sec"]
    helped = subprocess.run(run + ["--help"], env=off, capture_output=True, text=True)
    if helped.returncode != 0 or "usage: paypay" not in helped.stdout:
        return False, "offline `paypay --help` failed"
    ver = subprocess.run(run + ["--version"], env=off, capture_output=True, text=True)
    return True, ver.stdout.strip()


def _update_lock(lock_file: Path, tree: str) -> bool:
    lock = _read_lock(lock_file)
    entry = (lock or {}).get("skills", {}).get(SKILL_NAME) if isinstance(lock, dict) else None
    if not isinstance(entry, dict):
        return False
    entry["skillFolderHash"] = tree
    entry["updatedAt"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    tmp = lock_file.with_name(lock_file.name + ".paypay-tmp")
    tmp.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, lock_file)
    return True


def run_self_update(*, ref: str = "main", check: bool = False, yes: bool = False,
                    force: bool = False, folder: Path | None = None,
                    verify=_uv_verify, confirm=None) -> dict:
    folder = (folder or skill_dir()).resolve()
    src = detect_source(folder)
    result: dict = {
        "skill": SKILL_NAME, "install_dir": str(folder), "source": src["source"],
        "current": {"version": read_version(folder), "tree": None},
        "target": None, "result": None,
        "data_preserved": "~/.paypay-sec (credentials, sessions, cache, snapshots) is never touched",
        "skill_updated_with_cli": True,
    }
    with tempfile.TemporaryDirectory(prefix="pp-update-") as tmp:
        target = fetch_target(ref, Path(tmp))
        result["target"] = {k: target[k] for k in ("ref", "commit", "tree", "version")}
        cur_tree, _ = local_tree(folder)
        result["current"]["tree"] = cur_tree

        if src["source"] != "skills-global":
            result["result"] = "manual"
            result["message"] = (
                "Running from a git checkout: update with `git pull`."
                if src["source"] == "git-checkout" else
                "Could not confirm this folder was installed by `npx skills add -g`, so it was "
                f"left untouched. Update it the way you installed it, e.g. `{SKILL_UPDATE_CMD}` "
                f"or `{REINSTALL_CMD}`.")
            return result
        if cur_tree == target["tree"]:
            result["result"] = "up-to-date"
            return result

        modified, changed, extras = local_changes(folder, src.get("installed_tree"))
        result["local_changes"] = changed
        if modified and not force:
            result["result"] = "local-changes"
            result["message"] = ("The installed folder has local edits; nothing was changed. "
                                 "Re-run with --force to update anyway (a backup is kept).")
            return result
        if check:
            result["result"] = "update-available"
            return result
        if not yes:
            if confirm is None:
                raise UpdateError("confirmation required: re-run with --yes (or use --check to only look)")
            if not confirm(f"Update {SKILL_NAME} {result['current']['version']} ({cur_tree[:7]}) -> "
                           f"{target['version']} ({target['commit'][:7]})?"):
                result["result"] = "cancelled"
                return result

        stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
        staged = folder.parent / f".{SKILL_NAME}.update-{stamp}"
        backup = folder.parent / f".{SKILL_NAME}.backup-{stamp}"
        shutil.copytree(target["folder"], staged, symlinks=True)
        if local_tree(staged)[0] != target["tree"]:
            shutil.rmtree(staged, ignore_errors=True)
            raise UpdateError("copied files do not match the target tree hash; aborted, nothing changed")
        (staged / MANIFEST).write_text(json.dumps(
            {"repo": REPO_SLUG, "ref": ref, "commit": target["commit"], "tree": target["tree"],
             "files": target["files"]}, indent=2) + "\n", encoding="utf-8")
        result["carried_over"] = _carry_over(folder, staged, extras)

        os.replace(folder, backup)
        os.replace(staged, folder)
        ok, detail = verify(folder)
        if not ok:
            failed = folder.parent / f".{SKILL_NAME}.failed-{stamp}"
            os.replace(folder, failed)
            os.replace(backup, folder)
            shutil.rmtree(failed, ignore_errors=True)
            result["result"] = "rolled-back"
            result["message"] = f"post-update check failed ({detail}); restored the previous version"
            return result

        result["installed"] = {"version": read_version(folder), "cli_version": detail,
                               "commit": target["commit"], "tree": target["tree"]}
        result["lock_updated"] = _update_lock(Path(src["lock"]), target["tree"])
        if force and modified:
            result["backup"] = str(backup)
        else:
            shutil.rmtree(backup, ignore_errors=True)
        result["result"] = "updated"
        return result


def _tty_confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower().startswith("y")
    except EOFError:
        return False


def cmd_self_update(args) -> int:
    confirm = _tty_confirm if sys.stdin.isatty() else None
    try:
        res = run_self_update(ref=args.ref, check=args.check, yes=args.yes, force=args.force,
                              confirm=confirm)
    except UpdateError as e:
        if args.json:
            print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        cur, tgt = res["current"], res.get("target") or {}
        print(f"installed: {cur['version']} (tree {str(cur['tree'])[:7]})  {res['install_dir']}")
        if tgt:
            print(f"target:    {tgt.get('version')} ({tgt.get('ref')} @ {str(tgt.get('commit'))[:7]})")
        print(f"source:    {res['source']}")
        print(f"result:    {res['result']}")
        for f in res.get("local_changes") or []:
            print(f"  modified: {f}")
        if res.get("message"):
            print(res["message"])
        if res.get("backup"):
            print(f"previous version kept at {res['backup']}")
        print("The skill (SKILL.md) and the CLI ship as one folder and are updated together. "
              "~/.paypay-sec (credentials, sessions, cache, snapshots) is never touched.")
    return {"updated": 0, "up-to-date": 0, "update-available": 0, "manual": 0,
            "cancelled": 1, "local-changes": 3, "rolled-back": 1}.get(res["result"], 1)
