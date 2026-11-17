"""Обновление GiftBot с GitHub — только подписанные релизы.

Как это защищено: автор подписывает release.json (версия + SHA-256 каждого файла) своим закрытым ключом,
который хранится только у него на компьютере (release.py). Здесь вшит открытый ключ: обновление ставится,
только если подпись верна и каждый файл из архива совпадает с хешем из подписанного списка.
Даже если GitHub-аккаунт угонят, без закрытого ключа подменить код не выйдет.

Установка атомарная: новые файлы сначала проверяются и раскладываются во временной папке, заменяемые
копируются в backup/, а при любой ошибке (в том числе pip) всё возвращается как было.

Ваши данные не трогаются: .env (токены и ключи), data.db (фильтры, история, журнал сделок),
user.session (вход в Telegram), журналы и update.json."""
import base64
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SETTINGS = APP_DIR / "update.json"
MANIFEST = "release.json"
SIGNATURE = "release.sig"
# Открытый ключ автора (ed25519). Закрытый — только у автора, в ~/.giftbot/release_key.pem
PUBLIC_KEY = "e292ef12bf2bbbfced194ccd0c0815449da718b7b37a992e6c12c8c8aa4e3fc1"
# Что никогда не перезаписывается и не удаляется обновлением
PROTECTED = {".env", "data.db", "data.db-journal", "data.db-wal", "data.db-shm", "user.session",
             "user.session-journal", "bot.log", "bot.err.log", "bot.pid", "update.json", "app.log"}
PROTECTED_DIRS = {"backup", "__pycache__", ".git"}
KEEP_BACKUPS = 3
TIMEOUT = 30


def settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def repo() -> str:
    """owner/name репозитория на GitHub, откуда брать обновления."""
    return (settings().get("repo") or "").strip().strip("/")


def branch() -> str:
    return settings().get("branch") or "main"


