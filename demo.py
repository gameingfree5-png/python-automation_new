
#!/usr/bin/env python3
"""Homepage video scraper with Auto and quality URLs."""

import argparse
import html
import json
import logging
import os
import re
import sys
import time

from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


BASE_URL = os.environ.get(
    "BASE_URL", "https://pornhao.com"
).rstrip("/")

SCROLL_PAUSE = 2
REQUEST_DELAY = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)


# -----------------------------------------
# CHROME DRIVER
# -----------------------------------------

def get_driver(headless=False):
    options = webdriver.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(45)

    return driver


# -----------------------------------------
# URL HELPERS
# -----------------------------------------

def same_site(url):
    base_host = (
        urlsplit(BASE_URL).hostname or ""
    ).lower().removeprefix("www.")

    target_host = (
        urlsplit(url).hostname or ""
    ).lower().removeprefix("www.")

    return (
        urlsplit(url).scheme in {"http", "https"}
        and base_host == target_host
    )


def absolute_url(page_url, value):
    if not value:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\\/", "/")
    value = value.strip()

    return urljoin(page_url, value)


# -----------------------------------------
# QUALITY PARSER
# -----------------------------------------

def parse_qualities(raw_stream, page_url=""):
    """
    Input:
    URL1,[1080p]URL2,[480p]URL3,[360p]URL4

    Output:
    {
        "Auto": URL1,
        "1080p": URL2,
        "480p": URL3,
        "360p": URL4
    }
    """

    if not raw_stream:
        return {}

    raw_stream = html.unescape(
        str(raw_stream)
    ).replace("\\/", "/").strip()

    parts = re.split(
        r",\s*\[([^\]]+)\]\s*",
        raw_stream
    )

    qualities = {}

    first_url = parts[0].strip().strip(",")

    if first_url:
        qualities["Auto"] = absolute_url(
            page_url,
            first_url
        )

    for index in range(1, len(parts) - 1, 2):
        quality = parts[index].strip()
        video_url = parts[index + 1].strip().strip(",")

        if not quality or not video_url:
            continue

        if not re.fullmatch(
            r"(?:\d{3,4}p|auto|original|source)",
            quality,
            flags=re.I
        ):
            continue

        quality = (
            "Auto"
            if quality.lower() == "auto"
            else quality
        )

        qualities[quality] = absolute_url(
            page_url,
            video_url
        )

    return qualities


def get_auto_url(qualities):
    if not qualities:
        return ""

    return (
        qualities.get("Auto")
        or next(iter(qualities.values()), "")
    )


# -----------------------------------------
# EXTRACT STREAM STRING
# -----------------------------------------

def extract_stream(markup, page_url):
    soup = BeautifulSoup(markup, "html.parser")

    video = soup.find("video")

    if video:
        stream = video.get("src", "")

        if stream:
            return stream

        source = video.find("source", src=True)

        if source:
            return source.get("src", "")

    for selector in (
        'meta[property="og:video"]',
        'meta[property="og:video:url"]',
        'meta[property="og:video:secure_url"]'
    ):
        tag = soup.select_one(selector)

        if tag and tag.get("content"):
            return tag["content"]

    text = html.unescape(markup)
    text = text.replace("\\/", "/")

    # Look for streams with quality labels first.
    quality_pattern = (
        r'''https?://[^\s"'<>\\]+?'''
        r'''\.(?:mp4|m3u8)'''
        r'''(?:\?[^\s"'<>\\]*)?'''
        r'''(?:,\s*\[(?:\d{3,4}p|Auto|Original|Source)\]'''
        r'''https?://[^\s"'<>\\]+?'''
        r'''\.(?:mp4|m3u8)'''
        r'''(?:\?[^\s"'<>\\]*)?)*'''
    )

    matches = re.findall(
        quality_pattern,
        text,
        flags=re.I
    )

    if matches:
        # Prefer the match containing quality variants.
        matches.sort(
            key=lambda value: value.count("],["),
            reverse=True
        )

        return matches[0]

    # Fallback to a single stream URL.
    simple_pattern = (
        r'''https?://[^\s"'<>\\,]+?'''
        r'''\.(?:mp4|m3u8)'''
        r'''(?:\?[^\s"'<>\\,]*)?'''
    )

    match = re.search(
        simple_pattern,
        text,
        flags=re.I
    )

    if match:
        return match.group()

    return ""


