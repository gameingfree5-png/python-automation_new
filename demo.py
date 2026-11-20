
#!/usr/bin/env python3
"""Scrape up to two homepage listings into JSON and M3U playlists."""

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = os.getenv(
    "BASE_URL", "https://pornhao.com"
).rstrip("/")

SCROLL_PAUSE = 2
REQUEST_DELAY = 1
SCRIPT_VERSION = "home-2pages-auto-stream-v5"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)


def get_driver(headless=False):
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(90)
    driver.set_script_timeout(30)

    return driver


def open_page(driver, url):
    try:
        driver.get(url)
    except TimeoutException:
        log.warning(
            "Timeout loading %s; trying partially loaded HTML",
            url
        )
        try:
            driver.execute_script("window.stop()")
        except Exception:
            pass

    markup = driver.page_source

    if (
        not markup
        or BeautifulSoup(
            markup, "html.parser"
        ).find("body") is None
    ):
        raise RuntimeError(
            f"No usable HTML loaded: {url}"
        )

    return markup


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

        unchanged = (
            unchanged + 1
            if height == last_height
            else 0
        )

        if unchanged >= 2:
            break

        last_height = height


def same_site(url):
    return (
        urlsplit(url).netloc.lower()
        == urlsplit(BASE_URL).netloc.lower()
    )


def extract_video_urls(driver):
    soup = BeautifulSoup(
        driver.page_source, "html.parser"
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

        url = urldefrag(
            urljoin(driver.current_url, href)
        )[0]

        if (
            same_site(url)
            and "/video/" in urlsplit(url).path
            and url not in seen
        ):
            seen.add(url)
            urls.append(url)

    return urls


def find_next_page(markup, current_url):
    soup = BeautifulSoup(markup, "html.parser")

    candidates = list(
        soup.select(
            'a[rel~="next"], '
            'link[rel~="next"], '
            'a.next, .next a, '
            'a[aria-label="Next"], '
            'a[aria-label="Next page"]'
        )
    )

    pagination = soup.select(
        ".pagination, .page-numbers, .pager, "
        "nav[aria-label*='agination']"
    )

    for anchor in soup.select("a[href]"):
        label = " ".join((
            anchor.get_text(" ", strip=True),
            anchor.get("aria-label", ""),
            anchor.get("title", "")
        )).lower().strip()

        if (
            re.search(
                r"\bnext(?:\s+page)?\b|\bolder posts\b",
                label
            )
            or label in {"›", "»", "→", ">", ">>"}
        ):
            candidates.append(anchor)

    def valid_listing(url):
        path = urlsplit(url).path.lower()

        return (
            same_site(url)
            and url != urldefrag(current_url)[0]
            and "/video/" not in path
            and "/model/" not in path
            and "/models/" not in path
        )

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

        if valid_listing(url):
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
            wanted = str(int(match.group()) + 1)

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

                        if valid_listing(url):
                            return url

    return None


def get_home_avatar(driver):
    soup = BeautifulSoup(
        driver.page_source, "html.parser"
    )

    image = soup.select_one(
        'meta[property="og:image"]'
    )

    if image and image.get("content"):
        return urljoin(
            driver.current_url,
            image["content"]
        )

    return ""


def get_video_model(soup):
    for attr in ("data-model", "data-model-name"):
        tag = soup.select_one(f"[{attr}]")

        if tag and str(tag.get(attr, "")).strip():
            return str(tag[attr]).strip()

    for key, value in (
        ("name", "model"),
        ("property", "video:model")
    ):
        tag = soup.find(
            "meta", attrs={key: value}
        )

        if tag and tag.get("content", "").strip():
            return tag["content"].strip()

    for scope in soup.select(
        "main article, article.video, "
        ".video-info, .video-details, main"
    ):
        anchor = scope.select_one(
            'a[href*="/models/"], '
            'a[href*="/model/"]'
        )

        if anchor:
            match = re.search(
                r"/models?/([^/]+)/?",
                urlsplit(
                    anchor.get("href", "")
                ).path,
                re.I
            )

            if match:
                return match.group(1)

    return "home"


def parse_video_info(markup, video_url):
    """Preserve the original stream extraction behavior."""

    soup = BeautifulSoup(
        markup, "html.parser"
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
        "meta", property="og:image"
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

    thumbnail = (
        urljoin(video_url, thumbnail)
        if thumbnail
        else ""
    )

    stream = (
        video.get("src", "")
        if video
        else ""
    )

    if not stream:
        source = soup.select_one(
            "video source[src]"
        )

        stream = (
            source.get("src", "")
            if source
            else ""
        )

    if not stream:
        text = html.unescape(markup)
        text = text.replace("\\/", "/")

        for extension in ("m3u8", "mp4"):
            match = re.search(
                r'''https?://[^\s"'<>\\]+?\.'''
                + extension
                + r'''(?:\?[^\s"'<>\\]*)?''',
                text,
                flags=re.I
            )

            if match:
                stream = match.group()
                break

    if not stream:
        return None

    return {
        "url": urljoin(
            video_url,
            html.unescape(stream)
        ),
        "title": title,
        "thumbnail": thumbnail,
        "model": get_video_model(soup)
    }


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
            response.text, video_url
        )

        if info:
            return info

    except requests.RequestException as exc:
        log.warning(
            "Video page request failed: %s",
            type(exc).__name__
        )

    if driver is not None:
        try:
            open_page(driver, video_url)
        except RuntimeError:
            log.warning(
                "Video page had no usable HTML: %s",
                video_url
            )
            return None

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
            driver.page_source, video_url
        )

    return None


