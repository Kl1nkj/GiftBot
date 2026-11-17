"""GiftBot — приложение для Windows: запускает бота в фоне, показывает журнал, настройки и обновления.

Окно можно закрыть — бот продолжит работать, приложение останется значком в трее (возле часов).
Запуск: двойной щелчок по ярлыку «GiftBot» (или pythonw app.pyw). С ключом --minimized — сразу в трей."""
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import updater

APP_DIR = Path(__file__).resolve().parent
os.chdir(APP_DIR)
ENV = APP_DIR / ".env"
LOG = APP_DIR / "bot.log"
ERR_LOG = APP_DIR / "bot.err.log"  # только то, что бот вывел мимо журнала (падение интерпретатора)
PID_FILE = APP_DIR / "bot.pid"
ICON = APP_DIR / "icon.ico"
SINGLE_PORT = 47391  # второе окно не запускаем — показываем уже открытое
RESTART_DELAY = 10
UPDATE_EVERY = 6 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STARTUP = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
STARTUP_LINK = STARTUP / "GiftBot.lnk"

logging.basicConfig(filename=APP_DIR / "app.log", level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("app")

# Поля окна «Настройки» (.env): ключ, подпись, скрывать ли значение
SETTINGS_FIELDS = [
    ("BOT_TOKEN", "Токен бота (@BotFather)", True),
    ("OWNER_ID", "Владельцы — Telegram ID через запятую", False),
    ("API_ID", "API_ID (my.telegram.org)", False),
    ("API_HASH", "API_HASH (my.telegram.org)", True),
    ("MARKETAPP_API_TOKEN", "Токен MarketApp", True),
    ("GETGEMS_API_KEY", "Ключ GetGems", True),
    ("NGROK_AUTHTOKEN", "ngrok authtoken (постоянный адрес мини-приложения)", True),
    ("NGROK_DOMAIN", "ngrok домен", False),
]

C = {"bg": "#17212b", "panel": "#1e2c3a", "text": "#e5e7eb", "muted": "#94a3b8", "accent": "#3b82f6",
     "ok": "#22c55e", "bad": "#ef4444", "warn": "#f59e0b"}


def python_exe() -> str:
    return updater._python()


def kill_tree(pid: int) -> None:
    """Процесс и всё, что он запустил (туннели), — и ничего чужого."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=NO_WINDOW, capture_output=True)


def kill_strays() -> None:
    """Останавливает бота из этой папки, запущенного раньше (прошлое окно или старый start_bot.bat).
    Ищем точно: по bot.pid, который пишет сам бот, и по start_bot.bat именно из этой папки."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid():
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            creationflags=NO_WINDOW, capture_output=True, text=True,
        ).stdout
        if "bot.py" in out:
            kill_tree(pid)
    bat = str(APP_DIR / "start_bot.bat").replace("'", "''").replace("[", "`[").replace("]", "`]")
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and "
        f"$_.CommandLine -like '*{bat}*' }} | ForEach-Object {{ taskkill /PID $_.ProcessId /T /F }}"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], creationflags=NO_WINDOW, capture_output=True)


class BotProcess:
    """bot.py в фоне без окна; если упал — перезапуск через 10 секунд."""

    def __init__(self, on_change):
        self.proc: subprocess.Popen | None = None
        self.want = False
        self.on_change = on_change
        self.started = 0.0
        # запуск/остановка из окна и из сторожа одновременно дали бы два bot.py (двойные алерты)
        self.lock = threading.RLock()
        threading.Thread(target=self._watch, daemon=True).start()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        with self.lock:
            self.want = True
            if self.running:
                return
            kill_strays()
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "GIFTBOT_LOG_FILE": LOG.name}
            try:
                if ERR_LOG.exists() and ERR_LOG.stat().st_size > 2_000_000:
                    ERR_LOG.unlink()
            except OSError:
                pass
            out = open(ERR_LOG, "a", encoding="utf-8")
            self.proc = subprocess.Popen([python_exe(), "bot.py"], cwd=APP_DIR, stdout=out, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW)
            out.close()
            self.started = time.time()
            log.info("бот запущен, pid %s", self.proc.pid)
        self.on_change()

    def stop(self) -> None:
        with self.lock:
            self.want = False
            if self.running:
                kill_tree(self.proc.pid)  # вместе с туннелями, которые бот запустил
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            kill_strays()
            log.info("бот остановлен")
        self.on_change()

    def restart(self) -> None:
        with self.lock:
            self.stop()
            self.start()

    def _watch(self) -> None:
        while True:
            time.sleep(2)
            if self.want and self.proc is not None and self.proc.poll() is not None:
                log.warning("бот завершился с кодом %s — перезапуск через %s с", self.proc.returncode, RESTART_DELAY)
                self.on_change()
                time.sleep(RESTART_DELAY)
                with self.lock:
                    if self.want and not self.running:
                        self.start()


