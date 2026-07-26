"""Small browser audit invoked only through the ui_audit check preset."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


_AXE_PATHS = (Path("/usr/local/lib/node_modules/axe-core/axe.min.js"), Path("/usr/lib/node_modules/axe-core/axe.min.js"))


async def _audit(url: str, screenshot: Path | None = None) -> dict[str, object]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed in this repo-ops image.") from exc
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        console_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        response = await page.goto(url, wait_until="networkidle", timeout=30_000)
        title = await page.title()
        axe_path = next((path for path in _AXE_PATHS if path.is_file()), None)
        if axe_path is None:
            raise RuntimeError("axe-core is not installed in the preview image.")
        await page.add_script_tag(path=str(axe_path))
        axe = await page.evaluate(
            """async () => {
              const result = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}});
              return result.violations.map(item => ({id: item.id, impact: item.impact, nodes: item.nodes.length}));
            }"""
        )
        accessibility = await page.evaluate(
            """() => ({
              missing_alt_images: [...document.images].filter(image => !image.alt.trim()).length,
              unnamed_buttons: [...document.querySelectorAll('button')].filter(button => !button.innerText.trim() && !button.getAttribute('aria-label')).length,
              empty_links: [...document.querySelectorAll('a')].filter(link => !link.innerText.trim() && !link.getAttribute('aria-label')).length,
              viewport: {width: window.innerWidth, height: window.innerHeight},
              dom_nodes: document.querySelectorAll('*').length,
              load_ms: Math.round(performance.now()),
            })"""
        )
        if screenshot:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot), full_page=True)
        await browser.close()
    return {
        "url": url,
        "status": response.status if response else None,
        "title": title,
        "console_errors": console_errors,
        "accessibility": accessibility,
        "axe_violations": axe,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    result = asyncio.run(_audit(args.url, args.screenshot))
    print(json.dumps(result))
    accessibility = result["accessibility"]
    if (
        result["status"] is None
        or int(result["status"]) >= 400
        or result["console_errors"]
        or accessibility["missing_alt_images"]
        or accessibility["unnamed_buttons"]
        or accessibility["empty_links"]
        or result["axe_violations"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
