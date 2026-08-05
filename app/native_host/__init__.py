"""GrabLine's Native Messaging host (F1.1).

The browser launches this process and speaks length-prefixed JSON over stdio
- the same mechanism IDM uses. The host's only job is to relay: it validates
a message, writes a row into the shared SQLite database, and replies. The
running desktop app polls that table and takes it from there. No sockets, no
ports, nothing to scan (S3).
"""

HOST_NAME = "dev.grabline.host"

#: Stable ID of the unpacked Chrome extension (pinned by the "key" field in
#: extension/manifest.json). Store-published builds get their IDs appended.
CHROME_EXTENSION_IDS = ("ophhnobbimbhjgamalkmagmhohpffcip",)

#: Firefox add-on ID (browser_specific_settings.gecko.id in the manifest).
FIREFOX_EXTENSION_IDS = ("grabline@grabline.dev",)
