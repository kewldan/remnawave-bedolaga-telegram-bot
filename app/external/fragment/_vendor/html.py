# Vendored from fragment-api-py 12.1.0 (https://github.com/S1qwy/fragment-api-py),
# MIT License, (c) S1qwy. Modified for Bedolaga: see ../NOTICE.md.
"""
HTML parsing utilities for Fragment marketplace pages and item details.

Uses selectolax (lexbor backend) for robust CSS-selector based parsing,
with regex fallback for edge cases. Much more resilient to Fragment
HTML changes than pure-regex approach.
"""

from __future__ import annotations

import re
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from app.external.fragment._vendor.models import StarsPrice


def _text(node: Any) -> str:
    """Safely extract text from a selectolax node."""
    if node is None:
        return ""
    return (node.text(strip=True) or "").strip()


def _attr(node: Any, name: str) -> str:
    """Safely extract an attribute from a selectolax node."""
    if node is None:
        return ""
    return (node.attributes.get(name) or "").strip()


def _clean_price(raw: str) -> str:
    """Clean price string: remove commas and whitespace."""
    return raw.replace(",", "").replace("\xa0", "").strip()


def parse_stars_packages(html: str) -> list[StarsPrice]:
    """Parse stars package prices from stars page HTML."""
    tree = LexborHTMLParser(html)
    packages: list[StarsPrice] = []

    for label in tree.css("label"):
        input_node = label.css_first('input[name="stars"]')
        if not input_node:
            continue
        stars = int(_attr(input_node, "value") or "0")
        if stars == 0:
            continue

        label_html = label.html or ""

        ton_m = re.search(r'icon-ton[^>]*>([^<]*(?:<span[^>]*>[^<]*</span>)?)', label_html)
        ton_raw = re.sub(r'<[^>]+>', '', ton_m.group(1)).replace(',', '').strip() if ton_m else "0"

        usd_m = re.search(r'icon-usd[^>]*>([^<]+)', label_html)
        if not usd_m:
            usd_m = re.search(r'(?:&#036;|\$)\s*([\d.,]+)', label_html)
        usd_raw = usd_m.group(1).replace(',', '').strip() if usd_m else "0"

        packages.append(StarsPrice(stars=stars, gram_price=ton_raw, usd_price=usd_raw))

    return packages


def parse_stars_price_from_html(html: str) -> tuple[str | None, str | None]:
    """Parse GRAM and USD price from inline HTML fragment."""
    ton_m = re.search(r'icon-ton[^>]*>([^<]*(?:<span[^>]*>[^<]*</span>)?)', html)
    gram_price = re.sub(r'<[^>]+>', '', ton_m.group(1)).replace(',', '').strip() if ton_m else None

    usd_m = re.search(r'icon-usd[^>]*>([^<]+)', html)
    if not usd_m:
        usd_m = re.search(r'(?:&#036;|\$)\s*([\d.,]+)', html)
    usd_price = usd_m.group(1).replace(',', '').strip() if usd_m else None

    return gram_price, usd_price
