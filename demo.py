
#!/usr/bin/env python3
"""Scrape video pages and generate JSON/M3U playlists."""

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
# VIDEO QUALITY PARSER
# -----------------------------------------

def parse_qualities(raw_stream, page_url=""):
    """
    Example input:
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

    raw_stream = html.unescape(str(raw_stream))
    raw_stream = raw_stream.replace("\\/", "/")
    raw_stream = raw_stream.strip()

    parts = re.split(
        r",\s*\[([^\]]+)\]\s*",
        raw_stream
    )

    qualities = {}

    first_url = parts[0].strip().strip(",")

    if first_url:
        qualities["Auto"] = urljoin(
            page_url, first_url
        )

    for index in range(1, len(parts) - 1, 2):
        quality = parts[index].strip()
        stream_url = parts[index + 1].strip()

        if not quality or not stream_url:
            continue

        stream_url = stream_url.strip(",")

        qualities[quality] = urljoin(
            page_url, stream_url
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
# SCROLL PAGE
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

        unchanged = (
            unchanged + 1
            if height == last_height
            else 0
        )

        if unchanged >= 2:
            break

        last_height = height


# -----------------------------------------
# WEBSITE URL HELPERS
# -----------------------------------------

def same_site(url):
    return (
        urlsplit(url).netloc.lower()
        == urlsplit(BASE_URL).netloc.lower()
    )


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


# -----------------------------------------
# NEXT PAGE
# -----------------------------------------

def find_next_page(markup, current_url):
    soup = BeautifulSoup(
        markup,
        "html.parser"
    )

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
        ".pagination, .page-numbers, .pager, "
        "nav[aria-label*='agination']"
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

        if (
            same_site(url)
            and url != urldefrag(current_url)[0]
        ):
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
                        url = urljoin(
                            current_url,
                            anchor.get("href", "")
                        )

                        if (
                            same_site(url)
                            and url != current_url
                        ):
                            return urldefrag(url)[0]

    return None


# -----------------------------------------
# MODEL AVATAR
# -----------------------------------------

def get_model_avatar(driver):
    soup = BeautifulSoup(
        driver.page_source,
        "html.parser"
    )

    image = soup.select_one(
        "img.avatar, "
        "img.model-avatar, "
        "div.avatar img, "
        "img[alt*='model'], "
        "img.photo, "
        "img[src*='avatar']"
    )

    if image and image.get("src"):
        return urljoin(
            driver.current_url,
            image.get("src", "")
        )

    return ""


# -----------------------------------------
# EXTRACT VIDEO INFORMATION
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

        # Find MP4 or M3U8 stream.
        # Also preserve attached quality links.
        pattern = (
            r'''https?://[^\s"'<>\\]+?'''
            r'''\.(?:m3u8|mp4)'''
            r'''(?:\?[^\s"'<>\\]*)?'''
        )

        match = re.search(
            pattern,
            text,
            flags=re.I
        )

        if match:
            stream = match.group()

    if not stream:
        return None

    # Split Auto and quality URLs.
    qualities = parse_qualities(
        stream,
        video_url
    )

    auto_url = get_auto_url(qualities)

    if not auto_url:
        return None

    return {
        "url": auto_url,
        "title": title,
        "thumbnail": thumbnail,
        "qualities": qualities
    }


# -----------------------------------------
# GET VIDEO INFO
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
            "Video page request failed: %s",
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
# SCRAPE MODEL
# -----------------------------------------

def scrape_model(
    model_name,
    max_videos=None,
    headless=False
):
    driver = get_driver(headless)
    results = []

    try:
        page = urljoin(
            BASE_URL + "/",
            f"models/{model_name}/"
        )

        visited_pages = set()
        seen_videos = set()
        video_pages = []
        avatar = ""

        while page and page not in visited_pages:
            log.info(
                "Model %s: listing page %s",
                model_name,
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
                    "No video links found."
                )

            scroll_to_load_all(driver)

            avatar = (
                avatar or get_model_avatar(driver)
            )

            for url in extract_video_urls(driver):
                if url not in seen_videos:
                    seen_videos.add(url)
                    video_pages.append(url)

            if (
                max_videos
                and len(video_pages) >= max_videos
            ):
                video_pages = video_pages[:max_videos]
                break

            page = find_next_page(
                driver.page_source,
                driver.current_url
            )

            time.sleep(REQUEST_DELAY)

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
                    "%s: processing video %s/%s",
                    model_name,
                    index,
                    len(video_pages)
                )

                info = get_video_info(
                    url,
                    session,
                    driver
                )

                if info:
                    results.append({
                        "model": model_name,
                        "title": info["title"],
                        "page_url": url,
                        "stream_url": info["url"],
                        "thumbnail": (
                            info["thumbnail"] or avatar
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

    return results


# -----------------------------------------
# SCRAPE MULTIPLE MODELS
# -----------------------------------------

def scrape_models(
    model_names,
    max_videos=None,
    headless=False
):
    entries = []

    for model in model_names:
        results = scrape_model(
            model,
            max_videos,
            headless
        )

        if not results:
            raise RuntimeError(
                f"No streams found for {model}; "
                "existing output files were preserved."
            )

        entries.extend(results)

    return entries


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
    group_title="pornhao",
    display_prefix=""
):
    lines = ["#EXTM3U", ""]

    for item in entries:
        model = item.get(
            "model",
            "unknown"
        )

        title = clean_attribute(
            display_prefix
            + item.get("title", model)
        )

        # M3U contains only Auto stream URL.
        stream_url = item["stream_url"]

        extinf = (
            f'#EXTINF:-1 '
            f'tvg-id="{clean_attribute(model.lower())}" '
            f'tvg-name="{clean_attribute(model.replace("-", " ").title())}" '
            f'tvg-logo="{clean_attribute(item.get("thumbnail", ""))}" '
            f'group-title="{clean_attribute(group_title)}",'
            f'{title}'
        )

        lines.append(extinf)

        lines.extend([
            stream_url,
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
        temporary = path.with_name(
            path.name + ".tmp"
        )

        temporary.write_text(
            content,
            encoding="utf-8"
        )

        temporary.replace(path)

    log.info(
        "Saved %s entries to %s and %s",
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
            "Scrape video website and "
            "generate JSON and M3U playlists"
        )
    )

    parser.add_argument(
        "--base-url",
        default=BASE_URL
    )

    parser.add_argument(
        "--models",
        default=os.environ.get(
            "MODEL_NAMES",
            "diana-rider,violet-myers,candy-love"
        )
    )

    parser.add_argument(
        "--max",
        type=int,
        default=None
    )

    parser.add_argument(
        "--group-title",
        default="⭐ Favorites"
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

    parsed_url = urlsplit(BASE_URL)

    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
    ):
        parser.error(
            "--base-url must be a valid HTTP/HTTPS URL"
        )

    if args.max is not None and args.max < 1:
        parser.error(
            "--max must be a positive integer"
        )

    models = list(
        dict.fromkeys(
            model.strip()
            for model in args.models.split(",")
            if model.strip()
        )
    )

    if (
        not models
        or any(
            not re.fullmatch(
                r"[A-Za-z0-9_-]+",
                model
            )
            for model in models
        )
    ):
        parser.error(
            "--models must contain valid model slugs"
        )

    try:
        entries = scrape_models(
            models,
            args.max,
            args.headless
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
            "Scraping failed. "
            "Existing playlist files preserved."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
