"""Real toolbar preferences, overlay geometry and browser-only downloads."""

import hashlib
import sqlite3
import time

from browser_api import count_jobs, database, manifest, overlays, set_pref, stored, ui
from command import command
from common import case, save

with case("BROWSER-PREF-RESTART", "real browser") as evidence:
    evidence["persisted_preferences"] = stored()
    assert stored().get("hoverButtons") is False and stored().get("intercept") is False

with case("BROWSER-POPUP-PASTE", "native GUI + real transfer") as evidence:
    with sqlite3.connect(database) as db:
        row = db.execute(
            "select id,status,dest_dir,filename from jobs where url like "
            "'%case=popup-paste' order by id desc limit 1"
        ).fetchone()
    assert row and row[1] == "completed", row
    from pathlib import Path

    file = Path(row[2]) / row[3]
    actual = hashlib.sha256(file.read_bytes()).hexdigest()
    assert actual == manifest["resources"]["/file.bin"]["sha256"]
    evidence.update(job_id=row[0], sha256=actual, bytes=file.stat().st_size)

with case("BROWSER-TAKEOVER-OFF", "real browser") as evidence:
    set_pref("Take over browser downloads", "intercept", False, True)
    before = count_jobs()
    command("evaluate", js="document.querySelector('#file').href='/file.bin?case=browser-only'")
    result = command("download", selector="#file", name="takeover-off.bin")
    assert result["sha256"] == manifest["resources"]["/file.bin"]["sha256"]
    assert count_jobs() == before
    evidence.update(result)

for identifier, selector, content in [
    ("BLOB", "#blob", b"test-only generated blob"),
    ("POST", "#post", b"test-only POST export\n"),
]:
    with case("BROWSER-EXCLUDE-" + identifier, "real browser") as evidence:
        set_pref("Take over browser downloads", "intercept", True, True)
        before = count_jobs()
        result = command("download", selector=selector, name=identifier.lower() + ".txt")
        assert result["sha256"] == hashlib.sha256(content).hexdigest(), result
        assert count_jobs() == before
        evidence.update(result)

with case("BROWSER-HOVER-MASTER", "native GUI + real browser") as evidence:
    set_pref("Show hover download buttons", "hoverButtons", False, True)
    assert not overlays("#video")
    set_pref("Show hover download buttons", "hoverButtons", True, True)
    assert overlays("#video")

with case("BROWSER-HOVER-IMAGES", "native GUI + real browser") as evidence:
    set_pref("Hover button on images too", "overlayImages", False, False)
    assert not overlays("#blue")
    set_pref("Hover button on images too", "overlayImages", True, False)
    evidence["visible_buttons"] = overlays("#blue")
    assert evidence["visible_buttons"]

with case("BROWSER-HOVER-SITE", "native GUI + real browser") as evidence:
    host = "127.0.0.1"
    if host not in stored().get("disabledSites", []):
        ui("Hover button on this site")
    assert host in stored().get("disabledSites", [])
    assert not overlays("#video")
    ui("Hover button on this site")
    assert host not in stored().get("disabledSites", [])
    assert overlays("#video")

for identifier, selector in [
    ("VIDEO", "#video"),
    ("AUDIO", "#audio"),
    ("FRAME", "iframe"),
    ("SHADOW", "#shadow"),
]:
    with case("BROWSER-OVERLAY-" + identifier, "real browser") as evidence:
        evidence["buttons"] = overlays(selector)
        assert evidence["buttons"]

with case("BROWSER-OVERLAY-DYNAMIC", "real browser") as evidence:
    command("click", selector="#insert")
    evidence["buttons"] = overlays("#dynamic")
    assert evidence["buttons"]

with case("BROWSER-OVERLAY-RESIZE", "real browser") as evidence:
    positions = []
    for width, height in [(1200, 900), (800, 600)]:
        command("viewport", size={"width": width, "height": height})
        buttons = overlays("#video")
        assert buttons
        b = buttons[0]
        assert 0 <= b["x"] <= width - b["width"] and 0 <= b["y"] <= height - b["height"]
        positions.append(b)
    evidence["positions"] = positions

with case("BROWSER-PREF-CORNERS", "native GUI + real browser") as evidence:
    corners = []
    for index, corner in enumerate(["top-right", "top-left", "bottom-right", "bottom-left"]):
        ui("Button position", "ComboBox", "{HOME}" + "{DOWN}" * index + "{ENTER}")
        assert stored().get("buttonCorner", "top-right") == corner, stored()
        buttons = overlays("#video")
        assert buttons
        corners.append(buttons[0])
    assert corners[0]["x"] > corners[1]["x"] and corners[2]["x"] > corners[3]["x"]
    assert corners[0]["y"] < corners[2]["y"] and corners[1]["y"] < corners[3]["y"]
    evidence["positions"] = corners

for identifier, text in [("EMPTY", ""), ("INVALID", "not-a-download-url")]:
    with case("BROWSER-POPUP-" + identifier, "native GUI + real browser"):
        before = count_jobs()
        ui("Paste a link to download", "Edit", text)
        time.sleep(1)
        assert count_jobs() == before

save("browser-final-preferences.json", stored())
