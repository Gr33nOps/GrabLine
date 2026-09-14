from __future__ import annotations

from pathlib import Path

from app.core.categories import category_for, dest_dir_for


def test_category_mapping():
    assert category_for("movie.mkv") == "Video"
    assert category_for("song.MP3") == "Music"
    assert category_for("photo.jpeg") == "Images"
    assert category_for("paper.pdf") == "Documents"
    assert category_for("bundle.tar") == "Archives"
    assert category_for("mystery.xyz") is None
    assert category_for("no_extension") is None


def test_programs_games_torrents_categories():
    assert category_for("setup.exe") == "Programs"
    assert category_for("app.deb") == "Programs"
    assert category_for("tool.AppImage") == "Programs"
    assert category_for("installer.dmg") == "Programs"  # moved out of Archives
    assert category_for("zelda.gba") == "Games"
    assert category_for("mario.nsp") == "Games"
    assert category_for("ubuntu.torrent") == "Torrents"
    # .iso stays an archive - it's ambiguous (OS image, backup, game dump).
    assert category_for("backup.iso") == "Archives"


def test_dest_dir_for_enabled():
    base = Path("/downloads")
    assert dest_dir_for(base, "movie.mp4", enabled=True) == base / "Video"
    assert dest_dir_for(base, "song.mp3", enabled=True) == base / "Music"
    assert dest_dir_for(base, "mystery.xyz", enabled=True) == base


def test_dest_dir_for_disabled():
    base = Path("/downloads")
    assert dest_dir_for(base, "movie.mp4", enabled=False) == base
