"""self-update against a throwaway local git repo — no network, no account data."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from _runner import run
from paypay_sec import selfupdate as su


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


def _write_version(repo: Path, version: str, extra: str = ""):
    d = repo / su.SKILL_SUBDIR
    (d / "paypay_sec").mkdir(parents=True, exist_ok=True)
    (d / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    (d / "SKILL.md").write_text(f"skill {version}\n{extra}")
    (d / "paypay_sec" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", version)


def _setup():
    """origin repo at v1, a skills-CLI style global install of v1, then origin moves to v2."""
    root = Path(tempfile.mkdtemp(prefix="pp_su_"))
    repo = root / "origin"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write_version(repo, "0.1.0")
    agents = root / "agents"
    install = agents / "skills" / su.SKILL_NAME
    shutil.copytree(repo / su.SKILL_SUBDIR, install)
    tree = subprocess.run(["git", "rev-parse", f"HEAD:{su.SKILL_SUBDIR}"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    lock = agents / ".skill-lock.json"
    lock.write_text(json.dumps({"version": 3, "skills": {su.SKILL_NAME: {
        "source": su.REPO_SLUG, "sourceType": "github", "skillFolderHash": tree}}}))
    _write_version(repo, "0.2.0", "new line\n")
    os.environ["PAYPAY_SELFUPDATE_REPO"] = str(repo)
    os.environ["PAYPAY_SELFUPDATE_LOCK"] = str(lock)
    return root, install, lock


def _ok(folder):
    return True, "paypay test"


def test_installed_tree_matches_lock_hash():
    _, install, lock = _setup()
    assert su.local_tree(install)[0] == json.loads(lock.read_text())["skills"][su.SKILL_NAME]["skillFolderHash"]


def test_check_reports_without_changing_anything():
    _, install, _ = _setup()
    before = su.local_tree(install)[0]
    res = su.run_self_update(check=True, folder=install, verify=_ok)
    assert res["result"] == "update-available", res
    assert res["current"]["version"] == "0.1.0" and res["target"]["version"] == "0.2.0"
    assert su.local_tree(install)[0] == before


def test_requires_yes_without_tty():
    _, install, _ = _setup()
    try:
        su.run_self_update(folder=install, verify=_ok)
    except su.UpdateError as e:
        assert "--yes" in str(e)
    else:
        raise AssertionError("expected UpdateError")


def test_update_swaps_folder_keeps_env_and_updates_lock():
    root, install, lock = _setup()
    (install / ".env").write_text("PAYPAY_MEMBER_ID=x\n")
    res = su.run_self_update(yes=True, folder=install, verify=_ok)
    assert res["result"] == "updated", res
    assert su.read_version(install) == "0.2.0"
    assert (install / ".env").read_text() == "PAYPAY_MEMBER_ID=x\n"
    assert (install / su.MANIFEST).exists()
    assert json.loads(lock.read_text())["skills"][su.SKILL_NAME]["skillFolderHash"] == res["target"]["tree"]
    assert not [p for p in install.parent.iterdir() if p.name.startswith(".")], "staging/backup left behind"
    again = su.run_self_update(check=True, folder=install, verify=_ok)
    assert again["result"] == "up-to-date", again


def test_pin_to_older_commit():
    root, install, _ = _setup()
    su.run_self_update(yes=True, folder=install, verify=_ok)
    old = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=root / "origin",
                         capture_output=True, text=True, check=True).stdout.strip()
    res = su.run_self_update(ref=old, yes=True, folder=install, verify=_ok)
    assert res["result"] == "updated" and su.read_version(install) == "0.1.0", res


def test_local_edits_block_update_unless_forced():
    _, install, _ = _setup()
    (install / "SKILL.md").write_text("my custom notes\n")
    res = su.run_self_update(yes=True, folder=install, verify=_ok)
    assert res["result"] == "local-changes", res
    assert (install / "SKILL.md").read_text() == "my custom notes\n"
    res = su.run_self_update(yes=True, force=True, folder=install, verify=_ok)
    assert res["result"] == "updated" and Path(res["backup"], "SKILL.md").read_text() == "my custom notes\n"


def test_manifest_pinpoints_edited_file_and_keeps_user_files():
    _, install, _ = _setup()
    su.run_self_update(yes=True, folder=install, verify=_ok)
    (install / "my-notes.md").write_text("mine\n")
    changed = su.local_changes(install, None)
    assert changed == (False, [], ["my-notes.md"]), changed
    (install / "SKILL.md").write_text("edited\n")
    assert su.local_changes(install, None)[1] == ["SKILL.md"]


def test_failed_verification_restores_previous_version():
    _, install, _ = _setup()
    res = su.run_self_update(yes=True, folder=install, verify=lambda f: (False, "boom"))
    assert res["result"] == "rolled-back", res
    assert su.read_version(install) == "0.1.0"
    assert not [p for p in install.parent.iterdir() if p.name.startswith(".")]


def test_unknown_install_is_left_alone():
    root, install, _ = _setup()
    other = root / "elsewhere" / su.SKILL_NAME
    shutil.copytree(install, other)
    res = su.run_self_update(yes=True, folder=other, verify=_ok)
    assert res["result"] == "manual", res
    assert su.read_version(other) == "0.1.0"


def test_rejects_bad_ref():
    _, install, _ = _setup()
    try:
        su.run_self_update(ref="--upload-pack=x", check=True, folder=install, verify=_ok)
    except su.UpdateError:
        pass
    else:
        raise AssertionError("expected UpdateError")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