# -----------------------------------------
# PAGE SCROLLING
# -----------------------------------------

def scroll_to_load_all(driver, max_scrolls=50):
    last_height = driver.execute_script(
        "return document.body.scrollHeight"
    )

    unchanged = 0

    for _ in range(max_scrolls):
        driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight)"
        )

        time.sleep(SCROLL_PAUSE)

        height = driver.execute_script(
            "return document.body.scrollHeight"
        )

        if height == last_height:
            unchanged += 1
        else:
            unchanged = 0

        if unchanged >= 2:
            break

        last_height = height


# -----------------------------------------
# FIND VIDEO LINKS ON HOMEPAGE
# -----------------------------------------

def extract_video_urls(driver):
    soup = BeautifulSoup(
        driver.page_source,
        "html.parser"
    )

    urls = []
    seen = set()

    for anchor in soup.select(
        "a[href], a[data-href]"
    ):
        href = (
            anchor.get("data-href")
            or anchor.get("href", "")
        )

        if not href:
            continue

        url = urldefrag(
            urljoin(driver.current_url, href)
        )[0]

        if not same_site(url):
            continue

        path = urlsplit(url).path

        if "/video/" not in path:
            continue

        if url in seen:
            continue

        seen.add(url)
        urls.append(url)

    return urls


# -----------------------------------------
# HOMEPAGE PAGINATION
# -----------------------------------------

def find_next_page(markup, current_url):
    soup = BeautifulSoup(markup, "html.parser")

    candidates = list(
        soup.select(
            'a[rel~="next"], '
            'link[rel~="next"], '
            'a.next, '
            '.next a, '
            'a[aria-label="Next"], '
            'a[aria-label="Next page"]'
        )
    )

    pagination = soup.select(
        ".pagination, .page-numbers, "
        ".pager, nav[aria-label*='agination']"
    )

    for anchor in soup.select("a[href]"):
        label = " ".join([
            anchor.get_text(" ", strip=True),
            anchor.get("aria-label", ""),
            anchor.get("title", "")
        ]).lower().strip()

        if (
            re.search(
                r"\bnext(?:\s+page)?\b|\bolder posts\b",
                label
            )
            or label in {"›", "»", "→", ">", ">>"}
        ):
            candidates.append(anchor)

    current_clean = urldefrag(current_url)[0]

    def valid_next(url):
        if not same_site(url):
            return False

        if url == current_clean:
            return False

        path = urlsplit(url).path.lower()

        # Do not navigate to model or video pages
        # while collecting homepage listings.
        if "/models/" in path or "/model/" in path:
            return False

        if "/video/" in path:
            return False

        return True

    for element in candidates:
        parent_classes = (
            element.parent.get("class", [])
            if element.parent
            else []
        )

        disabled = (
            " ".join(element.get("class", []))
            + " "
            + " ".join(parent_classes)
        )

        if (
            "disabled" in disabled.lower()
            or element.get("aria-disabled") == "true"
        ):
            continue

        href = element.get("href", "")

        if not href or href.startswith(
            ("#", "javascript:")
        ):
            continue

        url = urldefrag(
            urljoin(current_url, href)
        )[0]

        if valid_next(url):
            return url

    current = soup.select_one(
        '.pagination .current, '
        '.page-numbers.current, '
        '.pagination .active, '
        '[aria-current="page"]'
    )

    if current:
        match = re.search(
            r"\d+",
            current.get_text(" ", strip=True)
        )

        if match:
            wanted = str(
                int(match.group()) + 1
            )

            for container in pagination:
                anchors = (
                    [container]
                    if container.name == "a"
                    else container.select("a[href]")
                )

                for anchor in anchors:
                    if anchor.get_text(strip=True) == wanted:
                        url = urldefrag(
                            urljoin(
                                current_url,
                                anchor.get("href", "")
                            )
                        )[0]

                        if valid_next(url):
                            return url

    return None


# -----------------------------------------
# HOMEPAGE AVATAR FALLBACK
# -----------------------------------------

def get_home_avatar(driver):
    soup = BeautifulSoup(
        driver.page_source,
        "html.parser"
    )

    image = soup.select_one(
        'meta[property="og:image"]'
    )

    if image and image.get("content"):
        return absolute_url(
            driver.current_url,
            image["content"]
        )

    return ""


