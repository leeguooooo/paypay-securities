"""doctor --fix: drops stale auth cookies from PAYPAY_COOKIE (with a backup)
and tightens .env permissions. Synthetic files only; never touches the Keychain."""
import contextlib
import io
import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _runner import run
from paypay_sec import cli, client, config, passkey_login as pk

_COOKIE = ('"123_SMS_AUTH_STRING=tok; laravel_session=old; '
           '__Secure-client-auth-state-prod=stale; _ga=1"')


@contextlib.contextmanager
def _env(mode=0o644):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = root / ".env"
        env.write_text(f"PAYPAY_MEMBER_ID=m\nPAYPAY_PASSWORD=p\nPAYPAY_COOKIE={_COOKIE}\n")
        env.chmod(mode)
        with patch.dict(os.environ, {}, clear=True), patch.object(config, "HOME", root), \
                patch.object(config, "env_file_for", return_value=env), \
                patch.object(client, "state_dir", return_value=root), \
                patch.object(pk, "passkey_status") as kc:
            yield env, kc


def _doctor(**kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.cmd_doctor(None, SimpleNamespace(account="default", online=False, json=True, **kw))
    return json.loads(out.getvalue()), rc


def test_warns_without_fix_and_leaves_file_alone():
    with _env() as (env, kc):
        before = env.read_text()
        out, _ = _doctor(fix=False)
        codes = {w["code"] for w in out["warnings"]}
        assert {"STALE_AUTH_COOKIE", "ENV_PERMISSIONS"} <= codes, codes
        assert env.read_text() == before
        kc.assert_not_called()           # offline doctor never touches the Keychain


def test_fix_strips_cookies_keeps_backup_and_chmods():
    with _env() as (env, kc):
        out, _ = _doctor(fix=True)
        text = env.read_text()
        assert "__Secure-client-auth-state-prod" not in text and "laravel_session" not in text
        assert "123_SMS_AUTH_STRING=tok" in text and "_ga=1" in text
        assert 'PAYPAY_COOKIE="' in text, "quoting must be preserved"
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        backups = list(env.parent.glob(".env.bak-*"))
        assert len(backups) == 1 and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert "__Secure-client-auth-state-prod" in backups[0].read_text()
        assert not {w["code"] for w in out["warnings"]} & {"STALE_AUTH_COOKIE", "ENV_PERMISSIONS"}
        assert len(out["fixed"]) == 2
        kc.assert_not_called()


def test_stale_cookie_names():
    assert pk.stale_auth_cookie_names("a=1; laravel_session=x; __Secure-client-auth-state-prod=y") == [
        "laravel_session", "__Secure-client-auth-state-prod"]
    assert pk.stale_auth_cookie_names("123_SMS_AUTH_STRING=t") == []


if __name__ == "__main__":
    raise SystemExit(run(globals()))