# ---------- .env ----------
def read_env() -> dict[str, str]:
    values = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_env(new: dict[str, str]) -> None:
    """Меняет только переданные ключи, комментарии и остальные строки сохраняются."""
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    done = set()
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in new:
                lines[i] = f"{k}={new[k]}"
                done.add(k)
    lines += [f"{k}={v}" for k, v in new.items() if k not in done]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- автозапуск ----------
def autostart_enabled() -> bool:
    if not STARTUP_LINK.exists():
        return False
    return "app.pyw" in _link_target()


def _link_target() -> str:
    path = str(STARTUP_LINK).replace("'", "''")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{path}'); $s.TargetPath + ' ' + $s.Arguments"],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    )
    return out.stdout


def make_shortcut(link: Path, args: str) -> None:
    pythonw = str(Path(python_exe()).with_name("pythonw.exe")).replace("'", "''")
    app = str(APP_DIR / "app.pyw").replace("'", "''")
    icon = str(ICON).replace("'", "''")
    script = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{str(link).replace(chr(39), chr(39) * 2)}');"
        f"$s.TargetPath='{pythonw}';$s.Arguments='\"{app}\" {args}';"
        f"$s.WorkingDirectory='{str(APP_DIR).replace(chr(39), chr(39) * 2)}';"
        f"$s.IconLocation='{icon}';$s.Description='GiftBot';$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], creationflags=NO_WINDOW, capture_output=True)


def set_autostart(on: bool) -> None:
    if on:
        make_shortcut(STARTUP_LINK, "--minimized")  # заменяет старый ярлык на start_hidden.vbs
    elif STARTUP_LINK.exists():
        STARTUP_LINK.unlink()


