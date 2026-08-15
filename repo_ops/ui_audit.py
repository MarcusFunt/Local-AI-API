"""Bounded, privacy-preserving browser evidence for disposable UI previews."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


_AXE_PATHS = (Path("/usr/local/lib/node_modules/axe-core/axe.min.js"), Path("/usr/lib/node_modules/axe-core/axe.min.js"))
_MAX_PAGES = 25
_MAX_DEPTH = 3
_VIEWPORT = {"width": 1440, "height": 900}
_MASK_STYLE = """
input, textarea, [contenteditable], [data-sensitive], [data-private],
.message, .chat-message, .conversation, .transcript { color: transparent !important; text-shadow: none !important; }
"""


def _fingerprint(value: str) -> dict[str, int | str]:
    return {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(), "characters": len(value)}


def _same_origin_path(url: str, origin: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if f"{parsed.scheme}://{parsed.netloc}" != origin:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


async def _page_evidence(page, url: str, screenshot: Path | None) -> dict[str, object]:
    await page.add_style_tag(content=_MASK_STYLE)
    axe_path = next((path for path in _AXE_PATHS if path.is_file()), None)
    if axe_path is None:
        raise RuntimeError("axe-core is not installed in the preview image.")
    await page.add_script_tag(path=str(axe_path))
    axe = await page.evaluate(
        """async () => (await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}}))
          .violations.map(item => ({id: item.id, impact: item.impact, nodes: item.nodes.length}))"""
    )
    snapshot = await page.evaluate(
        """() => ({
          tags: [...document.querySelectorAll('*')].reduce((counts, node) => {
            counts[node.tagName.toLowerCase()] = (counts[node.tagName.toLowerCase()] || 0) + 1; return counts;
          }, {}),
          links: [...document.links].map(link => new URL(link.href).pathname).slice(0, 100),
          controls: document.querySelectorAll('button,input,select,textarea,[role=button]').length,
          accessibility: {
            missing_alt_images: [...document.images].filter(image => !image.alt.trim()).length,
            unnamed_buttons: [...document.querySelectorAll('button')].filter(button => !button.innerText.trim() && !button.getAttribute('aria-label')).length,
            empty_links: [...document.querySelectorAll('a')].filter(link => !link.innerText.trim() && !link.getAttribute('aria-label')).length,
            dom_nodes: document.querySelectorAll('*').length,
            load_ms: Math.round(performance.now()),
          },
        })"""
    )
    if screenshot:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshot), full_page=True)
    return {"url": url, "title": _fingerprint(await page.title()), "dom": snapshot, "axe_violations": axe}


def _visual_diff(current: Path, baseline: Path | None, diff_path: Path | None) -> dict[str, object]:
    if baseline is None or not baseline.is_file():
        return {"status": "baseline_created", "changed_fraction": 0.0}
    try:
        from PIL import Image, ImageChops
    except ImportError as exc:  # pragma: no cover - image dependency is in the worker image
        raise RuntimeError("Pillow is required for visual comparison.") from exc
    with Image.open(current).convert("RGBA") as left, Image.open(baseline).convert("RGBA") as right:
        if left.size != right.size:
            return {"status": "different_dimensions", "changed_fraction": 1.0}
        delta = ImageChops.difference(left, right)
        changed = sum(1 for pixel in delta.getdata() if pixel != (0, 0, 0, 0))
        fraction = changed / (left.width * left.height)
        if diff_path and changed:
            diff_path.parent.mkdir(parents=True, exist_ok=True)
            delta.save(diff_path)
        return {"status": "compared", "changed_fraction": round(fraction, 6), "diff": str(diff_path) if diff_path and changed else None}


async def _audit(
    url: str,
    screenshot: Path | None = None,
    *,
    artifact_dir: Path | None = None,
    baseline_dir: Path | None = None,
    max_pages: int = 1,
    max_depth: int = 0,
) -> dict[str, object]:
    """Crawl same-origin pages only and return redacted, bounded visual evidence."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed in this repo-ops image.") from exc
    max_pages = min(_MAX_PAGES, max(1, max_pages))
    max_depth = min(_MAX_DEPTH, max(0, max_depth))
    initial = _same_origin_path(url, f"{urlsplit(url).scheme}://{urlsplit(url).netloc}")
    if initial is None:
        raise ValueError("url must be an absolute HTTP(S) URL.")
    origin = f"{urlsplit(initial).scheme}://{urlsplit(initial).netloc}"
    pages: list[dict[str, object]] = []
    console_errors: list[dict[str, int | str]] = []
    network_failures: list[dict[str, object]] = []
    queue: deque[tuple[str, int]] = deque([(initial, 0)])
    visited: set[str] = set()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_VIEWPORT)

        async def route(route) -> None:
            if _same_origin_path(route.request.url, origin) or route.request.url.startswith("data:"):
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", route)
        while queue and len(pages) < max_pages:
            current, depth = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            page = await context.new_page()
            page.on("console", lambda message: console_errors.append(_fingerprint(message.text)) if message.type == "error" else None)
            page.on("pageerror", lambda error: console_errors.append(_fingerprint(str(error))))
            page.on("requestfailed", lambda request: network_failures.append({"path": urlsplit(request.url).path, "reason": "request_failed"}) if _same_origin_path(request.url, origin) else None)
            response = await page.goto(current, wait_until="networkidle", timeout=30_000)
            if response is None or response.status >= 400:
                network_failures.append({"path": urlsplit(current).path, "status": response.status if response else None})
            name = hashlib.sha256(urlsplit(current).path.encode("utf-8")).hexdigest()[:16]
            capture = screenshot if len(pages) == 0 and screenshot else (artifact_dir / f"{name}.png" if artifact_dir else None)
            evidence = await _page_evidence(page, current, capture)
            if capture:
                baseline = baseline_dir / capture.name if baseline_dir else None
                evidence["visual"] = _visual_diff(capture, baseline, artifact_dir / f"{name}.diff.png" if artifact_dir else None)
            pages.append(evidence)
            if depth < max_depth:
                links = await page.locator("a[href]").evaluate_all("links => links.map(link => link.href)")
                for link in links:
                    candidate = _same_origin_path(urljoin(current, link), origin)
                    if candidate and candidate not in visited:
                        queue.append((candidate, depth + 1))
            await page.close()
        await browser.close()
    violations = [item for page in pages for item in page["axe_violations"]]
    changed = [page["visual"] for page in pages if isinstance(page.get("visual"), dict) and float(page["visual"].get("changed_fraction", 0)) > 0.005]
    return {
        "origin": origin,
        "viewport": _VIEWPORT,
        "pages": pages,
        "console_errors": console_errors[:100],
        "network_failures": network_failures[:100],
        "axe_violations": violations,
        "visual_regressions": changed,
        "passed": not console_errors and not network_failures and not violations and not changed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--max-depth", type=int, default=0)
    args = parser.parse_args()
    result = asyncio.run(_audit(args.url, args.screenshot, artifact_dir=args.artifact_dir, baseline_dir=args.baseline_dir, max_pages=args.max_pages, max_depth=args.max_depth))
    print(json.dumps(result))
    if not result["passed"]:
        raise SystemExit(1)
