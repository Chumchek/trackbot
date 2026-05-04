from __future__ import annotations

import re
import logging
import time
from typing import Dict, Iterable, List, Tuple

import httpx

from config import CHECK_VERBOSE_LOGS, HTTP_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


def _short_url(u: str, max_len: int = 96) -> str:
    return u if len(u) <= max_len else u[: max_len - 3] + "..."


# JSON-LD / og-теги зазвичай у перших сотнях КБ; повна сторінка Play — кілька МБ.
# Regex з «.*» на всьому HTML давав десятки секунд CPU (катастрофічний бектрекінг).
_HTML_META_PREFIX_BYTES = 1600_000

# Removed old patterns - now using more specific text-based detection

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Gecko/20100101 Firefox/119.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


async def is_google_play_available(link: str) -> bool:
    available, _ = await fetch_app_metadata(link)
    return available


def _ensure_en_params(link: str) -> str:
    if "hl=" in link or "gl=" in link:
        return link
    sep = "&" if "?" in link else "?"
    return f"{link}{sep}hl=en&gl=US"


def _extract_json_field(block: str, field: str) -> str | None:
    m = re.search(rf'"{re.escape(field)}"\s*:\s*"(.*?)"', block, re.DOTALL)
    if m:
        return re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), m.group(1))
    return None


def _extract_screenshots_from_img_tags(html: str) -> List[str]:
    """Будь-який порядок атрибутів у <img>: src + data-screenshot-index (актуально для Play)."""
    indexed: List[tuple[int, str]] = []
    for m in re.finditer(r"<img\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if "play-lh.googleusercontent.com" not in tag or "data-screenshot-index" not in tag:
            continue
        idx_m = re.search(r'data-screenshot-index=["\'](\d+)["\']', tag, re.IGNORECASE)
        src_m = re.search(
            r'src=["\'](https://play-lh\.googleusercontent\.com/[^"\']+)["\']',
            tag,
            re.IGNORECASE,
        )
        if not idx_m or not src_m:
            continue
        indexed.append((int(idx_m.group(1)), src_m.group(1)))
    if not indexed:
        return []
    indexed.sort(key=lambda x: x[0])
    return [u for _, u in indexed]


def _extract_screenshots(html: str) -> List[str]:
    """Extract screenshot URLs from Google Play HTML (повний HTML — блоки часто нижче 2 МБ)."""
    screenshots: List[str] = []

    # 1) Найнадійніше: data-screenshot-index + play-lh (атрибути в довільному порядку)
    screenshots = _extract_screenshots_from_img_tags(html)
    if not screenshots:
        # 2) Старі жорсткі патерни (src перед alt)
        screenshot_pattern = (
            r'<img[^>]+src="(https://play-lh\.googleusercontent\.com/[^"]+)"[^>]*alt="Screenshot image"[^>]*data-screenshot-index="(\d+)"'
        )
        matches = re.findall(screenshot_pattern, html, re.IGNORECASE)
        if matches:
            sorted_matches = sorted(matches, key=lambda x: int(x[1]))
            screenshots = [url for url, _ in sorted_matches]
        else:
            alt_pattern = r'<img[^>]+src="(https://play-lh\.googleusercontent\.com/[^"]+)"[^>]*alt="Screenshot image"'
            screenshots = re.findall(alt_pattern, html, re.IGNORECASE)

    # 3) Контейнер ULeU3b: DOTALL лише у вікнах, щоб покрити всю сторінку без 60-сек regex на 5 МБ
    if not screenshots:
        container_pattern = r'<div[^>]*class="[^"]*ULeU3b[^"]*"[^>]*>.*?<img[^>]+src="(https://play-lh\.googleusercontent\.com/[^"]+)"'
        window, step = 700_000, 350_000
        for start in range(0, max(1, len(html)), step):
            chunk = html[start : start + window]
            found = re.findall(container_pattern, chunk, re.IGNORECASE | re.DOTALL)
            if found:
                screenshots = found
                break

    # 4) Загальний fallback по всьому HTML (findall лінійний; фільтр іконок)
    if not screenshots:
        generic_pattern = r'<img[^>]+src="(https://play-lh\.googleusercontent\.com/[^"]+)"[^>]*>'
        all_images = re.findall(generic_pattern, html, re.IGNORECASE)
        for img_url in all_images:
            if any(skip_pattern in img_url.lower() for skip_pattern in ("icon", "logo", "badge")):
                continue
            if "=w" in img_url and any(size in img_url for size in ("=w36", "=w48", "=w72")):
                continue
            screenshots.append(img_url)

    seen: set[str] = set()
    unique_screenshots: List[str] = []
    for url in screenshots:
        if url not in seen:
            seen.add(url)
            unique_screenshots.append(url)
    return unique_screenshots


def _parse_metadata_from_html(html: str) -> Dict[str, str | List[str] | None]:
    meta: Dict[str, str | List[str] | None] = {
        "name": None,
        "version": None,
        "developer": None,
        "icon": None,
        "short_desc": None,
        "long_desc": None,
        "updated_on_text": None,
        "screenshots": [],
    }

    html_meta = html if len(html) <= _HTML_META_PREFIX_BYTES else html[:_HTML_META_PREFIX_BYTES]

    # Prefer a JSON-LD SoftwareApplication block
    block = None
    m = re.search(r'\{\s*"@type"\s*:\s*"SoftwareApplication"[\s\S]*?\}', html_meta)
    if m:
        block = m.group(0)

    if block:
        meta["name"] = _extract_json_field(block, "name") or meta["name"]
        meta["version"] = _extract_json_field(block, "softwareVersion") or meta["version"]
        # author/publisher
        mdev = re.search(r'"author"\s*:\s*\{[\s\S]*?"name"\s*:\s*"(.*?)"', block)
        if mdev:
            meta["developer"] = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), mdev.group(1))
        else:
            mpub = re.search(r'"publisher"[\s\S]*?"name"\s*:\s*"(.*?)"', block)
            if mpub:
                meta["developer"] = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), mpub.group(1))
        meta["icon"] = _extract_json_field(block, "image") or meta["icon"]
        # This description tends to be short
        meta["short_desc"] = _extract_json_field(block, "description") or meta["short_desc"]
        # datePublished sometimes present
        dp = _extract_json_field(block, "datePublished")
        if dp:
            meta["updated_on_text"] = dp

    # Additional JSON hints present elsewhere in HTML blobs
    if not meta["developer"]:
        developer_patterns = [
            r'"(developerName|publisherName)"\s*:\s*"(.*?)"',
            # JSON-LD schema pattern found in debugging
            r'"author":\s*\{"@type":"Person","name":"([^"]+)"',
            # Developer link pattern
            r'<a\s+href="/store/apps/developer\?id=([^"]+)"><span>([^<]+)</span></a>',
            r'href="/store/apps/developer\?id=[^"]*">([^<]+)</a>',
        ]
        for pattern in developer_patterns:
            mjsondev = re.search(pattern, html_meta)
            if mjsondev:
                if len(mjsondev.groups()) > 1:
                    # For patterns with multiple groups, use the last one (the name)
                    dev_name = mjsondev.group(-1)
                else:
                    dev_name = mjsondev.group(1)
                meta["developer"] = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), dev_name)
                break

    # Fallbacks from visible HTML
    if not meta["name"]:
        mname = re.search(r'<h1[^>]*?>\s*<span[^>]*?>([^<]+)</span>\s*</h1>', html_meta)
        if mname:
            meta["name"] = mname.group(1)
        else:
            # Try alternative title patterns
            alt_name_patterns = [
                r'<title[^>]*>([^<]+)</title>',
                r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
                r'<h1[^>]*>([^<]+)</h1>',
            ]
            for pattern in alt_name_patterns:
                mname_alt = re.search(pattern, html_meta, re.IGNORECASE)
                if mname_alt:
                    meta["name"] = mname_alt.group(1).strip()
                    break

    if not meta["developer"]:
        # Anchor to developer page
        mdev2 = re.search(r'<a[^>]+href="/store/apps/dev\?id=[^"]+"[^>]*>([^<]+)</a>', html_meta)
        if mdev2:
            meta["developer"] = mdev2.group(1)
        else:
            # "By <span>Developer</span>" pattern
            mdev3 = re.search(r'By\s*<span[^>]*?>([^<]+)</span>', html_meta, re.IGNORECASE)
            if mdev3:
                meta["developer"] = mdev3.group(1)
            else:
                # Try more developer patterns
                dev_patterns = [
                    r'Offered\s+by\s*</div>\s*<div[^>]*>([^<]+)</div>',
                    r'<div[^>]*>Offered by</div>\s*<div[^>]*>([^<]+)</div>',
                    r'<span[^>]*>([^<]+)</span>\s*<span[^>]*>Developer</span>',
                ]
                for pattern in dev_patterns:
                    dev_match = re.search(pattern, html_meta, re.IGNORECASE)
                    if dev_match:
                        meta["developer"] = dev_match.group(1).strip()
                        break

    if not meta["icon"]:
        micon = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html_meta)
        if micon:
            meta["icon"] = micon.group(1)
        else:
            micon2 = re.search(r'<img[^>]+src="(https://play-lh[^"]+)"', html_meta)
            if micon2:
                meta["icon"] = micon2.group(1)

    # Short description from og:description or meta description (this is usually the visible short description)
    if not meta["short_desc"]:
        msd = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html_meta)
        if not msd:
            msd = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html_meta)
        if msd:
            meta["short_desc"] = msd.group(1)

    # Видалено js_patterns (AF_initDataCallback / .* на всьому HTML) — десятки секунд CPU
    # і дублювали версію з інших патернів.

    # Look for version in various patterns, including JSON structures
    if not meta["version"]:
        version_patterns = [
            r'Current\s+Version\s*</div>\s*<span[^>]*?>([^<]+)</span>',
            r'Version\s*</div>\s*<div[^>]*>([^<]+)</div>',
            r'"softwareVersion":\s*"([^"]+)"',
            r'versionName[\'"]:\s*[\'"]([^\'"]+)[\'"]',
        ]
        for pattern in version_patterns:
            version_match = re.search(pattern, html_meta, re.IGNORECASE)
            if version_match:
                meta["version"] = version_match.group(1).strip()
                break

    # Look for updated date in various patterns
    if not meta["updated_on_text"]:
        date_patterns = [
            r'Updated\s+on\s*</div>\s*<span[^>]*?>([^<]+)</span>',
            r'Updated\s+on\s*</div>\s*<div[^>]*>([^<]+)</div>',
            r'"datePublished":\s*"([^"]+)"',
            r'"dateModified":\s*"([^"]+)"',
        ]
        for pattern in date_patterns:
            date_match = re.search(pattern, html_meta, re.IGNORECASE)
            if date_match:
                meta["updated_on_text"] = date_match.group(1).strip()
                break

    # Try to find long description in expandable content or JSON data
    long_desc_patterns = [
        r'"description":\s*"([^"]{100,})"',  # Long descriptions are usually 100+ chars
        r'<div[^>]*data-g-id="description"[^>]*>([^<]{100,})</div>',
        r'<div[^>]*class="[^"]*description[^"]*"[^>]*>([^<]{100,})</div>',
    ]

    for pattern in long_desc_patterns:
        desc_match = re.search(pattern, html_meta, re.IGNORECASE | re.DOTALL)
        if desc_match:
            desc_text = desc_match.group(1).strip()
            # Clean up HTML entities and extra whitespace
            desc_text = re.sub(r'\\n', ' ', desc_text)
            desc_text = re.sub(r'\\t', ' ', desc_text)
            desc_text = re.sub(r'\s+', ' ', desc_text)
            if len(desc_text) > 100:  # Only use if it's actually long
                meta["long_desc"] = desc_text
                break

    # Скріншоти можуть бути далеко внизу HTML; _extract_screenshots сам обмежує важкі DOTALL вікнами
    meta["screenshots"] = _extract_screenshots(html)

    return meta


