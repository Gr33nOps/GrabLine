"""Batch import (F2.4): pull URLs out of pasted text, and expand number/letter
range patterns like ``file[1-100].jpg`` or ``img[a-f].png`` into many URLs."""

from __future__ import annotations

import re
from itertools import product

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
#: Punctuation that is far more likely to trail a URL in prose than end one.
_TRAILING = ".,;:!?)]}'\""

#: A [start-end] range: numbers (optionally zero-padded) or single letters.
_RANGE = re.compile(r"\[(\d+)-(\d+)\]|\[([a-zA-Z])-([a-zA-Z])\]")
#: Cap the expansion so a typo like [1-100000000] can't flood the queue.
MAX_EXPANSION = 1000


def extract_urls(text: str) -> list[str]:
    """Every http(s) URL in ``text``, de-duplicated, in order of appearance."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL.findall(text):
        url = match.rstrip(_TRAILING)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _range_values(match: re.Match[str]) -> list[str]:
    if match.group(1) is not None:  # numeric range
        start, end = match.group(1), match.group(2)
        lo, hi = int(start), int(end)
        step = 1 if lo <= hi else -1
        width = len(start) if start.startswith("0") and len(start) > 1 else 0
        return [str(n).zfill(width) for n in range(lo, hi + step, step)]
    lo, hi = ord(match.group(3)), ord(match.group(4))  # letter range
    step = 1 if lo <= hi else -1
    return [chr(c) for c in range(lo, hi + step, step)]


def expand_pattern(url: str) -> list[str]:
    """Expand ``[1-100]`` / ``[a-f]`` ranges in a URL into concrete URLs.

    Multiple ranges multiply out (cartesian product), capped at
    ``MAX_EXPANSION``. A URL with no range comes back unchanged.
    """
    matches = list(_RANGE.finditer(url))
    if not matches:
        return [url]
    value_lists = [_range_values(m) for m in matches]
    total = 1
    for values in value_lists:
        total *= len(values)
    if total > MAX_EXPANSION or total == 0:
        return [url]  # refuse a runaway expansion; treat it as a literal URL
    results: list[str] = []
    for combo in product(*value_lists):
        out, last = [], 0
        for match, value in zip(matches, combo, strict=True):
            out.append(url[last : match.start()])
            out.append(value)
            last = match.end()
        out.append(url[last:])
        results.append("".join(out))
    return results


def expand_all(urls: list[str]) -> list[str]:
    """Expand every URL's range patterns, de-duplicated, order preserved."""
    seen: set[str] = set()
    expanded: list[str] = []
    for url in urls:
        for candidate in expand_pattern(url):
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return expanded
