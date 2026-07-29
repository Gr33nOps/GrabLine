"""Batch import (F2.4): URL extraction from pasted text."""

from __future__ import annotations

from app.core.batch import extract_urls


def test_extracts_one_url_per_line():
    text = "https://a.test/one.zip\nhttps://b.test/two.mp4\n"
    assert extract_urls(text) == ["https://a.test/one.zip", "https://b.test/two.mp4"]


def test_extracts_urls_from_prose_and_strips_punctuation():
    text = "grab this (https://a.test/file.pdf), and https://b.test/x?id=1."
    assert extract_urls(text) == ["https://a.test/file.pdf", "https://b.test/x?id=1"]


def test_deduplicates_preserving_order():
    text = "https://a.test/1 https://b.test/2 https://a.test/1"
    assert extract_urls(text) == ["https://a.test/1", "https://b.test/2"]


def test_ignores_non_http_schemes_and_noise():
    text = "ftp://a.test/x file:///etc/passwd javascript:alert(1) hello world"
    assert extract_urls(text) == []


def test_query_strings_and_fragments_survive():
    text = "https://a.test/watch?v=abc&list=xyz#t=30"
    assert extract_urls(text) == ["https://a.test/watch?v=abc&list=xyz#t=30"]
