"""Выпуск подписанного релиза (только для автора — нужен закрытый ключ ~/.giftbot/release_key.pem).

    python release.py            — проверить, подписать, закоммитить release.json, поставить тег и отправить
    python release.py --dry-run  — только проверить и показать, что будет подписано

Порядок: всё уже закоммичено → тесты → список файлов последнего коммита с SHA-256 → подпись → коммит
release.json/release.sig → тег vX.Y.Z → push. Приложения друзей ставят только то, что подписано этим ключом.
Ключ не терять и никому не давать: без него новые версии не установятся, с ним — установится что угодно."""
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from updater import MANIFEST, PUBLIC_KEY, SIGNATURE, _safe_rel

ROOT = Path(__file__).resolve().parent
KEY = Path.home() / ".giftbot" / "release_key.pem"


def git(*args: str, binary: bool = False):
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return out.stdout if binary else out.stdout.decode("utf-8").strip()


def changes_section(changes: str, version: str) -> str:
    """Кусок CHANGES.md про нужную версию (заголовок «## 1.3.0 …»)."""
    out, inside = [], False
    for line in changes.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lstrip("v").startswith(version)
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def main() -> None:
    dry = "--dry-run" in sys.argv
    if git("status", "--porcelain"):
        sys.exit("Сначала закоммитьте все изменения (git status не пустой).")
    if subprocess.run([sys.executable, "tests/run.py"], cwd=ROOT).returncode:
        sys.exit("Тесты не прошли — релиз отменён.")
    if subprocess.run([sys.executable, "-m", "compileall", "-q", "-x", r"backup|\.git", "."], cwd=ROOT).returncode:
        sys.exit("Есть синтаксические ошибки — релиз отменён.")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tag = f"v{version}"
    if git("tag", "-l", tag):
        sys.exit(f"Тег {tag} уже есть — увеличьте VERSION.")
    files = {}
    for line in git("ls-tree", "-r", "HEAD").splitlines():
        meta, rel = line.split("\t", 1)
        _, kind, sha = meta.split()
        if kind != "blob" or rel in (MANIFEST, SIGNATURE) or not _safe_rel(rel):
            continue
        files[rel] = hashlib.sha256(git("cat-file", "blob", sha, binary=True)).hexdigest()
    notes = changes_section((ROOT / "CHANGES.md").read_text(encoding="utf-8"), version)
    manifest = json.dumps({"version": version, "tag": tag, "notes": notes, "files": files},
                          ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    print(f"{tag}: {len(files)} файлов")
    if dry:
        return

    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(KEY.read_bytes(), None)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    if pub != PUBLIC_KEY:
        sys.exit("Ключ не совпадает с PUBLIC_KEY в updater.py — подпись не примут.")
    (ROOT / MANIFEST).write_bytes(manifest)
    (ROOT / SIGNATURE).write_text(base64.b64encode(key.sign(manifest)).decode() + "\n", encoding="ascii")
    git("add", MANIFEST, SIGNATURE)
    git("commit", "-m", f"release {tag}")
    git("tag", tag)
    git("push", "origin", "HEAD", tag)
    print(f"Готово: {tag} подписан и отправлен.")


if __name__ == "__main__":
    main()