def parse_qualities(raw_stream):
    """Keep all available quality URLs separately."""

    raw_stream = html.unescape(
        str(raw_stream or "")
    ).replace("\\/", "/").strip()

    if not raw_stream:
        return {}

    parts = re.split(
        r",\s*\[([^\]]+)\]\s*",
        raw_stream
    )

    qualities = {
        "Auto": parts[0].strip()
    }

    for index in range(1, len(parts) - 1, 2):
        label = parts[index].strip()
        url = parts[index + 1].strip()

        if label and url:
            qualities[label] = url

    return qualities


def scrape_home(
    max_videos=None,
    headless=False,
    max_pages=2
):
    driver = get_driver(headless)
    results = []

    try:
        page = BASE_URL + "/"
        visited_pages = set()
        seen_videos = set()
        video_pages = []
        avatar = ""

        while (
            page
            and page not in visited_pages
            and len(visited_pages) < max_pages
        ):
            log.info(
                "Home: listing page %s/%s",
                len(visited_pages) + 1,
                max_pages
            )

            visited_pages.add(page)
            open_page(driver, page)

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
                    "No video links found on listing page"
                )

            try:
                scroll_to_load_all(driver)
            except TimeoutException:
                log.warning(
                    "Scrolling timed out; collecting available links"
                )

            avatar = avatar or get_home_avatar(driver)

            for url in extract_video_urls(driver):
                if url not in seen_videos:
                    seen_videos.add(url)
                    video_pages.append(url)

            if max_videos and len(video_pages) >= max_videos:
                video_pages = video_pages[:max_videos]
                break

            page = find_next_page(
                driver.page_source,
                driver.current_url
            )

            time.sleep(REQUEST_DELAY)

        log.info(
            "Home: discovered %s videos across %s pages",
            len(video_pages),
            len(visited_pages)
        )

        if not video_pages:
            raise RuntimeError(
                "No homepage video links discovered"
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

            for index, url in enumerate(video_pages, 1):
                log.info(
                    "Home: processing video %s/%s",
                    index,
                    len(video_pages)
                )

                try:
                    info = get_video_info(
                        url, session, driver
                    )
                except Exception:
                    log.exception(
                        "Video processing failed: %s",
                        url
                    )
                    info = None

                if info:
                    raw_stream = info["url"]

                    # Extract qualities BEFORE trimming.
                    qualities = parse_qualities(
                        raw_stream
                    )

                    # Save ONLY the first Auto URL.
                    auto_url = raw_stream.split(
                        ",[", 1
                    )[0].strip()

                    results.append({
                        "model": (
                            info.get("model") or "home"
                        ),
                        "title": info["title"],
                        "page_url": url,
                        "stream_url": auto_url,
                        "thumbnail": (
                            info["thumbnail"] or avatar
                        ),
                        "qualities": qualities
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
            "No streams found on homepage; "
            "existing playlist preserved"
        )

    return results


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
    display_prefix="⭐ "
):
    lines = ["#EXTM3U", ""]

    for item in entries:
        model = item.get("model", "home")

        title = clean_attribute(
            display_prefix
            + item.get("title", model)
        )

        lines.append(
            f'#EXTINF:-1 '
            f'tvg-id="{clean_attribute(model.lower())}" '
            f'tvg-name="{clean_attribute(model.title())}" '
            f'tvg-logo="{clean_attribute(item.get("thumbnail", ""))}" '
            f'group-title="{clean_attribute(group_title)}",'
            f'{title}'
        )

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

    for path, content in outputs:
        temp = path.with_name(
            path.name + ".tmp"
        )

        temp.write_text(
            content,
            encoding="utf-8"
        )
        temp.replace(path)

    log.info(
        "Saved %s entries to %s and %s",
        len(entries),
        json_file,
        m3u_file
    )


def main():
    global BASE_URL

    parser = argparse.ArgumentParser(
        description="Scrape first two homepage listing pages"
    )

    parser.add_argument(
        "--base-url", default=BASE_URL
    )
    parser.add_argument(
        "--max", type=int, default=None
    )
    parser.add_argument(
        "--max-pages", type=int, default=3
    )
    parser.add_argument(
        "--group-title", default="home"
    )
    parser.add_argument(
        "--display-prefix", default="⭐ "
    )
    parser.add_argument(
        "--output-json", default="playlist.json"
    )
    parser.add_argument(
        "--output-m3u", default="playlist.m3u"
    )
    parser.add_argument(
        "--headless", action="store_true"
    )

    args = parser.parse_args()
    BASE_URL = args.base_url.rstrip("/")

    if (
        urlsplit(BASE_URL).scheme not in {"http", "https"}
        or not urlsplit(BASE_URL).netloc
    ):
        parser.error(
            "--base-url must be an HTTP or HTTPS URL"
        )

    if args.max is not None and args.max < 1:
        parser.error("--max must be positive")

    if args.max_pages < 1:
        parser.error("--max-pages must be positive")

    log.info(
        "Scraper version: %s; base URL: %s; max pages: %s",
        SCRIPT_VERSION,
        BASE_URL,
        args.max_pages
    )

    try:
        entries = scrape_home(
            args.max,
            args.headless,
            args.max_pages
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
            "Refresh failed. Existing playlist files "
            "preserved if scraping failed"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
