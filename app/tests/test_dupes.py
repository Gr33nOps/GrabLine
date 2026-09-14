"""Duplicate detection: only byte-identical files group together."""

from __future__ import annotations

from pathlib import Path

from app.core.dupes import find_duplicates


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_identical_files_group_and_first_stays_first(tmp_path: Path):
    a = _write(tmp_path / "a.bin", b"same-bytes")
    b = _write(tmp_path / "b.bin", b"same-bytes")
    c = _write(tmp_path / "c.bin", b"different!")
    groups = find_duplicates([a, b, c])
    assert groups == [[a, b]]


def test_same_size_different_content_is_not_a_duplicate(tmp_path: Path):
    a = _write(tmp_path / "a.bin", b"aaaa")
    b = _write(tmp_path / "b.bin", b"bbbb")
    assert find_duplicates([a, b]) == []


def test_the_same_file_listed_twice_is_not_a_duplicate(tmp_path: Path):
    a = _write(tmp_path / "a.bin", b"payload")
    assert find_duplicates([a, a, tmp_path / "a.bin"]) == []


def test_missing_files_are_skipped(tmp_path: Path):
    a = _write(tmp_path / "a.bin", b"payload")
    assert find_duplicates([a, tmp_path / "gone.bin"]) == []


def test_multiple_groups(tmp_path: Path):
    a1 = _write(tmp_path / "a1", b"group-a")
    a2 = _write(tmp_path / "a2", b"group-a")
    b1 = _write(tmp_path / "b1", b"group-bb")
    b2 = _write(tmp_path / "b2", b"group-bb")
    groups = find_duplicates([a1, b1, a2, b2])
    assert sorted(tuple(g) for g in groups) == [(a1, a2), (b1, b2)]
