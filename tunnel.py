"""Бесплатный https-адрес для мини-приложения без сервера и домена.
Лучший вариант — ngrok с постоянным бесплатным доменом (NGROK_AUTHTOKEN и NGROK_DOMAIN в .env):
адрес не меняется никогда. Без него — Cloudflare Quick Tunnel (https://xxx.trycloudflare.com), если он недоступен —
localhost.run через встроенный в Windows ssh (https://xxx.lhr.life).
Адрес меняется при каждом запуске — бот сам обновляет кнопку меню."""
import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("tunnel")

# api.trycloudflare.com — служебный адрес из сообщений об ошибках, не наш туннель
_CF_URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
_LR_URL_RE = re.compile(r"https://[a-z0-9-]+\.lhr\.life")
_CF_PATHS = [
    Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
    Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
]
_SSH_PATHS = [Path(r"C:\Windows\System32\OpenSSH\ssh.exe")]
_NGROK_PATHS = [Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ngrok.exe"))]
_NGROK_URL_RE = re.compile(r"(?<=url=)https://[a-z0-9.-]+")


def _find(name: str, paths: list[Path]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    return next((str(p) for p in paths if p.exists()), None)


def find_cloudflared() -> str | None:
    return _find("cloudflared", _CF_PATHS)


def find_ssh() -> str | None:
    return _find("ssh", _SSH_PATHS)


def find_ngrok() -> str | None:
    return _find("ngrok", _NGROK_PATHS)


class Tunnel:
    def __init__(
        self, port: int, on_change: Callable[[str], Awaitable[None]] | None = None,
        ngrok_token: str = "", ngrok_domain: str = "",
    ):
        self.port = port
        self.ngrok_token = ngrok_token
        self.ngrok_domain = ngrok_domain.removeprefix("https://").strip("/")
        self.on_change = on_change
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None

    def _providers(self) -> list[tuple[str, list[str], re.Pattern]]:
        local = f"127.0.0.1:{self.port}"
        result = []
        if self.ngrok_token and self.ngrok_domain and (ngrok := find_ngrok()):
            result.append(("ngrok", [
                ngrok, "http", local, f"--url=https://{self.ngrok_domain}", f"--authtoken={self.ngrok_token}",
                "--log=stdout", "--log-format=logfmt",
            ], _NGROK_URL_RE))
        if cf := find_cloudflared():
            result.append(("cloudflare", [cf, "tunnel", "--no-autoupdate", "--url", f"http://{local}"], _CF_URL_RE))
        if ssh := find_ssh():
            result.append(("localhost.run", [
                ssh, "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
                "-o", "ExitOnForwardFailure=yes", "-R", f"80:{local}", "nokey@localhost.run",
            ], _LR_URL_RE))
        return result

    def available(self) -> bool:
        return bool(self._providers())

    async def start(self, timeout: float = 40) -> str:
        errors = []
        for name, cmd, url_re in self._providers():
            try:
                return await self._start_one(name, cmd, url_re, timeout)
            except Exception as e:
                await self.stop()
                errors.append(f"{name}: {e or type(e).__name__}")
        raise RuntimeError("; ".join(errors) or "нет ни cloudflared, ни ssh")

    async def _start_one(self, name: str, cmd: list[str], url_re: re.Pattern, timeout: float) -> str:
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,  # ssh держит сессию, пока stdin открыт
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.url = await asyncio.wait_for(self._next_url(url_re), timeout)
        # Читаем вывод дальше: чтобы буфер не переполнился и чтобы поймать смену адреса
        # (localhost.run на бесплатном тарифе иногда выдаёт новый)
        asyncio.create_task(self._watch(url_re))
        log.info("Туннель (%s): %s", name, self.url)
        return self.url

    async def _next_url(self, url_re: re.Pattern) -> str:
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                raise RuntimeError("завершился, не выдав адрес")
            m = url_re.search(line.decode(errors="ignore"))
            if m:
                return m.group(0)

    async def _watch(self, url_re: re.Pattern) -> None:
        proc = self.proc
        while proc and (line := await proc.stdout.readline()):
            m = url_re.search(line.decode(errors="ignore"))
            if m and m.group(0) != self.url:
                self.url = m.group(0)
                log.info("Туннель сменил адрес: %s", self.url)
                if self.on_change:
                    await self.on_change(self.url)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()
