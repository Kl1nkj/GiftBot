"""Обновление GiftBot с GitHub: сравнивает VERSION в репозитории с локальной и ставит новую версию поверх.

Ваши данные не трогаются: .env (токены и ключи), data.db (фильтры, история), user.session (вход в Telegram),
bot.log и update.json остаются как есть. Перед обновлением делается резервная копия заменяемых файлов в backup/."""
import io
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SETTINGS = APP_DIR / "update.json"
# Что никогда не перезаписывается обновлением
PROTECTED = {".env", "data.db", "data.db-journal", "user.session", "user.session-journal", "bot.log",
             "update.json", "app.log"}
PROTECTED_DIRS = {"backup", "__pycache__", ".git"}
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


def check() -> dict:
    """{"available": bool, "local": "1.0.0", "remote": "1.1.0", "notes": "что нового"}.
    Бросает исключение, если репозиторий не настроен или GitHub недоступен."""
    r = repo()
    if not r:
        raise RuntimeError("не указан репозиторий для обновлений (update.json)")
    raw = f"https://raw.githubusercontent.com/{r}/{branch()}"
    # параметр в адресе обходит кэш GitHub (иначе новая версия видна только через несколько минут)
    remote = _get(f"{raw}/VERSION?t={int(time.time())}").decode().strip()
    notes = ""
    if _parse(remote) > _parse(local_version()):
        try:
            notes = _section(_get(f"{raw}/CHANGES.md?t={int(time.time())}").decode("utf-8"), remote)
        except Exception:
            notes = ""
    return {"available": _parse(remote) > _parse(local_version()), "local": local_version(),
            "remote": remote, "notes": notes}


def _section(changes: str, version: str) -> str:
    """Кусок CHANGES.md про нужную версию (заголовок «## 1.1.0»)."""
    out, inside = [], False
    for line in changes.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lstrip("v").startswith(version.lstrip("v"))
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def apply(log=print) -> dict:
    """Скачивает ветку с GitHub и ставит поверх. Возвращает {"version", "app_changed", "deps_changed"}."""
    r = repo()
    if not r:
        raise RuntimeError("не указан репозиторий для обновлений (update.json)")
    log(f"Скачиваю {r} ({branch()})…")
    data = _get(f"https://codeload.github.com/{r}/zip/refs/heads/{branch()}")
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = archive.namelist()
    root = names[0].split("/")[0] + "/"  # архив GitHub кладёт всё в папку «repo-branch/»
    if not any(n == root + "bot.py" for n in names):
        raise RuntimeError("в архиве нет bot.py — это не GiftBot")

    old_req = _read(APP_DIR / "requirements.txt")
    old_app = _read(APP_DIR / "app.pyw") + _read(APP_DIR / "updater.py")
    backup = APP_DIR / "backup" / time.strftime("%Y%m%d-%H%M%S")
    changed = 0
    for name in names:
        rel = name[len(root):]
        if not rel or name.endswith("/"):
            continue
        parts = Path(rel).parts
        if rel in PROTECTED or parts[0] in PROTECTED_DIRS or ".." in parts:
            continue
        target = APP_DIR / rel
        new = archive.read(name)
        if target.exists() and target.read_bytes() == new:
            continue
        if target.exists():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup / rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new)
        changed += 1
    log(f"Обновлено файлов: {changed}" + (f", старые копии в {backup.relative_to(APP_DIR)}" if changed else ""))

    deps_changed = _read(APP_DIR / "requirements.txt") != old_req
    if deps_changed:
        log("Ставлю новые библиотеки…")
        proc = subprocess.run(
            [_python(), "-m", "pip", "install", "-r", str(APP_DIR / "requirements.txt")],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        if proc.returncode:
            raise RuntimeError("pip install не прошёл:\n" + (proc.stderr or proc.stdout)[-1500:])
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