# -----------------------------------------
# PARSE VIDEO INFORMATION
# -----------------------------------------

def parse_video_info(markup, video_url):
    soup = BeautifulSoup(
        markup,
        "html.parser"
    )

    title_tag = soup.find("title")

    title = (
        title_tag.get_text(" ", strip=True)
        if title_tag
        else "Unknown"
    )

    title = (
        re.sub(
            r"\s*[-|]\s*pornhao.*$",
            "",
            title,
            flags=re.I
        ).strip()
        or "Unknown"
    )

    image = soup.find(
        "meta",
        property="og:image"
    )

    video = soup.find("video")

    thumbnail = (
        image.get("content", "")
        if image
        else ""
    )

    if not thumbnail and video:
        thumbnail = video.get("poster", "")

    if not thumbnail:
        image = soup.find(
            "img",
            class_=re.compile(
                r"thumb|poster|cover|preview"
            )
        )

        thumbnail = (
            image.get("src", "")
            if image
            else ""
        )

    thumbnail = absolute_url(
        video_url,
        thumbnail
    )

    raw_stream = extract_stream(
        markup,
        video_url
    )

    if not raw_stream:
        return None

    qualities = parse_qualities(
        raw_stream,
        video_url
    )

    auto_url = get_auto_url(qualities)

    if not auto_url:
        return None

    return {
        "title": title,
        "url": auto_url,
        "thumbnail": thumbnail,
        "qualities": qualities
    }


# -----------------------------------------
# FETCH INDIVIDUAL VIDEO PAGE
# -----------------------------------------

def get_video_info(
    video_url,
    session=None,
    driver=None
):
    session = session or requests.Session()

    try:
        response = session.get(
            video_url,
            timeout=30,
            headers={
                "Referer": BASE_URL + "/"
            }
        )

        response.raise_for_status()

        info = parse_video_info(
            response.text,
            video_url
        )

        if info:
            return info

    except requests.RequestException as error:
        log.warning(
            "Request failed for %s: %s",
            video_url,
            type(error).__name__
        )

    if driver is not None:
        driver.get(video_url)

        try:
            WebDriverWait(driver, 10).until(
                lambda d: parse_video_info(
                    d.page_source,
                    video_url
                ) is not None
            )

        except TimeoutException:
            return None

        return parse_video_info(
            driver.page_source,
            video_url
        )

    return None


# -----------------------------------------
# SCRAPE ONLY HOMEPAGE
# -----------------------------------------

def scrape_home(
    max_videos=None,
    headless=False,
    max_pages=100
):
    driver = get_driver(headless)
    results = []

    try:
        page = BASE_URL + "/"

        visited_pages = set()
        seen_videos = set()
        video_pages = []
        home_avatar = ""

        while page and page not in visited_pages:
            if len(visited_pages) >= max_pages:
                log.info(
                    "Reached maximum homepage pages: %s",
                    max_pages
                )
                break

            log.info(
                "Home: scanning listing page %s",
                len(visited_pages) + 1
            )

            visited_pages.add(page)

            driver.get(page)

            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located(
                    (By.TAG_NAME, "body")
                )
            )

            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located(
                        (
                            By.CSS_SELECTOR,
                            'a[href*="/video/"], '
                            'a[data-href*="/video/"]'
                        )
                    )
                )

            except TimeoutException:
                log.warning(
                    "No video links found on this page."
                )

            scroll_to_load_all(driver)

            home_avatar = (
                home_avatar
                or get_home_avatar(driver)
            )

            for url in extract_video_urls(driver):
                if url not in seen_videos:
                    seen_videos.add(url)
                    video_pages.append(url)

            log.info(
                "Home: %s unique video pages found",
                len(video_pages)
            )

            if (
                max_videos is not None
                and len(video_pages) >= max_videos
            ):
                video_pages = video_pages[:max_videos]
                break

            page = find_next_page(
                driver.page_source,
                driver.current_url
            )

            time.sleep(REQUEST_DELAY)

        if not video_pages:
            raise RuntimeError(
                "No homepage video links found."
            )

        with requests.Session() as session:
            session.headers["User-Agent"] = (
                driver.execute_script(
                    "return navigator.userAgent"
                )
            )

            for cookie in driver.get_cookies():
                session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", "")
                )

            for index, url in enumerate(
                video_pages, 1
            ):
                log.info(
                    "Home: processing video %s/%s",
                    index,
                    len(video_pages)
                )

                try:
                    info = get_video_info(
                        url,
                        session,
                        driver
                    )

                except Exception as error:
                    log.warning(
                        "Video failed: %s (%s)",
                        url,
                        type(error).__name__
                    )
                    info = None

                if info:
                    results.append({
                        "model": "home",
                        "title": info["title"],
                        "page_url": url,
                        "stream_url": info["url"],
                        "thumbnail": (
                            info["thumbnail"]
                            or home_avatar
                        ),
                        "qualities": info["qualities"]
                    })

                else:
                    log.warning(
                        "No stream found for video %s/%s",
                        index,
                        len(video_pages)
                    )

                time.sleep(REQUEST_DELAY)

    finally:
        driver.quit()

    if not results:
        raise RuntimeError(
            "No playable stream URLs found. "
            "Existing playlist files preserved."
        )

    return results


