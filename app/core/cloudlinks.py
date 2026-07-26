"""Turn a cloud *share* link into a direct-download URL the normal segmented
engine can fetch - so a Google Drive / Dropbox / OneDrive / Nextcloud / Box
link you were sent downloads at full speed with resume, no account needed.

These are public-share transforms only. Private, whole-account access
(OAuth) is deliberately not here: an open-source app can't ship the client
secrets each provider's OAuth requires. Credentialed access lives in the
protocol engine instead (SFTP/FTP/WebDAV/S3 - see app.engines.cloud).
"""

from __future__ import annotations

import base64
import re
from urllib.parse import SplitResult, parse_qs, urlsplit, urlunsplit

_DRIVE_ID = re.compile(r"/file/d/([-\w]+)")
_DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}


def _google_drive(url: str, parts: SplitResult) -> str | None:
    if parts.netloc not in _DRIVE_HOSTS:
        return None
    match = _DRIVE_ID.search(parts.path)
    file_id = match.group(1) if match else parse_qs(parts.query).get("id", [None])[0]
    if not file_id:
        return None
    # confirm=t skips the "can't scan for viruses" interstitial on big files.
    return f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"


def _dropbox(url: str, parts: SplitResult) -> str | None:
    host = parts.netloc.lower()
    if host.endswith("dropboxusercontent.com"):
        return url  # already a direct-content host
    if not host.endswith("dropbox.com"):
        return None
    query = parse_qs(parts.query)
    query["dl"] = ["1"]  # dl=1 forces the file instead of the preview page
    new_query = "&".join(f"{k}={v[-1]}" for k, v in query.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))


def _onedrive(url: str, parts: SplitResult) -> str | None:
    host = parts.netloc.lower()
    known = (
        host.endswith("1drv.ms") or "onedrive.live.com" in host or host.endswith("sharepoint.com")
    )
    if not known:
        return None
    # The documented no-auth transform: base64url the whole share URL into the
    # public shares content endpoint (works for 1drv.ms and onedrive.live.com).
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://api.onedrive.com/v1.0/shares/u!{token}/root/content"


#: Hosts whose /s/ links are handled by their own transform, not Nextcloud's.
_OTHER_SHARE_HOSTS = ("box.com", "dropbox.com", "google.com", "onedrive.live.com")


def _nextcloud(url: str, parts: SplitResult) -> str | None:
    # Nextcloud / ownCloud is self-hosted (any host), so this /s/ transform is
    # the catch-all - skip the other providers whose /s/ we handle elsewhere.
    if any(known in parts.netloc.lower() for known in _OTHER_SHARE_HOSTS):
        return None
    match = re.search(r"(/s/[-\w]+)/?$", parts.path)
    if not match:
        return None
    new_path = match.group(1) + "/download"
    path = parts.path[: match.start()] + new_path
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _box(url: str, parts: SplitResult) -> str | None:
    if "box.com" not in parts.netloc.lower():
        return None
    if "/shared/static/" in parts.path:
        return url  # already the direct static form
    # A /s/ share page needs the file id we don't have without the API.
    return None


_TRANSFORMS = (_google_drive, _dropbox, _onedrive, _nextcloud, _box)


def direct_download_url(url: str) -> str | None:
    """A direct-download URL for a recognized cloud *share* link, or None.

    The result is a plain https URL, so the caller routes it straight to the
    segmented downloader - full speed, resume, the lot.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    for transform in _TRANSFORMS:
        result = transform(url, parts)
        if result is not None:
            return result
    return None


def is_cloud_share(url: str) -> bool:
    return direct_download_url(url) is not None


#: Services whose downloads need end-to-end decryption or a proprietary,
#: client-secret OAuth flow an open-source app can't ship - we say so plainly
#: instead of failing with a confusing HTML/parse error.
_UNSUPPORTED = {
    "mega.nz": "Mega files are end-to-end encrypted; decrypting them needs the MEGAcmd tool.",
    "mega.co.nz": "Mega files are end-to-end encrypted; decrypting them needs the MEGAcmd tool.",
    "drive.proton.me": "Proton Drive is end-to-end encrypted and has no public download API.",
    "icloud.com": "iCloud has no public download API; use SFTP/WebDAV or a share link instead.",
}


def unsupported_cloud_reason(url: str) -> str | None:
    """An honest, specific message for a cloud host GrabLine deliberately does
    not download from, or None. Keeps us from silently mangling the URL."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    for known, reason in _UNSUPPORTED.items():
        if host == known or host.endswith("." + known) or host.endswith(known):
            return reason
    return None
