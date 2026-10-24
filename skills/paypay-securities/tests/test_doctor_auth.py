"""Synthetic diagnostics regressions; no accounts, network or Keychain access."""
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _runner import run
from paypay_sec import cli, config, client, passkey_login as pk


@contextlib.contextmanager
def environment(cookie="123_SMS_AUTH_STRING=synthetic", account="default", credentials=True):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = root / (".env" if account == "default" else account + ".env")
        env.write_text("PAYPAY_MEMBER_ID=synthetic-id\nPAYPAY_PASSWORD=synthetic-password\nPAYPAY_COOKIE=" + cookie if credentials else "")
        env.chmod(0o600)
        with patch.dict(os.environ, {}, clear=True), patch.object(config, "HOME", root), patch.object(config, "env_file_for", return_value=env), patch.object(client, "state_dir", return_value=root):
            yield root


def doctor(online=True, account="default", as_json=True):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.cmd_doctor(None, SimpleNamespace(account=account, online=online, json=as_json))
    return (json.loads(out.getvalue()) if as_json else out.getvalue()), rc


def test_errors_are_classified_and_redacted():
    for error, code in [(client.LoginError("secret-cookie", code="PASSKEY_MISSING"), "PASSKEY_MISSING"),
                        (client.LoginError("secret-cookie", code="PASSKEY_LOGIN_FAILED"), "PASSKEY_LOGIN_FAILED"),
                        (client.LoginError("secret-cookie", code="SMS_REQUIRED"), "SMS_REQUIRED"),
                        (client.SessionExpired("secret-cookie"), "SESSION_EXPIRED"),
                        (RuntimeError("secret-cookie"), "PROBE_FAILED")]:
        with environment(), patch.object(cli, "PayPayClient") as factory:
            factory.return_value.login.side_effect = error
            out, rc = doctor()
            assert rc == 1 and out["ready"] is False
            assert out["error_codes"] == [code]
            assert "secret-cookie" not in json.dumps(out)
            text, rc = doctor(as_json=False)
            assert rc == 1 and "secret-cookie" not in text
            if code == "PASSKEY_MISSING":
                assert "passkey-setup -a default" in text


def test_missing_credentials_does_not_attempt_login():
    with environment(credentials=False), patch.object(cli, "PayPayClient") as factory:
        out, rc = doctor()
        assert rc == 1 and out["error_codes"] == ["CREDENTIALS_MISSING"]
        factory.assert_not_called()


def test_online_passkey_success_overrides_cookie_heuristic():
    with environment(cookie=""), patch.object(cli, "PayPayClient") as factory:
        factory.return_value.login.return_value = {"STATUS": True, "PASSKEY": True}
        out, rc = doctor()
        assert rc == 0 and out["authentication_verified"] and out["status"] == "READY"
        assert not out["offline_ready"]
        assert factory.call_args.kwargs["cache_ttl"] == 0


def test_offline_is_unverified_and_never_accesses_keychain():
    with environment() as root, patch.object(cli, "PayPayClient") as factory, patch.object(pk, "load_passkey") as load:
        (root / "session.json").write_text("[]")
        out, rc = doctor(online=False)
        assert rc == 0 and out["status"] == "UNVERIFIED"
        assert not out["authentication_verified"]
        factory.assert_not_called()
        load.assert_not_called()


def test_expired_probe_is_not_ready():
    with environment(), patch.object(cli, "PayPayClient") as factory:
        factory.return_value.login.return_value = {"STATUS": True}
        factory.return_value.portfolio_html.side_effect = client.SessionExpired("secret")
        out, rc = doctor()
        assert rc == 1 and out["error_codes"] == ["SESSION_EXPIRED"]


def test_named_account_does_not_inherit_default_environment():
    with environment(account="second", credentials=False), patch.dict(os.environ, {"PAYPAY_MEMBER_ID": "wrong", "PAYPAY_PASSWORD": "wrong"}), patch.object(cli, "PayPayClient") as factory:
        out, rc = doctor(account="second")
        assert rc == 1 and out["error_codes"] == ["CREDENTIALS_MISSING"]
        factory.assert_not_called()


def test_client_preserves_missing_passkey_type():
    c = object.__new__(client.PayPayClient)
    c._session = Mock()
    c.settings = config.Settings("synthetic", "synthetic", "", account="second")
    c.session_file = Path("unused/session.json")
    with patch.object(pk, "passkey_login", side_effect=pk.PasskeyMissing("secret")):
        try:
            c._passkey_login()
        except client.LoginError as e:
            assert e.code == "PASSKEY_MISSING"
        else:
            assert False, "expected LoginError"


def test_cli_process_exit_matches_not_ready():
    import subprocess
    import sys
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, HOME=tmp, PAYPAY_ENV=str(Path(tmp) / "absent"))
        for key in ("PAYPAY_MEMBER_ID", "PAYPAY_PASSWORD", "PAYPAY_COOKIE", "PAYPAY_ACCOUNT"):
            env.pop(key, None)
        result = subprocess.run([sys.executable, "-c",
                                 "from paypay_sec.cli import main; raise SystemExit(main())",
                                 "doctor", "--online", "--json"], env=env,
                                capture_output=True, text=True)
        assert result.returncode == 1
        assert json.loads(result.stdout)["error_codes"] == ["CREDENTIALS_MISSING"]


def test_passkey_absent_raises_typed_error_without_network():
    session = Mock()
    with patch.object(pk, "load_passkey", return_value=None):
        try:
            pk.passkey_login(session, "default", Path("unused"))
        except pk.PasskeyMissing:
            pass
        else:
            assert False, "expected PasskeyMissing"
    assert not session.mock_calls


if __name__ == "__main__":
    raise SystemExit(run(globals()))
