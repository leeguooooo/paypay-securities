import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from _runner import run
from paypay_sec import cli, config
from paypay_sec import client as clientmod


def _setup(cookie: str):
    """Hermetic doctor environment: a tmp .env + state dir, controlled os.environ."""
    d = Path(tempfile.mkdtemp(prefix="pp_doctor_"))
    envf = d / ".env"
    envf.write_text("PAYPAY_MEMBER_ID=Pxxxxxxxxx\n", encoding="utf-8")
    os.environ["PAYPAY_MEMBER_ID"] = "Pxxxxxxxxx"
    os.environ["PAYPAY_PASSWORD"] = "pw"
    os.environ["PAYPAY_COOKIE"] = cookie
    config.env_file_for = lambda a: envf       # monkeypatch
    clientmod.state_dir = lambda a: d          # monkeypatch (doctor imports it at call time)
    return d


def _doctor_json():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_doctor(None, SimpleNamespace(account="default", json=True))
    return json.loads(buf.getvalue()), rc


def test_doctor_ready_with_device_token():
    _setup("123_SMS_AUTH_STRING=tok; _ga=1")
    out, rc = _doctor_json()
    assert out["ready"] is True and rc == 0
    assert out["has_device_token"] is True
    assert out["sms_would_be_required"] is False
    assert all(c["ok"] for c in out["checks"])


def test_doctor_flags_missing_device_token():
    _setup("123; _ga=1")   # cookie present but NO trusted-device token
    out, rc = _doctor_json()
    assert out["has_device_token"] is False
    assert out["sms_would_be_required"] is True
    assert out["ready"] is False and rc == 1


if __name__ == "__main__":
    raise SystemExit(run(globals()))
