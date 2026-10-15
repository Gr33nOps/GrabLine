from __future__ import annotations

from pathlib import Path

import pytest

from app.core import naming
from app.core.naming import (
    filename_from_url,
    improved_filename,
    is_ugly_name,
    sanitize_filename,
    unique_path,
)


def test_sanitize_strips_invalid_characters():
    assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j.mp4') == "a_b_c_d_e_f_g_h_i_j.mp4"


def test_sanitize_control_chars_and_dots():
    assert sanitize_filename("..\x00\x1fmovie.mkv..") == "__movie.mkv"


def test_sanitize_empty_becomes_fallback():
    assert sanitize_filename("   ") == "download"
    assert sanitize_filename("...") == "download"


def test_sanitize_windows_reserved_names():
    assert sanitize_filename("CON.txt") == "_CON.txt"
    assert sanitize_filename("com1.tar.gz") == "_com1.tar.gz"


def test_sanitize_caps_length_but_keeps_extension():
    name = sanitize_filename("x" * 400 + ".mp4")
    assert len(name) <= 150
    assert name.endswith(".mp4")


def test_filename_from_url():
    assert filename_from_url("http://x.test/a/My%20Video.mp4?token=1") == "My Video.mp4"
    assert filename_from_url("http://x.test/") == "download"


def test_is_ugly_name():
    assert is_ugly_name("videoplayback.mp4")
    assert is_ugly_name("index.html")
    assert is_ugly_name("download")
    assert is_ugly_name("123456.mp4")
    assert is_ugly_name("f.bin")
    assert not is_ugly_name("My Vacation 2026.mp4")
    assert not is_ugly_name("lecture-03-recursion.pdf")
    # CDN asset keys: digit soup, UUIDs, hex - a title beats all of these.
    assert is_ugly_name("1434659607842-pgv4ql-1642193429401.mp4")  # the IMDb trailer case
    assert is_ugly_name("vi3341271833.mp4")
    assert is_ugly_name("a3f9c2e8b1d04567.ts")
    assert is_ugly_name("f81d4fae-7dec-11d0-a765-00a0c91e6bf6.mp4")
    # Release-style names keep their sparse digits.
    assert not is_ugly_name("Top.Gun.Maverick.2022.1080p.WEBRip.mp4")
    assert not is_ugly_name("S01E02 - The Heist.mkv")


def test_improved_filename_rescues_ugly_names():
    fixed = improved_filename(
        "https://cdn.example/videoplayback.mp4", "Amazing Talk - Conference 2026"
    )
    assert fixed == "Amazing Talk - Conference 2026.mp4"


def test_improved_filename_keeps_good_names():
    kept = improved_filename("https://cdn.example/great-talk.mp4", "Some Page Title")
    assert kept == "great-talk.mp4"


def test_improved_filename_without_title_keeps_url_name():
    assert improved_filename("https://cdn.example/videoplayback.mp4", None) == ("videoplayback.mp4")


def test_improved_filename_guesses_extension_from_content_type():
    fixed = improved_filename("https://cdn.example/get", "A Nice Song", "audio/mpeg")
    assert fixed == "A Nice Song.mp3"


def test_unique_path_never_overwrites(tmp_path: Path):
    target = tmp_path / "file.bin"
    assert unique_path(target) == target
    target.write_bytes(b"x")
    assert unique_path(target) == tmp_path / "file (1).bin"
    (tmp_path / "file (1).bin").write_bytes(b"y")
    assert unique_path(target) == tmp_path / "file (2).bin"


# ------------------------------------------------------------- rename rules


def test_rename_rules_replace_in_order_and_keep_extension():
    rules = [("[SPONSORED] ", ""), ("Draft", "Final")]
    assert naming.apply_rename_rules("[SPONSORED] Draft Report.pdf", rules) == "Final Report.pdf"


def test_rename_rules_never_touch_the_extension():
    assert naming.apply_rename_rules("notes.txt", [("txt", "md")]) == "notes.txt"


def test_rename_rules_result_is_sanitized():
    # A rule can't inject path separators or empty the name out.
    assert "/" not in naming.apply_rename_rules("report.pdf", [("report", "a/b")])
    assert naming.apply_rename_rules("junk.bin", [("junk", "")]) == "download.bin"


def test_no_rules_is_a_no_op():
    assert naming.apply_rename_rules("as-is.zip", []) == "as-is.zip"


def test_clean_page_title_strips_badge_and_boilerplate():
    from app.core.naming import clean_page_title

    # The browser's unread badge rides in on every tab title.
    assert clean_page_title("(93) Cool Video - YouTube") == "Cool Video"
    assert clean_page_title("(2) YouTube") is None  # badge + site name = nothing
    assert clean_page_title("YouTube") is None
    assert clean_page_title("Cool Video - YouTube") == "Cool Video"
    # Known casualty, accepted: a rare title that itself starts with "(n) "
    # loses its prefix. Badges on every tab beat one film's typography.
    assert clean_page_title("(500) Days of Summer") == "Days of Summer"


