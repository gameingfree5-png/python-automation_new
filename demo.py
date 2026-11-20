#!/usr/bin/env python3
"""Scrape two homepage listing pages; preserve the original combined stream format."""

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

BASE_URL = os.environ.get("BASE_URL", "https://pornhao.com").rstrip("/")
SCROLL_PAUSE = 2
REQUEST_DELAY = 1
SCRIPT_VERSION = "home-2pages-original-stream-v3"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def get_driver(headless=False):
    options = webdriver.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)

    # Full page load ka wait nahi karega.
    driver.set_page_load_timeout(90)
    driver.set_script_timeout(30)
    driver.execute_cdp_cmd(
        "Page.enable", {}
    )

    return driver


def scroll_to_load_all(driver, max_scrolls=50):
    last_height = driver.execute_script("return document.body.scrollHeight")
    unchanged = 0
    for _ in range(max_scrolls):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(SCROLL_PAUSE)
        height = driver.execute_script("return document.body.scrollHeight")
        unchanged = unchanged + 1 if height == last_height else 0
        if unchanged >= 2:
            break
        last_height = height


def same_site(url):
    return urlsplit(url).netloc.lower() == urlsplit(BASE_URL).netloc.lower()


def extract_video_urls(driver):
    soup = BeautifulSoup(driver.page_source, "html.parser")
    urls = []
    seen = set()
    for anchor in soup.select("a[href], a[data-href]"):
        href = anchor.get("data-href") or anchor.get("href", "")
        url = urldefrag(urljoin(driver.current_url, href))[0]
        if same_site(url) and "/video/" in urlsplit(url).path and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def find_next_page(markup, current_url):
    soup = BeautifulSoup(markup, "html.parser")
    candidates = list(soup.select(
        'a[rel~="next"], link[rel~="next"], a.next, .next a, '
        'a[aria-label="Next"], a[aria-label="Next page"]'
    ))
    pagination = soup.select(".pagination, .page-numbers, .pager, nav[aria-label*='agination']")
    for anchor in soup.select("a[href]"):
        label = " ".join([
            anchor.get_text(" ", strip=True),
            anchor.get("aria-label", ""),
            anchor.get("title", "")
        ]).lower().strip()
        if re.search(r"\bnext(?:\s+page)?\b|\bolder posts\b", label) or label in {"›", "»", "→", ">", ">>"}:
            candidates.append(anchor)

    def valid_listing(url):
        path = urlsplit(url).path.lower()
        return (same_site(url) and url != urldefrag(current_url)[0]
                and "/video/" not in path
                and "/models/" not in path
                and "/model/" not in path)

    for element in candidates:
