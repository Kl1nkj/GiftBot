import time

from curl_cffi.requests import AsyncSession

from models import Listing


class Source:
    name: str = ""
    interval: float = 15  # секунд между опросами

    def __init__(self):
        self.http = AsyncSession(impersonate="chrome", timeout=25)
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.last_count = 0
        self.seeded = False  # первый успешный опрос только запоминает лоты, без уведомлений
        self.next_run = 0.0

    @property
    def available(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str:
        return ""

    async def fetch(self, wanted_collections: set[str] | None) -> list[Listing]:
        """wanted_collections: нормализованные названия коллекций из фильтров, None = нужны все."""
        raise NotImplementedError

    async def load_details(self, item: Listing) -> None:
        """Догружает модель/фон/символ, если площадка не отдаёт их в списке."""

    def mark_ok(self, count: int) -> None:
        self.last_ok = time.time()
        self.last_error = None
        self.last_count = count

    async def close(self) -> None:
        await self.http.close()