async def _try_fetch_detailed_info(
    link: str,
    client: httpx.AsyncClient,
    *,
    verbose: bool = False,
) -> Dict[str, str | None]:
    """Try to fetch detailed app info that might be in modal/expandable content."""
    detailed_meta = {}
    t_block = time.perf_counter()

    # Try different URL variations that might give us more data
    variations = [
        link + "&showAllReviews=true",
        link + "&expanded=true",
        link + "&details=true",
        link.replace("details?", "details/") + "?tab=details",
    ]

    for idx, url in enumerate(variations):
        t0 = time.perf_counter()
        try:
            resp = await client.get(url)
            elapsed = time.perf_counter() - t0
            if verbose:
                logger.info(
                    "[check] detailed variant %d/%d HTTP %s %.3fs %s",
                    idx + 1,
                    len(variations),
                    resp.status_code,
                    elapsed,
                    _short_url(url),
                )
            if resp.status_code == 200:
                html = resp.text or ""
                
                # Try to extract version info from this response - multiple patterns
                version_patterns = [
                    r'Version\s*</div>\s*<div[^>]*>([^<]+)</div>',
                    r'"([0-9]+\.[0-9]+(?:\.[0-9]+)?)"[^}]*"Version"',
                    r'"Version"[^}]*"([0-9]+\.[0-9]+(?:\.[0-9]+)?)"',
                    r'"softwareVersion"\s*:\s*"([^"]+)"',
                    r'<div[^>]*>Version</div>\s*<div[^>]*>([^<]+)</div>',
                    r'Version[^<]*<[^>]*>([0-9]+\.[0-9]+(?:\.[0-9]+)?)',
                ]
                for pattern in version_patterns:
                    version_match = re.search(pattern, html, re.IGNORECASE)
                    if version_match and not detailed_meta.get("version"):
                        detailed_meta["version"] = version_match.group(1).strip()
                        break
                
                # Try to extract update date
                date_match = re.search(r'Updated\s+on\s*</div>\s*<div[^>]*>([^<]+)</div>', html, re.IGNORECASE)
                if date_match and not detailed_meta.get("updated_on_text"):
                    detailed_meta["updated_on_text"] = date_match.group(1).strip()
                
                # Try to extract developer info
                developer_patterns = [
                    r'Offered\s+by\s*</div>\s*<div[^>]*>([^<]+)</div>',
                    r'"author"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
                    r'"publisher"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
                    r'<div[^>]*>Offered by</div>\s*<div[^>]*>([^<]+)</div>',
                ]
                for pattern in developer_patterns:
                    dev_match = re.search(pattern, html, re.IGNORECASE)
                    if dev_match and not detailed_meta.get("developer"):
                        detailed_meta["developer"] = dev_match.group(1).strip()
                        break
                
                # Look for expanded description content
                long_desc_match = re.search(r'<div[^>]*class="[^"]*bARER[^"]*"[^>]*>(.*?)</div>', html, re.IGNORECASE | re.DOTALL)
                if long_desc_match and not detailed_meta.get("long_desc"):
                    desc_text = long_desc_match.group(1).strip()
                    # Clean HTML tags
                    desc_text = re.sub(r'<[^>]+>', '', desc_text)
                    desc_text = re.sub(r'\s+', ' ', desc_text).strip()
                    if len(desc_text) > 100:
                        detailed_meta["long_desc"] = desc_text
                
                # If we got some data, no need to try other variations
                if detailed_meta:
                    break

        except Exception as e:
            if verbose:
                logger.info(
                    "[check] detailed variant %d/%d error %.3fs %s: %s",
                    idx + 1,
                    len(variations),
                    time.perf_counter() - t0,
                    _short_url(url),
                    e,
                )
            continue

    if verbose:
        logger.info("[check] detailed block total %.3fs keys=%s", time.perf_counter() - t_block, list(detailed_meta.keys()))
    return detailed_meta