# ---------- окно ----------
class App:
    def __init__(self, minimized: bool):
        self.root = tk.Tk()
        self.root.title("GiftBot")
        self.root.geometry("760x560")
        self.root.minsize(600, 420)
        self.root.configure(bg=C["bg"])
        if ICON.exists():
            try:
                self.root.iconbitmap(str(ICON))
            except tk.TclError:
                pass
        self.calls: queue.Queue = queue.Queue()  # вызовы из других потоков (трей, проверка обновлений)
        self.bot = BotProcess(lambda: self.later(self.refresh_status))
        self.update_info: dict | None = None
        self.log_pos = 0
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.tray = None
        threading.Thread(target=self._tray, daemon=True).start()
        if minimized:
            self.root.withdraw()
        self.bot.start()
        self.root.after(200, self._pump)
        self.root.after(1000, self._tail_log)
        self.root.after(5000, lambda: self.check_updates(silent=True))

    # --- оформление ---
    def _style(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=C["bg"], foreground=C["text"], font=("Segoe UI", 10))
        s.configure("Panel.TFrame", background=C["panel"])
        s.configure("Panel.TLabel", background=C["panel"], foreground=C["text"])
        s.configure("Muted.TLabel", background=C["panel"], foreground=C["muted"])
        s.configure("Title.TLabel", background=C["bg"], foreground=C["text"], font=("Segoe UI Semibold", 16))
        s.configure("TButton", background="#2b3b4d", foreground=C["text"], borderwidth=0, padding=(12, 6))
        s.map("TButton", background=[("active", "#35495f"), ("disabled", "#1f2a36")],
              foreground=[("disabled", C["muted"])])
        s.configure("Accent.TButton", background=C["accent"], foreground="white")
        s.map("Accent.TButton", background=[("active", "#2563eb")])
        s.configure("TCheckbutton", background=C["panel"], foreground=C["text"])
        s.map("TCheckbutton", background=[("active", C["panel"])])
        s.configure("TEntry", fieldbackground="#0f1720", foreground=C["text"], insertcolor=C["text"])

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(16, 14, 16, 6))
        top.pack(fill="x")
        ttk.Label(top, text="🎁 GiftBot", style="Title.TLabel").pack(side="left")
        self.status = tk.Label(top, text="", bg=C["bg"], fg=C["muted"], font=("Segoe UI", 10))
        self.status.pack(side="left", padx=14)

        bar = ttk.Frame(self.root, style="Panel.TFrame", padding=12)
        bar.pack(fill="x", padx=16, pady=6)
        self.toggle_btn = ttk.Button(bar, text="Остановить", command=self.toggle)
        self.toggle_btn.pack(side="left")
        ttk.Button(bar, text="Перезапустить", command=lambda: self.run_bg(self.bot.restart)).pack(side="left", padx=6)
        ttk.Button(bar, text="Настройки", command=self.open_settings).pack(side="left", padx=6)
        ttk.Button(bar, text="Войти в Telegram", command=self.telegram_login).pack(side="left", padx=6)
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(bar, text="Запускать с Windows", variable=self.autostart,
                        command=lambda: set_autostart(self.autostart.get())).pack(side="right")

        upd = ttk.Frame(self.root, style="Panel.TFrame", padding=12)
        upd.pack(fill="x", padx=16, pady=6)
        self.version_lbl = ttk.Label(upd, text=f"Версия {updater.local_version()}", style="Panel.TLabel")
        self.version_lbl.pack(side="left")
        self.update_lbl = ttk.Label(upd, text="", style="Muted.TLabel")
        self.update_lbl.pack(side="left", padx=12)
        self.update_btn = ttk.Button(upd, text="Проверить обновления", command=self.on_update_click)
        self.update_btn.pack(side="right")

        logf = ttk.Frame(self.root, padding=(16, 6, 16, 16))
        logf.pack(fill="both", expand=True)
        self.log_box = ScrolledText(logf, bg="#0f1720", fg="#cbd5e1", insertbackground="#cbd5e1", relief="flat",
                                    font=("Consolas", 9), wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True)
        self.refresh_status()

    # --- потоки → окно ---
    def later(self, fn, *args) -> None:
        self.calls.put((fn, args))

    def _pump(self) -> None:
        while not self.calls.empty():
            fn, args = self.calls.get()
            try:
                fn(*args)
            except Exception:
                log.exception("ui call")
        self.root.after(200, self._pump)

    def run_bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    # --- бот ---
    def refresh_status(self) -> None:
        if self.bot.running:
            up = int(time.time() - self.bot.started)
            self.status.config(text=f"● работает  ({up // 3600} ч {up % 3600 // 60} мин)", fg=C["ok"])
            self.toggle_btn.config(text="Остановить")
        elif self.bot.want:
            self.status.config(text="● перезапускается…", fg=C["warn"])
        else:
            self.status.config(text="● остановлен", fg=C["bad"])
            self.toggle_btn.config(text="Запустить")

    def toggle(self) -> None:
        self.run_bg(self.bot.stop if self.bot.running else self.bot.start)

    def _tail_log(self) -> None:
        try:
            size = LOG.stat().st_size if LOG.exists() else 0
            if size < self.log_pos:  # журнал очистили
                self.log_pos = 0
            if size > self.log_pos:
                with open(LOG, "rb") as f:
                    f.seek(max(self.log_pos, size - 60_000))
                    chunk = f.read().decode("utf-8", errors="replace")
                self.log_pos = size
                self.log_box.config(state="normal")
                self.log_box.insert("end", chunk)
                lines = int(self.log_box.index("end-1c").split(".")[0])
                if lines > 1500:
                    self.log_box.delete("1.0", f"{lines - 1500}.0")
                self.log_box.see("end")
                self.log_box.config(state="disabled")
        except OSError:
            pass
        self.refresh_status()
        self.root.after(2000, self._tail_log)

    # --- обновления ---
    def check_updates(self, silent: bool = False) -> None:
        def work():
            try:
                info = updater.check()
            except Exception as e:
                log.info("проверка обновлений: %s", e)
                self.later(self.update_lbl.config, {"text": "" if silent else f"не удалось проверить: {e}"})
                return
            self.later(self._show_update, info, silent)
        self.update_lbl.config(text="проверяю…")
        self.run_bg(work)
        self.root.after(UPDATE_EVERY * 1000, lambda: self.check_updates(silent=True))

    def _show_update(self, info: dict, silent: bool) -> None:
        self.update_info = info
        if info["available"]:
            self.update_lbl.config(text=f"доступна версия {info['remote']}", foreground=C["warn"])
            self.update_btn.config(text=f"Обновить до {info['remote']}", style="Accent.TButton")
            if self.tray:
                try:
                    self.tray.notify(f"Доступна новая версия {info['remote']}", "GiftBot")
                except Exception:
                    pass
        else:
            self.update_lbl.config(text="" if silent else "у вас последняя версия", foreground=C["muted"])
            self.update_btn.config(text="Проверить обновления", style="TButton")

    def on_update_click(self) -> None:
        info = self.update_info
        if not info or not info["available"]:
            self.check_updates()
            return
        notes = f"\n\nЧто нового:\n{info['notes']}" if info.get("notes") else ""
        if not messagebox.askyesno("Обновление GiftBot",
                                   f"Обновить {info['local']} → {info['remote']}?{notes}\n\n"
                                   "Бот перезапустится, настройки и фильтры сохранятся.", parent=self.root):
            return
        self.update_btn.config(state="disabled")
        self.update_lbl.config(text="обновляю…")

        def work():
            try:
                self.bot.stop()
                result = updater.apply(log=lambda m: self.later(self.update_lbl.config, {"text": m}))
            except Exception as e:
                log.exception("обновление")
                self.bot.start()
                self.later(self._update_failed, str(e))
                return
            self.later(self._update_done, result)
        self.run_bg(work)

    def _update_failed(self, error: str) -> None:
        self.update_btn.config(state="normal")
        self.update_lbl.config(text="обновление не удалось", foreground=C["bad"])
        messagebox.showerror("Обновление", error, parent=self.root)

    def _update_done(self, result: dict) -> None:
        self.version_lbl.config(text=f"Версия {result['version']}")
        self.update_info = None
        self.update_btn.config(state="normal", text="Проверить обновления", style="TButton")
        self.update_lbl.config(text=f"обновлено до {result['version']}", foreground=C["ok"])
        if result["app_changed"]:
            # обновилось само приложение — перезапускаем окно целиком, бот поднимет новый экземпляр
            subprocess.Popen([str(Path(python_exe()).with_name("pythonw.exe")), str(APP_DIR / "app.pyw"), "--restarted"],
                             cwd=APP_DIR, creationflags=NO_WINDOW)
            self.quit(stop_bot=False, release_only=True)
        else:
            self.run_bg(self.bot.start)

    # --- настройки ---
    def open_settings(self) -> None:
        env = read_env()
        win = tk.Toplevel(self.root, bg=C["panel"])
        win.title("Настройки")
        win.transient(self.root)
        frame = ttk.Frame(win, style="Panel.TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        entries = {}
        for row, (key, label, secret) in enumerate(SETTINGS_FIELDS):
            ttk.Label(frame, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            e = ttk.Entry(frame, width=48, show="•" if secret else "")
            e.insert(0, env.get(key, ""))
            e.grid(row=row, column=1, padx=(12, 0), pady=4)
            entries[key] = e
        repo = tk.StringVar(value=updater.repo())
        ttk.Label(frame, text="GitHub для обновлений (владелец/репозиторий)", style="Panel.TLabel").grid(
            row=len(SETTINGS_FIELDS), column=0, sticky="w", pady=4)
        ttk.Entry(frame, width=48, textvariable=repo).grid(row=len(SETTINGS_FIELDS), column=1, padx=(12, 0), pady=4)
        show = tk.BooleanVar(value=False)

        def toggle_show():
            for key, _, secret in SETTINGS_FIELDS:
                if secret:
                    entries[key].config(show="" if show.get() else "•")
        ttk.Checkbutton(frame, text="Показать скрытые значения", variable=show, command=toggle_show).grid(
            row=len(SETTINGS_FIELDS) + 1, column=1, sticky="w", pady=(8, 0))

        def save():
            write_env({k: e.get().strip() for k, e in entries.items()})
            settings = updater.settings()
            settings["repo"] = repo.get().strip()
            updater.SETTINGS.write_text(__import__("json").dumps(settings, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
            win.destroy()
            if messagebox.askyesno("Настройки", "Сохранено. Перезапустить бота, чтобы применить?", parent=self.root):
                self.run_bg(self.bot.restart)
        ttk.Button(frame, text="Сохранить", style="Accent.TButton", command=save).grid(
            row=len(SETTINGS_FIELDS) + 2, column=1, sticky="e", pady=(14, 0))

    def telegram_login(self) -> None:
        env = read_env()
        if not (env.get("API_ID") and env.get("API_HASH")):
            messagebox.showinfo("Вход в Telegram", "Сначала впишите API_ID и API_HASH в «Настройки» "
                                "(их выдают на my.telegram.org → API development tools).", parent=self.root)
            return
        if self.bot.running:
            # бот и вход одновременно открыли бы один файл сессии — Telegram может отозвать вход
            if not messagebox.askyesno("Вход в Telegram", "На время входа бот будет остановлен. Продолжить?",
                                       parent=self.root):
                return
            self.bot.stop()
        # отдельное окно консоли: там вводятся номер и код, приложение их не видит и не хранит
        subprocess.Popen(["cmd", "/k", python_exe(), "login.py"], cwd=APP_DIR,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        messagebox.showinfo("Вход в Telegram", "В открывшемся окне введите номер телефона и код из Telegram. "
                            "Когда появится «Готово», закройте его и нажмите «Запустить».", parent=self.root)

    # --- трей и окно ---
    def _tray(self) -> None:
        try:
            import pystray
            from PIL import Image
        except ImportError:
            log.warning("pystray не установлен — без значка в трее")
            return
        image = Image.open(ICON) if ICON.exists() else Image.new("RGB", (64, 64), C["accent"])
        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda: self.later(self.show), default=True),
            pystray.MenuItem("Перезапустить бота", lambda: self.run_bg(self.bot.restart)),
            pystray.MenuItem("Выход (остановить бота)", lambda: self.later(self.quit)),
        )
        self.tray = pystray.Icon("GiftBot", image, "GiftBot", menu)
        self.tray.run()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()
        if self.tray and not getattr(self, "_hint_shown", False):
            self._hint_shown = True
            try:
                self.tray.notify("Бот работает в фоне. Открыть — значок у часов.", "GiftBot")
            except Exception:
                pass

    def quit(self, stop_bot: bool = True, release_only: bool = False) -> None:
        if stop_bot:
            self.bot.stop()
        else:
            self.bot.want = False
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def single_instance() -> socket.socket | None:
    """Занимает порт; если занят — просит уже открытое окно показаться."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLE_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        try:
            with socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=2) as c:
                c.sendall(b"show")
        except OSError:
            pass
        return None


def main() -> None:
    sock = None
    for _ in range(10 if "--restarted" in sys.argv else 1):  # после обновления старое окно ещё закрывается
        sock = single_instance()
        if sock:
            break
        time.sleep(1)
    if not sock:
        return
    app = App(minimized="--minimized" in sys.argv)

    def listen():
        while True:
            try:
                conn, _ = sock.accept()
                conn.close()
                app.later(app.show)
            except OSError:
                return
    threading.Thread(target=listen, daemon=True).start()
    app.root.mainloop()
    sock.close()


if __name__ == "__main__":
    main()