def test_presentation_mime_restores_missing_suffix():
    mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert naming.filename_with_type("pptx", mime) == "download.pptx"
    assert naming.filename_with_type(".pptx", mime) == "download.pptx"
    assert naming.filename_with_type("Lecture 1", mime) == "Lecture 1.pptx"
    assert naming.filename_with_type("Lecture.pptx", mime) == "Lecture.pptx"
    assert naming.filename_with_type("custom.zip", mime) == "custom.zip"
    assert naming.filename_with_type("pptx", "application/octet-stream") == "pptx"
    assert naming.filename_with_type("Notes", "application/vnd.ms-powerpoint") == "Notes.ppt"
    assert naming.improved_filename("https://lms.example/pptx", None, mime) == "download.pptx"
    assert naming.filename_with_type("a" * 180, mime).endswith(".pptx")


@pytest.mark.parametrize(
    "mime,suffix",
    [
        ("application/pdf", ".pdf"),
        ("application/msword", ".doc"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
        ("application/vnd.ms-excel", ".xls"),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
        ("application/vnd.ms-excel.sheet.macroEnabled.12", ".xlsm"),
        ("application/vnd.oasis.opendocument.text", ".odt"),
        ("application/vnd.oasis.opendocument.spreadsheet", ".ods"),
        ("application/vnd.oasis.opendocument.presentation", ".odp"),
        ("application/epub+zip", ".epub"),
        ("application/zip", ".zip"),
        ("application/x-7z-compressed", ".7z"),
        ("application/vnd.rar", ".rar"),
        ("application/gzip", ".gz"),
        ("application/x-tar", ".tar"),
        ("application/x-iso9660-image", ".iso"),
        ("application/x-msdownload", ".exe"),
        ("application/x-msi", ".msi"),
        ("application/vnd.android.package-archive", ".apk"),
        ("application/vnd.debian.binary-package", ".deb"),
        ("application/x-rpm", ".rpm"),
        ("application/x-apple-diskimage", ".dmg"),
        ("image/jpeg", ".jpg"),
        ("image/png", ".png"),
        ("image/webp", ".webp"),
        ("image/avif", ".avif"),
        ("audio/mpeg", ".mp3"),
        ("audio/mp4", ".m4a"),
        ("audio/flac", ".flac"),
        ("video/mp4", ".mp4"),
        ("video/webm", ".webm"),
        ("video/x-matroska", ".mkv"),
        ("text/plain; charset=utf-8", ".txt"),
        ("text/csv", ".csv"),
        ("text/html", ".html"),
        ("application/json", ".json"),
    ],
)
def test_file_types_recover_missing_extensions(mime, suffix):
    assert naming.filename_with_type("file", mime) == "file" + suffix
    assert naming.filename_with_type("file" + suffix, mime) == "file" + suffix
    assert naming.filename_with_type("custom.other", mime) == "custom.other"


def test_unknown_types_are_not_blocked_or_guessed():
    for mime in (None, "application/octet-stream", "application/x-custom-file"):
        assert naming.filename_with_type("my-file", mime) == "my-file"
        assert naming.filename_with_type("my-file.custom", mime) == "my-file.custom"


def test_lms_query_filenames():
    assert (
        filename_from_url("https://lms.example/download.php?filename=Week%201.docx")
        == "Week 1.docx"
    )
    assert (
        filename_from_url("https://lms.example/file.php?file=%2Fcourse%2Fnotes.pdf") == "notes.pdf"
    )
    assert (
        filename_from_url("https://lms.example/original.zip?filename=other.exe") == "original.zip"
    )
    assert filename_from_url("https://lms.example/get?filename=../../notes.pdf") == "notes.pdf"
    assert filename_from_url("https://lms.example/get?filename=README") == "get"


def test_response_names_use_redirects_without_overwriting_custom_names():
    def resolve(name: str, server_filename: str | None = None) -> str:
        return naming.resolve_download_name(
            name,
            url="https://lms.example/download.php",
            final_url="https://cdn.example/Week%201.pdf",
            server_filename=server_filename,
            content_type="application/octet-stream",
        )

    assert resolve("download.php") == "Week 1.pdf"
    assert resolve("my-notes.custom") == "my-notes.custom"
    assert resolve("download.php", "official.pdf") == "official.pdf"


def test_script_endpoint_is_not_mistaken_for_download_type():
    url = "https://lms.example/download.php"
    assert (
        naming.resolve_download_name(
            "download.php",
            url=url,
            final_url=url,
            server_filename=None,
            content_type="application/pdf",
        )
        == "download.pdf"
    )
    # Explicit source-code names and actual HTML downloads still work.
    assert (
        naming.resolve_download_name(
            "source.php", url=url, final_url=url, server_filename=None, content_type="text/plain"
        )
        == "source.php"
    )


def test_extension_only_names_keep_specific_mime_variants():
    assert naming.filename_with_type("dll", "application/x-msdownload") == "download.dll"
    assert naming.filename_with_type("pps", "application/vnd.ms-powerpoint") == "download.pps"
    assert naming.filename_with_type("jpeg", "image/jpeg") == "download.jpeg"


def test_meaningful_original_filename_survives_opaque_redirect():
    assert (
        naming.resolve_download_name(
            "Lecture.pdf",
            url="https://lms.example/Lecture.pdf",
            final_url="https://cdn.example/opaque.bin",
            server_filename=None,
            content_type="application/octet-stream",
        )
        == "Lecture.pdf"
    )
    assert (
        naming.resolve_download_name(
            "Lecture.pdf",
            url="https://lms.example/get?filename=Lecture.pdf",
            final_url="https://cdn.example/opaque.bin",
            server_filename=None,
            content_type="application/octet-stream",
        )
        == "Lecture.pdf"
    )
