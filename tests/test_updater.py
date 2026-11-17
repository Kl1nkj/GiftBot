"""Проверка подписанных обновлений: подменённый файл, чужая подпись и откат при ошибке."""
import base64
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import updater  # noqa: E402

KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _release(files: dict[str, bytes], tamper: dict[str, bytes] | None = None):
    manifest = json.dumps({"version": "9.9.9", "tag": "v9.9.9", "notes": "",
                           "files": {k: updater.file_hash(v) for k, v in files.items()}}).encode()
    sig = base64.b64encode(KEY.sign(manifest))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in {**files, **(tamper or {})}.items():
            z.writestr(f"GiftBot-9.9.9/{k}", v)
    return manifest, sig, zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def _expect_error(func, text):
    try:
        func()
    except RuntimeError as e:
        assert text in str(e), e
        return
    raise AssertionError("ожидалась ошибка")


def test_signature_checked():
    manifest, sig, _ = _release({"bot.py": b"print(1)"})
    assert updater.verify_manifest(manifest, sig, PUB)["version"] == "9.9.9"
    _expect_error(lambda: updater.verify_manifest(manifest.replace(b"9.9.9", b"9.9.8"), sig, PUB), "подпись")
    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    _expect_error(lambda: updater.verify_manifest(manifest, sig, other), "подпись")


def test_tampered_file_rejected():
    manifest, sig, archive = _release({"bot.py": b"print(1)"}, tamper={"bot.py": b"print('evil')"})
    data = updater.verify_manifest(manifest, sig, PUB)
    _expect_error(lambda: updater.stage(archive, data, Path(tempfile.mkdtemp())), "не совпадает")


def test_crlf_and_extra_files():
    manifest, sig, archive = _release({"a.py": b"x = 1\n"}, tamper={"a.py": b"x = 1\r\n", "evil.py": b"bad"})
    data = updater.verify_manifest(manifest, sig, PUB)
    staging = Path(tempfile.mkdtemp())
    assert updater.stage(archive, data, staging) == ["a.py"]
    assert not (staging / "evil.py").exists()  # файлы вне подписанного списка не ставятся


def test_unsafe_paths():
    assert not updater._safe_rel("../x.py")
    assert not updater._safe_rel(".env")
    assert not updater._safe_rel("data.db")
    assert not updater._safe_rel("C:/Windows/x")
    assert updater._safe_rel("sources/portals.py")


def test_install_and_rollback():
    app = Path(tempfile.mkdtemp())
    old_dir = updater.APP_DIR
    updater.APP_DIR = app
    try:
        (app / "keep.py").write_text("old")
        (app / "gone.py").write_text("old")
        (app / ".env").write_text("SECRET=1")
        (app / "release.json").write_text(json.dumps({"files": {"keep.py": "", "gone.py": ""}}))
        manifest, sig, archive = _release({"keep.py": b"new", "added.py": b"new"})
        data = updater.verify_manifest(manifest, sig, PUB)
        data["_raw"], data["_sig"] = manifest, sig
        staging = Path(tempfile.mkdtemp())
        files = updater.stage(archive, data, staging)
        backup = app / "backup" / "1"
        updater.install(staging, files, data, backup, log=lambda m: None)
        assert (app / "keep.py").read_text() == "new"
        assert (app / "added.py").exists() and not (app / "gone.py").exists()
        assert (app / ".env").read_text() == "SECRET=1"
        info = json.loads((backup / "_restore.json").read_text())
        updater.rollback(backup, info["restore"], info["created"])
        assert (app / "keep.py").read_text() == "old"
        assert (app / "gone.py").exists() and not (app / "added.py").exists()
    finally:
        updater.APP_DIR = old_dir