def local_version() -> str:
    try:
        return (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def _parse(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.strip().lstrip("v").split("."))


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "GiftBot-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


# ---------- подпись ----------
def verify_manifest(manifest: bytes, signature: bytes, public_key: str = PUBLIC_KEY) -> dict:
    """Проверяет подпись release.json и возвращает его содержимое. Неверная подпись — исключение."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as e:
        raise RuntimeError("нет библиотеки cryptography для проверки подписи: pip install cryptography") from e
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            base64.b64decode(signature.strip()), manifest)
    except (InvalidSignature, ValueError) as e:
        raise RuntimeError("подпись обновления неверна — обновление отклонено (возможна подмена)") from e
    data = json.loads(manifest)
    if not isinstance(data.get("files"), dict) or not data.get("version") or not data.get("tag"):
        raise RuntimeError("в подписанном списке нет версии или файлов")
    return data


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _matches(data: bytes, expected: str) -> bool:
    # GitHub может отдать текст с другими окончаниями строк — содержимое от этого не меняется
    return file_hash(data) == expected or file_hash(data.replace(b"\r\n", b"\n")) == expected


def _safe_rel(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(parts) and not Path(rel).is_absolute() and ".." not in parts and ":" not in rel \
        and rel not in PROTECTED and parts[0] not in PROTECTED_DIRS


def _remote_manifest() -> dict:
    r = repo()
    if not r:
        raise RuntimeError("не указан репозиторий для обновлений (update.json)")
    raw = f"https://raw.githubusercontent.com/{r}/{branch()}"
    t = int(time.time())  # параметр обходит кэш GitHub (иначе новая версия видна через несколько минут)
    try:
        manifest = _get(f"{raw}/{MANIFEST}?t={t}")
        signature = _get(f"{raw}/{SIGNATURE}?t={t}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("в репозитории нет подписанного релиза") from e
        raise
    data = verify_manifest(manifest, signature)
    data["_raw"], data["_sig"] = manifest, signature
    return data


def check() -> dict:
    """{"available": bool, "local": "1.0.0", "remote": "1.1.0", "notes": "что нового"}.
    Бросает исключение, если репозиторий не настроен, GitHub недоступен или подпись неверна."""
    m = _remote_manifest()
    return {"available": _parse(m["version"]) > _parse(local_version()), "local": local_version(),
            "remote": m["version"], "notes": m.get("notes", "")}


# ---------- установка ----------
def stage(archive: zipfile.ZipFile, manifest: dict, staging: Path) -> list[str]:
    """Раскладывает файлы из архива в staging, проверяя каждый по подписанному списку.
    Возвращает список установленных путей. Любое несовпадение — исключение, ничего не ставится."""
    names = archive.namelist()
    root = names[0].split("/")[0] + "/"  # архив GitHub кладёт всё в папку «repo-tag/»
    in_zip = {n[len(root):]: n for n in names if n.startswith(root) and not n.endswith("/")}
    files = manifest["files"]
    for rel, digest in files.items():
        if not _safe_rel(rel):
            raise RuntimeError(f"недопустимый путь в списке файлов: {rel}")
        if rel not in in_zip:
            raise RuntimeError(f"в архиве нет файла {rel}")
        data = archive.read(in_zip[rel])
        if not _matches(data, digest):
            raise RuntimeError(f"файл {rel} не совпадает с подписанным — обновление отклонено")
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return sorted(files)


def install(staging: Path, files: list[str], manifest: dict, backup: Path, log=print) -> int:
    """Ставит проверенные файлы поверх, убирает файлы прошлой версии, которых больше нет.
    При ошибке на середине возвращает всё из backup и пробрасывает исключение."""
    old = _local_manifest_files()
    removed = [rel for rel in old if rel not in manifest["files"] and _safe_rel(rel) and (APP_DIR / rel).exists()]
    created: list[str] = []
    replaced: list[str] = []
    try:
        for rel in files:
            src, target = staging / rel, APP_DIR / rel
            new = src.read_bytes()
            if target.exists():
                if target.read_bytes() == new:
                    continue
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / rel)
                replaced.append(rel)
            else:
                created.append(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".new")
            tmp.write_bytes(new)
            tmp.replace(target)
        for rel in removed:
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(APP_DIR / rel), backup / rel)
        (APP_DIR / MANIFEST).write_bytes(manifest["_raw"])
        (APP_DIR / SIGNATURE).write_bytes(manifest["_sig"])
    except Exception:
        log("Ошибка при установке — возвращаю прежнюю версию…")
        rollback(backup, replaced + removed, created)
        raise
    (backup / "_restore.json").write_text(json.dumps({"restore": replaced + removed, "created": created}),
                                          encoding="utf-8")
    return len(replaced) + len(created) + len(removed)


def rollback(backup: Path, restore: list[str], created: list[str]) -> None:
    for rel in created:
        try:
            (APP_DIR / rel).unlink()
        except OSError:
            pass
    for rel in restore:
        src = backup / rel
        if src.exists():
            (APP_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, APP_DIR / rel)


def _local_manifest_files() -> list[str]:
    try:
        return list(json.loads((APP_DIR / MANIFEST).read_text(encoding="utf-8"))["files"])
    except (OSError, ValueError, KeyError):
        return []


def _prune_backups() -> None:
    root = APP_DIR / "backup"
    if not root.exists():
        return
    old = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)[:-KEEP_BACKUPS]
    for p in old:
        shutil.rmtree(p, ignore_errors=True)


def apply(log=print) -> dict:
    """Скачивает подписанный релиз и ставит поверх. Возвращает {"version", "app_changed", "deps_changed"}."""
    manifest = _remote_manifest()
    if _parse(manifest["version"]) <= _parse(local_version()):
        raise RuntimeError(f"новой версии нет (у вас {local_version()}, в репозитории {manifest['version']})")
    log(f"Скачиваю {repo()} {manifest['tag']}…")
    data = _get(f"https://codeload.github.com/{repo()}/zip/refs/tags/{manifest['tag']}")
    archive = zipfile.ZipFile(io.BytesIO(data))

    old_req = _read(APP_DIR / "requirements.txt")
    old_app = _read(APP_DIR / "app.pyw") + _read(APP_DIR / "updater.py")
    backup = APP_DIR / "backup" / time.strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="giftbot-update-") as tmp:
        log("Проверяю подпись каждого файла…")
        files = stage(archive, manifest, Path(tmp))
        changed = install(Path(tmp), files, manifest, backup, log)
    log(f"Обновлено файлов: {changed}" + (f", старые копии в {backup.relative_to(APP_DIR)}" if changed else ""))

    deps_changed = _read(APP_DIR / "requirements.txt") != old_req
    if deps_changed:
        log("Ставлю новые библиотеки…")
        proc = subprocess.run(
            [_python(), "-m", "pip", "install", "-r", str(APP_DIR / "requirements.txt")],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        if proc.returncode:
            info = json.loads((backup / "_restore.json").read_text(encoding="utf-8"))
            rollback(backup, info["restore"], info["created"])
            raise RuntimeError("pip install не прошёл — вернул прежнюю версию:\n" + (proc.stderr or proc.stdout)[-1500:])
    _prune_backups()
    return {"version": local_version(), "deps_changed": deps_changed,
            "app_changed": _read(APP_DIR / "app.pyw") + _read(APP_DIR / "updater.py") != old_app}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _python() -> str:
    """python.exe (а не pythonw.exe) рядом с текущим интерпретатором — нужен для pip и бота."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if console.exists() else exe)


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