# -----------------------------------------
# PLAYLIST GENERATION
# -----------------------------------------

def clean_attribute(value):
    return (
        re.sub(r"[\r\n]+", " ", str(value))
        .replace('"', "'")
    )


def save_playlist(
    entries,
    json_file="playlist.json",
    m3u_file="playlist.m3u",
    group_title="home",
    display_prefix=""
):
    lines = ["#EXTM3U", ""]

    for item in entries:
        model = item.get("model", "home")

        title = clean_attribute(
            display_prefix
            + item.get("title", model)
        )

        extinf = (
            f'#EXTINF:-1 '
            f'tvg-id="{clean_attribute(model.lower())}" '
            f'tvg-name="{clean_attribute(model.title())}" '
            f'tvg-logo="{clean_attribute(item.get("thumbnail", ""))}" '
            f'group-title="{clean_attribute(group_title)}",'
            f'{title}'
        )

        lines.append(extinf)

        # M3U contains the Auto stream only.
        lines.extend([
            item["stream_url"],
            ""
        ])

    outputs = [
        (
            Path(json_file),
            json.dumps(
                entries,
                indent=2,
                ensure_ascii=False
            ) + "\n"
        ),
        (
            Path(m3u_file),
            "\n".join(lines)
        )
    ]

    # Prepare output files before replacing originals.
    for path, content in outputs:
        temporary = path.with_name(
            path.name + ".tmp"
        )

        temporary.write_text(
            content,
            encoding="utf-8"
        )

    for path, _ in outputs:
        temporary = path.with_name(
            path.name + ".tmp"
        )

        temporary.replace(path)

    log.info(
        "Saved %s home videos to %s and %s",
        len(entries),
        json_file,
        m3u_file
    )


# -----------------------------------------
# MAIN
# -----------------------------------------

def main():
    global BASE_URL

    parser = argparse.ArgumentParser(
        description=(
            "Scrape homepage videos and "
            "generate JSON/M3U playlists"
        )
    )

    parser.add_argument(
        "--base-url",
        default=BASE_URL
    )

    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Maximum videos; omit for all discovered"
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        help="Maximum homepage listing pages"
    )

    parser.add_argument(
        "--group-title",
        default="home"
    )

    parser.add_argument(
        "--display-prefix",
        default="⭐ "
    )

    parser.add_argument(
        "--output-json",
        default="playlist.json"
    )

    parser.add_argument(
        "--output-m3u",
        default="playlist.m3u"
    )

    parser.add_argument(
        "--headless",
        action="store_true"
    )

    args = parser.parse_args()

    BASE_URL = args.base_url.rstrip("/")

    parsed = urlsplit(BASE_URL)

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
    ):
        parser.error(
            "--base-url must be an HTTP/HTTPS URL"
        )

    if args.max is not None and args.max < 1:
        parser.error(
            "--max must be positive"
        )

    if args.max_pages < 1:
        parser.error(
            "--max-pages must be positive"
        )

    try:
        entries = scrape_home(
            max_videos=args.max,
            headless=args.headless,
            max_pages=args.max_pages
        )

        save_playlist(
            entries,
            args.output_json,
            args.output_m3u,
            args.group_title,
            args.display_prefix
        )

    except Exception:
        log.exception(
            "Homepage scraping failed. "
            "Existing playlists were preserved "
            "if scraping failed."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