async def fetch_app_metadata(link: str) -> Tuple[bool, Dict[str, str | List[str] | None]]:
    """
    Simplified app availability check:
    - HTTP 200 = app is alive (available)
    - HTTP 400+ = app is dead (unavailable)
    - Only check for explicit "not found" indicators on HTTP 200
    """
    v = CHECK_VERBOSE_LOGS
    t_all = time.perf_counter()
    url = _ensure_en_params(link)
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=_HEADERS) as client:
        t0 = time.perf_counter()
        try:
            resp = await client.get(url)
        except httpx.RequestError as e:
            if v:
                logger.info("[check] primary GET failed %.3fs %s err=%s", time.perf_counter() - t0, _short_url(url), e)
            else:
                logger.warning("Request error for %s: network/connection issue", link)
            return False, {}

        t_main = time.perf_counter() - t0
        if v:
            logger.info("[check] primary GET HTTP %s %.3fs %s", resp.status_code, t_main, _short_url(url))

        # Simple HTTP status logic: 200 = alive, 400+ = dead
        if resp.status_code >= 400:
            if v:
                logger.info("[check] result unavailable http=%s total %.3fs", resp.status_code, time.perf_counter() - t_all)
            return False, {}

        # HTTP 200 - app is available, but check for explicit "not found" indicators
        text = resp.text or ""
        text_lower = text.lower()

        not_found_indicators = [
            "we're sorry, the requested url was not found",
            "item not found",
            "this app is no longer available",
            "this app has been removed from google play",
            "this app is no longer available on google play",
            "app removed from store",
            "content removed",
            "unavailable for download",
        ]

        for indicator in not_found_indicators:
            if indicator in text_lower:
                if v:
                    logger.info("[check] result unavailable not_found_indicator total %.3fs", time.perf_counter() - t_all)
                return False, {}

        meta = _parse_metadata_from_html(text)

        try:
            t_det = time.perf_counter()
            detailed_info = await _try_fetch_detailed_info(link, client, verbose=v)
            if v:
                logger.info("[check] _try_fetch_detailed_info wall %.3fs", time.perf_counter() - t_det)
            for key, value in detailed_info.items():
                if value and (not meta.get(key) or len(str(value)) > len(str(meta.get(key, "")))):
                    meta[key] = value
        except Exception as e:
            logger.debug("Detailed info fetch failed for %s: %s", link, e)

        if v:
            logger.info("[check] fetch_app_metadata total %.3fs available=True", time.perf_counter() - t_all)
        return True, meta
