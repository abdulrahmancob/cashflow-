import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Locator, Page

from logging_config import get_logger

log = get_logger("human")


@dataclass
class HumanSettings:
    action_delay_min: float = 1.0
    action_delay_max: float = 2.5
    screenshots_enabled: bool = False
    screenshot_every_page: bool = False
    screenshot_dir: Path | None = None
    _step_counter: int = field(default=0, repr=False)

    async def delay(self) -> None:
        pause = random.uniform(self.action_delay_min, self.action_delay_max)
        log.debug("Human pause %.2fs", pause)
        await asyncio.sleep(pause)

    async def screenshot(self, page: Page, step_name: str) -> Path | None:
        if not self.screenshots_enabled or self.screenshot_dir is None:
            return None

        self._step_counter += 1
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{self._step_counter:02d}_{step_name}.png"
        path = self.screenshot_dir / filename
        await page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot saved: %s", path)
        return path


async def human_type(
    locator: Locator,
    text: str,
    settings: HumanSettings,
) -> None:
    await locator.click()
    await settings.delay()
    delay_ms = random.randint(80, 180)
    await locator.press_sequentially(text, delay=delay_ms)
    log.debug("Typed %s chars with ~%sms/char", len(text), delay_ms)


async def human_click(
    locator: Locator,
    page: Page,
    settings: HumanSettings,
) -> None:
    await locator.scroll_into_view_if_needed()
    await settings.delay()
    box = await locator.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await page.mouse.move(x, y, steps=random.randint(8, 16))
        await asyncio.sleep(random.uniform(0.1, 0.3))
    await locator.click()
    log.debug("Human click completed")


async def human_scroll(page: Page, settings: HumanSettings) -> None:
    await settings.delay()
    delta = random.randint(150, 350)
    await page.mouse.wheel(0, delta)
    log.debug("Human scroll %spx", delta)
