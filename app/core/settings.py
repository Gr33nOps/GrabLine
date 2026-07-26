"""Typed application settings backed by the settings table in SQLite."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.core import paths
from app.db.database import Database

#: Browsers yt-dlp can read a cookie store from (F0.8).
SESSION_BROWSERS = ("chrome", "firefox", "edge", "brave", "chromium", "opera", "safari")

#: Parallel HTTP connections per download (settings + per-job pin clamps).
DEFAULT_CONNECTIONS = 8
MAX_CONNECTIONS = 32

_AFTER_QUEUE_ACTIONS = ("nothing", "quit", "sleep", "shutdown", "hibernate", "lock")

#: Records of things already done to this machine, not preferences - see reset().
_KEEP_ON_RESET = ("setup_seen", "host_registered")

#: Settings keys dropped entirely from an export - plaintext API keys.
_EXPORT_DROP = ("virustotal_key", "safebrowsing_key")


def sanitized_export(raw: Mapping[str, str]) -> dict[str, str]:
    """A settings dict safe to write to a shareable file: API keys removed and
    any credentials embedded in the proxy URL redacted (CWE-312 / CWE-522).
    Everything else is carried through unchanged."""
    from app.core import net

    out = {k: v for k, v in raw.items() if k not in _EXPORT_DROP}
    if out.get("proxy"):
        out["proxy"] = net.redact_credentials(out["proxy"])
    return out


class Settings:
    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        """The backing database, for components that need their own store
        (e.g. the cloud CredentialStore) without re-opening the file."""
        return self._db

    def reset(self) -> None:
        """Drop every stored preference so the coded defaults apply again.

        Downloads, history and statistics live in their own tables and are
        untouched. Two keys survive because they record what has already
        happened to this machine, not what the user prefers: the wizard would
        otherwise reappear, and the browser host would be re-registered behind
        a user who deliberately unpaired it."""
        keep = {key: self._db.get_setting(key) for key in _KEEP_ON_RESET}
        self._db.reset_settings()
        for key, value in keep.items():
            if value is not None:
                self._db.set_setting(key, value)

    # ------------------------------------------------------------ helpers

    def _get_bool(self, key: str, default: bool) -> bool:
        raw = self._db.get_setting(key)
        return default if raw is None else raw == "1"

    def _set_bool(self, key: str, value: bool) -> None:
        self._db.set_setting(key, "1" if value else "0")

    def _get_int(self, key: str, default: int) -> int:
        raw = self._db.get_setting(key)
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    # ----------------------------------------------------------- settings

    @property
    def download_dir(self) -> Path:
        raw = self._db.get_setting("download_dir")
        return Path(raw) if raw else paths.default_download_dir()

    @download_dir.setter
    def download_dir(self, value: Path | str) -> None:
        self._db.set_setting("download_dir", str(value))

    @property
    def categories_enabled(self) -> bool:
        """F0.6: sort downloads into Video/Music/Images/Documents/Archives."""
        return self._get_bool("categories_enabled", True)

    @categories_enabled.setter
    def categories_enabled(self, value: bool) -> None:
        self._set_bool("categories_enabled", value)

    @property
    def host_registered(self) -> bool:
        """Whether the Native Messaging host has been auto-registered once (on
        first run of an installed build). Re-pairing is available in Settings."""
        return self._get_bool("host_registered", False)

    @host_registered.setter
    def host_registered(self, value: bool) -> None:
        self._set_bool("host_registered", value)

    @property
    def clipboard_watcher(self) -> bool:
        """F0.5: offer to download URLs copied to the clipboard. Off by
        default - an offer on every copied link is intrusive, and the browser
        button covers the common case."""
        return self._get_bool("clipboard_watcher", False)

    @clipboard_watcher.setter
    def clipboard_watcher(self, value: bool) -> None:
        self._set_bool("clipboard_watcher", value)

    @property
    def use_browser_session(self) -> bool:
        """Deprecated - always off. This used to force browser cookies and the
        JS runtime onto every YouTube download up front, which made them slow
        and sometimes stalled them before they started. Cookies are now used
        automatically only for a video that needs a login (age/members), so
        there is nothing to force on. Returns False regardless of any old
        stored value so existing users get the fast path too."""
        return False

    @property
    def session_browser(self) -> str:
        raw = self._db.get_setting("session_browser")
        if raw in SESSION_BROWSERS:
            return raw
        # Never explicitly chosen: point at the browser that's actually set up
        # here (Firefox before preinstalled-but-unused Edge) rather than a
        # hardcoded "chrome" the person may not even have.
        from app.core.browser_setup import detect_cookie_browser

        return detect_cookie_browser() or "chrome"

    @session_browser.setter
    def session_browser(self, value: str) -> None:
        if value not in SESSION_BROWSERS:
            raise ValueError(f"unsupported browser: {value}")
        self._db.set_setting("session_browser", value)

    @property
    def max_concurrent(self) -> int:
        return max(1, min(10, self._get_int("max_concurrent", 3)))

    @max_concurrent.setter
    def max_concurrent(self, value: int) -> None:
        self._db.set_setting("max_concurrent", str(value))

    @property
    def connections(self) -> int:
        return max(1, min(MAX_CONNECTIONS, self._get_int("connections", DEFAULT_CONNECTIONS)))

    @connections.setter
    def connections(self, value: int) -> None:
        self._db.set_setting("connections", str(max(1, min(MAX_CONNECTIONS, int(value)))))

    @property
    def speed_limit_kbps(self) -> int:
        """Global download cap in KB/s (F1.8). 0 means unlimited."""
        return max(0, self._get_int("speed_limit_kbps", 0))

    @speed_limit_kbps.setter
    def speed_limit_kbps(self, value: int) -> None:
        self._db.set_setting("speed_limit_kbps", str(max(0, value)))

    @property
    def fair_speed(self) -> bool:
        """Split bandwidth evenly across simultaneous downloads (budget ÷ N).
        On by default; turn off to let the fastest source take the whole line."""
        return self._get_bool("fair_speed", True)

    @fair_speed.setter
    def fair_speed(self, value: bool) -> None:
        self._set_bool("fair_speed", value)

    @property
    def host_limits(self) -> dict[str, int]:
        """Per-host download caps in KB/s, keyed by hostname. Downloads from a
        listed host share that cap; 0 or absent means no per-host limit."""
        raw = self._db.get_setting("host_limits")
        if not raw:
            return {}
        try:
            values = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(values, dict):
            return {}
        limits: dict[str, int] = {}
        for host, kbps in values.items():
            try:
                rate = int(kbps)
            except (TypeError, ValueError):
                continue
            if str(host).strip() and rate > 0:
                limits[str(host).strip().lower()] = rate
        return limits

    @host_limits.setter
    def host_limits(self, value: Mapping[str, int]) -> None:
        cleaned = {
            str(host).strip().lower(): int(kbps)
            for host, kbps in value.items()
            if str(host).strip() and int(kbps) > 0
        }
        self._db.set_setting("host_limits", json.dumps(cleaned))

    @property
    def shortcuts(self) -> dict[str, str]:
        """Per-user keyboard-shortcut overrides: ``{action_id: key_sequence}``.
        An entry maps an action to a custom key; an empty-string value unbinds
        it. Actions with no entry keep their coded default (see
        ``app.ui.shortcuts``). Absent from ``reset()``'s keep-list, so a settings
        reset restores every default key."""
        raw = self._db.get_setting("shortcut_overrides")
        if not raw:
            return {}
        try:
            values = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(values, dict):
            return {}
        return {str(key): str(seq) for key, seq in values.items()}

    @shortcuts.setter
    def shortcuts(self, value: Mapping[str, str]) -> None:
        cleaned = {str(key): str(seq) for key, seq in value.items()}
        self._db.set_setting("shortcut_overrides", json.dumps(cleaned))

    @property
    def auto_throttle(self) -> bool:
        """'Polite mode': automatically slow downloads when other apps are
        using the network heavily, and speed back up when they stop."""
        return self._get_bool("auto_throttle", False)

    @auto_throttle.setter
    def auto_throttle(self, value: bool) -> None:
        self._set_bool("auto_throttle", value)

    @property
    def auto_throttle_kbps(self) -> int:
        """The reduced download cap (KB/s) applied while other traffic is busy."""
        return max(1, self._get_int("auto_throttle_kbps", 512))

    @auto_throttle_kbps.setter
    def auto_throttle_kbps(self, value: int) -> None:
        self._db.set_setting("auto_throttle_kbps", str(max(1, value)))

    @property
    def auto_throttle_threshold_kbps(self) -> int:
        """How much *other* network traffic (KB/s) counts as 'busy' and trips
        the automatic throttle."""
        return max(1, self._get_int("auto_throttle_threshold_kbps", 256))

    @auto_throttle_threshold_kbps.setter
    def auto_throttle_threshold_kbps(self, value: int) -> None:
        self._db.set_setting("auto_throttle_threshold_kbps", str(max(1, value)))

    # --- speed schedule: lift the limit during a nightly full-speed window ---

    @property
    def speed_schedule_enabled(self) -> bool:
        """When on, the speed limit is lifted during the full-speed window."""
        return self._get_bool("speed_schedule_enabled", False)

    @speed_schedule_enabled.setter
    def speed_schedule_enabled(self, value: bool) -> None:
        self._set_bool("speed_schedule_enabled", value)

    def _get_time(self, key: str, default: str) -> str:
        raw = self._db.get_setting(key)
        if raw and len(raw) == 5 and raw[2] == ":" and raw[:2].isdigit() and raw[3:].isdigit():
            return raw
        return default

    @property
    def speed_full_from(self) -> str:
        """Start of the nightly full-speed window, "HH:MM"."""
        return self._get_time("speed_full_from", "00:00")

    @speed_full_from.setter
    def speed_full_from(self, value: str) -> None:
        self._db.set_setting("speed_full_from", value)

    @property
    def speed_full_to(self) -> str:
        """End of the nightly full-speed window, "HH:MM"."""
        return self._get_time("speed_full_to", "07:00")

    @speed_full_to.setter
    def speed_full_to(self, value: str) -> None:
        self._db.set_setting("speed_full_to", value)

    # --- timed download window: only run downloads between two times ---

    @property
    def download_schedule_enabled(self) -> bool:
        """When on, downloads only start (and keep running) inside the window."""
        return self._get_bool("download_schedule_enabled", False)

    @download_schedule_enabled.setter
    def download_schedule_enabled(self, value: bool) -> None:
        self._set_bool("download_schedule_enabled", value)

    @property
    def download_start(self) -> str:
        return self._get_time("download_start", "02:00")

    @download_start.setter
    def download_start(self, value: str) -> None:
        self._db.set_setting("download_start", value)

    @property
    def download_stop(self) -> str:
        return self._get_time("download_stop", "08:00")

    @download_stop.setter
    def download_stop(self, value: str) -> None:
        self._db.set_setting("download_stop", value)

    # --- updates ---

    @property
    def check_updates(self) -> bool:
        """Look for a newer GrabLine release on startup (best effort)."""
        return self._get_bool("check_updates", True)

    @check_updates.setter
    def check_updates(self, value: bool) -> None:
        self._set_bool("check_updates", value)

    @property
    def setup_seen(self) -> bool:
        """Whether the first-run Browser Setup wizard has been shown."""
        return self._get_bool("setup_seen", False)

    @setup_seen.setter
    def setup_seen(self, value: bool) -> None:
        self._set_bool("setup_seen", value)

    # --- automatic retry of failed downloads ---

    @property
    def auto_retry(self) -> bool:
        """Retry a download that fails from a network hiccup, with backoff."""
        return self._get_bool("auto_retry", True)

    @auto_retry.setter
    def auto_retry(self, value: bool) -> None:
        self._set_bool("auto_retry", value)

    @property
    def auto_retry_max(self) -> int:
        """Auto-retry attempts per download; 0 means retry forever."""
        return max(0, min(99, self._get_int("auto_retry_max", 5)))

    @auto_retry_max.setter
    def auto_retry_max(self, value: int) -> None:
        self._db.set_setting("auto_retry_max", str(max(0, value)))

    # --- appearance ---

    @property
    def language(self) -> str:
        """The chosen UI language code (see app.core.i18n), or "" when the user
        hasn't picked one yet - in which case startup follows the OS locale."""
        return self._db.get_setting("language") or ""

    @language.setter
    def language(self, value: str) -> None:
        self._db.set_setting("language", value)

    @property
    def theme(self) -> str:
        """UI theme: "system", "light", or "dark"."""
        raw = self._db.get_setting("theme")
        return raw if raw in ("system", "light", "dark") else "system"

    @theme.setter
    def theme(self, value: str) -> None:
        if value not in ("system", "light", "dark"):
            raise ValueError(f"unknown theme: {value}")
        self._db.set_setting("theme", value)

    # --- networking ---

    @property
    def proxy(self) -> str | None:
        """A proxy URL (http://, https://, socks5://…) applied to all
        downloading, or None to go direct."""
        return self._db.get_setting("proxy") or None

    @proxy.setter
    def proxy(self, value: str | None) -> None:
        self._db.set_setting("proxy", (value or "").strip())

    # --- finishing touches ---

    @property
    def notify_on_complete(self) -> bool:
        return self._get_bool("notify_on_complete", True)

    @notify_on_complete.setter
    def notify_on_complete(self, value: bool) -> None:
        self._set_bool("notify_on_complete", value)

    @property
    def auto_open_folder(self) -> bool:
        """Open the containing folder when a download finishes."""
        return self._get_bool("auto_open_folder", False)

    @auto_open_folder.setter
    def auto_open_folder(self, value: bool) -> None:
        self._set_bool("auto_open_folder", value)

    @property
    def auto_extract(self) -> bool:
        """Unpack .zip/.tar archives automatically once they finish."""
        return self._get_bool("auto_extract", False)

    @auto_extract.setter
    def auto_extract(self, value: bool) -> None:
        self._set_bool("auto_extract", value)

    @property
    def archive_passwords(self) -> tuple[str, ...]:
        """Passwords tried in order when an archive turns out to be encrypted.
        Stored locally and unencrypted - the same trust level as the
        downloaded files themselves."""
        return self._get_str_list("archive_passwords")

    @archive_passwords.setter
    def archive_passwords(self, value: Sequence[str]) -> None:
        deduped = list(dict.fromkeys(v.strip() for v in value if v.strip()))
        self._db.set_setting("archive_passwords", json.dumps(deduped))

    def _get_str_list(self, key: str) -> tuple[str, ...]:
        raw = self._db.get_setting(key)
        if not raw:
            return ()
        try:
            values = json.loads(raw)
        except ValueError:
            return ()
        if not isinstance(values, list):
            return ()
        return tuple(str(v) for v in values if str(v).strip())

    @property
    def favorite_folders(self) -> tuple[str, ...]:
        """Quick move-to destinations offered in the download's context menu."""
        return self._get_str_list("favorite_folders")

    @favorite_folders.setter
    def favorite_folders(self, value: Sequence[str]) -> None:
        deduped = list(dict.fromkeys(v.strip() for v in value if v.strip()))
        self._db.set_setting("favorite_folders", json.dumps(deduped))

    @property
    def rename_rules(self) -> tuple[tuple[str, str], ...]:
        """Literal find -> replace pairs applied (in order) to every new
        download's filename stem."""
        raw = self._db.get_setting("rename_rules")
        if not raw:
            return ()
        try:
            values = json.loads(raw)
        except ValueError:
            return ()
        if not isinstance(values, list):
            return ()
        rules: list[tuple[str, str]] = []
        for pair in values:
            if isinstance(pair, list) and len(pair) == 2 and str(pair[0]):
                rules.append((str(pair[0]), str(pair[1])))
        return tuple(rules)

    @rename_rules.setter
    def rename_rules(self, value: Sequence[tuple[str, str]]) -> None:
        cleaned = [[find, replace] for find, replace in value if find]
        self._db.set_setting("rename_rules", json.dumps(cleaned))

    @property
    def scan_before_extract(self) -> bool:
        """Run an installed virus scanner (ClamAV / Windows Defender) over an
        archive before extracting it."""
        return self._get_bool("scan_before_extract", False)

    @scan_before_extract.setter
    def scan_before_extract(self, value: bool) -> None:
        self._set_bool("scan_before_extract", value)

    # ------------------------------------------------------------ security

    @property
    def scan_downloads(self) -> bool:
        """Run an advisory security check on each finished download (a local
        virus scan and, if configured, VirusTotal). Never blocks - it only
        warns."""
        return self._get_bool("scan_downloads", False)

    @scan_downloads.setter
    def scan_downloads(self, value: bool) -> None:
        self._set_bool("scan_downloads", value)

    @property
    def enforce_https(self) -> bool:
        """Warn before starting a download over unencrypted HTTP (you can
        still proceed)."""
        return self._get_bool("enforce_https", False)

    @enforce_https.setter
    def enforce_https(self, value: bool) -> None:
        self._set_bool("enforce_https", value)

    @property
    def virustotal_key(self) -> str:
        """The user's own VirusTotal API key. Empty = the VirusTotal check is
        off. Only the file's hash is ever sent, never its contents."""
        return self._db.get_setting("virustotal_key") or ""

    @virustotal_key.setter
    def virustotal_key(self, value: str) -> None:
        self._db.set_setting("virustotal_key", value.strip())

    @property
    def safebrowsing_key(self) -> str:
        """The user's own Google Safe Browsing API key. Empty = off. When set,
        the URL is sent to Google before download - so this is opt-in."""
        return self._db.get_setting("safebrowsing_key") or ""

    @safebrowsing_key.setter
    def safebrowsing_key(self, value: str) -> None:
        self._db.set_setting("safebrowsing_key", value.strip())

    # ------------------------------------------------------------ torrents

    @property
    def torrent_port(self) -> int:
        """The BitTorrent listen port (both TCP and uTP)."""
        return max(1024, min(65535, self._get_int("torrent_port", 6881)))

    @torrent_port.setter
    def torrent_port(self, value: int) -> None:
        self._db.set_setting("torrent_port", str(value))

    @property
    def torrent_dht(self) -> bool:
        """DHT: find peers without trackers (also enables magnet-only swarms)."""
        return self._get_bool("torrent_dht", True)

    @torrent_dht.setter
    def torrent_dht(self, value: bool) -> None:
        self._set_bool("torrent_dht", value)

    @property
    def torrent_upnp(self) -> bool:
        return self._get_bool("torrent_upnp", True)

    @torrent_upnp.setter
    def torrent_upnp(self, value: bool) -> None:
        self._set_bool("torrent_upnp", value)

    @property
    def torrent_natpmp(self) -> bool:
        return self._get_bool("torrent_natpmp", True)

    @torrent_natpmp.setter
    def torrent_natpmp(self, value: bool) -> None:
        self._set_bool("torrent_natpmp", value)

    @property
    def torrent_seed(self) -> bool:
        """Keep seeding after a torrent finishes downloading."""
        return self._get_bool("torrent_seed", True)

    @torrent_seed.setter
    def torrent_seed(self, value: bool) -> None:
        self._set_bool("torrent_seed", value)

    @property
    def torrent_ratio_limit(self) -> float:
        """Stop seeding at this upload/download ratio (0 = seed forever)."""
        raw = self._db.get_setting("torrent_ratio_limit")
        try:
            return max(0.0, float(raw)) if raw is not None else 2.0
        except ValueError:
            return 2.0

    @torrent_ratio_limit.setter
    def torrent_ratio_limit(self, value: float) -> None:
        self._db.set_setting("torrent_ratio_limit", str(value))

    @property
    def torrent_upload_kbps(self) -> int:
        """Upload speed cap for the whole torrent session (0 = unlimited)."""
        return max(0, self._get_int("torrent_upload_kbps", 0))

    @torrent_upload_kbps.setter
    def torrent_upload_kbps(self, value: int) -> None:
        self._db.set_setting("torrent_upload_kbps", str(value))

    @property
    def torrent_sequential(self) -> bool:
        """Default new torrents to in-order pieces (streaming-friendly)."""
        return self._get_bool("torrent_sequential", False)

    @torrent_sequential.setter
    def torrent_sequential(self, value: bool) -> None:
        self._set_bool("torrent_sequential", value)

    @property
    def torrent_dir(self) -> Path | None:
        """Where torrent content saves by default (None = the download dir)."""
        raw = self._db.get_setting("torrent_dir")
        return Path(raw) if raw else None

    @torrent_dir.setter
    def torrent_dir(self, value: Path | str | None) -> None:
        self._db.set_setting("torrent_dir", str(value) if value else "")

    @property
    def torrent_search_url(self) -> str:
        """Search template opened in the browser; %s is the query. Empty =
        the search action asks you to configure one first."""
        return self._db.get_setting("torrent_search_url") or ""

    @torrent_search_url.setter
    def torrent_search_url(self, value: str) -> None:
        self._db.set_setting("torrent_search_url", value.strip())

    @property
    def rss_feeds(self) -> tuple[str, ...]:
        """RSS/Atom feed lines: 'url' or 'url | must-contain filter'."""
        return self._get_str_list("rss_feeds")

    @rss_feeds.setter
    def rss_feeds(self, value: Sequence[str]) -> None:
        deduped = list(dict.fromkeys(v.strip() for v in value if v.strip()))
        self._db.set_setting("rss_feeds", json.dumps(deduped))

    @property
    def rss_interval_minutes(self) -> int:
        return max(5, min(24 * 60, self._get_int("rss_interval_minutes", 30)))

    @rss_interval_minutes.setter
    def rss_interval_minutes(self, value: int) -> None:
        self._db.set_setting("rss_interval_minutes", str(value))

    @property
    def rss_seen(self) -> tuple[str, ...]:
        """GUIDs/links already added from feeds (capped to the newest 500)."""
        return self._get_str_list("rss_seen")

    @rss_seen.setter
    def rss_seen(self, value: Sequence[str]) -> None:
        self._db.set_setting("rss_seen", json.dumps(list(value)[-500:]))

    @property
    def after_queue_action(self) -> str:
        """What to do once every download finishes: nothing / quit / sleep /
        shutdown / hibernate / lock."""
        raw = self._db.get_setting("after_queue_action")
        return raw if raw in _AFTER_QUEUE_ACTIONS else "nothing"

    @after_queue_action.setter
    def after_queue_action(self, value: str) -> None:
        if value not in _AFTER_QUEUE_ACTIONS:
            raise ValueError(f"unknown after-queue action: {value}")
        self._db.set_setting("after_queue_action", value)

    @property
    def download_days(self) -> tuple[int, ...]:
        """Weekdays downloads may run (0=Mon .. 6=Sun). All days when unset;
        an empty selection also means all days - you can't accidentally
        configure 'never download'."""
        raw = self._db.get_setting("download_days")
        if not raw:
            return (0, 1, 2, 3, 4, 5, 6)
        try:
            values = json.loads(raw)
        except ValueError:
            return (0, 1, 2, 3, 4, 5, 6)
        days = tuple(sorted({int(v) for v in values if 0 <= int(v) <= 6}))
        return days or (0, 1, 2, 3, 4, 5, 6)

    @download_days.setter
    def download_days(self, value: Sequence[int]) -> None:
        self._db.set_setting("download_days", json.dumps(sorted({int(v) for v in value})))

    @property
    def pause_on_battery(self) -> bool:
        """Battery mode: hold downloads while on battery, resume on AC."""
        return self._get_bool("pause_on_battery", False)

    @pause_on_battery.setter
    def pause_on_battery(self, value: bool) -> None:
        self._set_bool("pause_on_battery", value)

    @property
    def wait_for_network(self) -> bool:
        """Hold new downloads while the internet is unreachable and retry
        failed ones the moment it returns (instead of waiting out backoff)."""
        return self._get_bool("wait_for_network", False)

    @wait_for_network.setter
    def wait_for_network(self, value: bool) -> None:
        self._set_bool("wait_for_network", value)

    @property
    def sound_on_complete(self) -> bool:
        return self._get_bool("sound_on_complete", False)

    @sound_on_complete.setter
    def sound_on_complete(self, value: bool) -> None:
        self._set_bool("sound_on_complete", value)

    @property
    def sound_file(self) -> str:
        """A custom completion sound (empty = the platform default sound)."""
        return self._db.get_setting("sound_file") or ""

    @sound_file.setter
    def sound_file(self, value: str) -> None:
        self._db.set_setting("sound_file", value.strip())

    @property
    def script_on_complete(self) -> str:
        """A command run after each finished download, with the file path
        appended as the last argument (empty = off)."""
        return self._db.get_setting("script_on_complete") or ""

    @script_on_complete.setter
    def script_on_complete(self, value: str) -> None:
        self._db.set_setting("script_on_complete", value.strip())

    @property
    def playlist_batch_cap(self) -> int:
        """How many playlist entries get preselected (F1.7)."""
        return max(1, min(500, self._get_int("playlist_batch_cap", 30)))

    @playlist_batch_cap.setter
    def playlist_batch_cap(self, value: int) -> None:
        self._db.set_setting("playlist_batch_cap", str(max(1, min(500, value))))

    @property
    def video_hq_first(self) -> bool:
        """Quality-first video downloads: solve YouTube's JS challenge from
        the first attempt for the complete format ladder (up to 4K/8K), at the
        cost of a much slower start. Off = start fast with the jsless clients
        (which can top out at 1080p)."""
        return self._get_bool("video_hq_first", False)

    @video_hq_first.setter
    def video_hq_first(self, value: bool) -> None:
        self._set_bool("video_hq_first", value)

    @property
    def ffmpeg_path(self) -> str | None:
        """Manual override; normally FFmpeg is found automatically."""
        return self._db.get_setting("ffmpeg_path") or None

    @ffmpeg_path.setter
    def ffmpeg_path(self, value: str | None) -> None:
        self._db.set_setting("ffmpeg_path", value or "")

    # ------------------------------------------------- general / window

    @property
    def start_minimized(self) -> bool:
        """Start in the tray even when launched by hand (autostart always does)."""
        return self._get_bool("start_minimized", False)

    @start_minimized.setter
    def start_minimized(self, value: bool) -> None:
        self._set_bool("start_minimized", value)

    @property
    def minimize_to_tray(self) -> bool:
        """Minimize hides to the tray instead of the taskbar."""
        return self._get_bool("minimize_to_tray", False)

    @minimize_to_tray.setter
    def minimize_to_tray(self, value: bool) -> None:
        self._set_bool("minimize_to_tray", value)

    @property
    def close_to_tray(self) -> bool:
        """Closing the window keeps GrabLine running in the tray."""
        return self._get_bool("close_to_tray", True)

    @close_to_tray.setter
    def close_to_tray(self, value: bool) -> None:
        self._set_bool("close_to_tray", value)

    @property
    def tray_hint_shown(self) -> bool:
        """Whether the "still running in the tray" notice has been shown. The
        first close hides the window, which otherwise looks like a quit."""
        return self._get_bool("tray_hint_shown", False)

    @tray_hint_shown.setter
    def tray_hint_shown(self, value: bool) -> None:
        self._set_bool("tray_hint_shown", value)

    @property
    def confirm_exit_active(self) -> bool:
        """Ask before quitting while downloads are still running."""
        return self._get_bool("confirm_exit_active", True)

    @confirm_exit_active.setter
    def confirm_exit_active(self, value: bool) -> None:
        self._set_bool("confirm_exit_active", value)

    @property
    def auto_start_downloads(self) -> bool:
        """Off = new downloads are added paused, started by hand."""
        return self._get_bool("auto_start_downloads", True)

    @auto_start_downloads.setter
    def auto_start_downloads(self, value: bool) -> None:
        self._set_bool("auto_start_downloads", value)

    @property
    def confirm_downloads(self) -> bool:
        """On = a browser download opens the Download Info dialog first (name,
        category, save location, quality). Off = it starts immediately."""
        return self._get_bool("confirm_downloads", True)

    @confirm_downloads.setter
    def confirm_downloads(self, value: bool) -> None:
        self._set_bool("confirm_downloads", value)

    # -------------------------------------------------------- downloads

    @property
    def ask_save_dir(self) -> bool:
        """Ask where to save on every add (instead of the default folder)."""
        return self._get_bool("ask_save_dir", False)

    @ask_save_dir.setter
    def ask_save_dir(self, value: bool) -> None:
        self._set_bool("ask_save_dir", value)

    @property
    def min_free_mb(self) -> int:
        """Warn when the destination has less than this free (MB); 0 = off."""
        return max(0, self._get_int("min_free_mb", 500))

    @min_free_mb.setter
    def min_free_mb(self, value: int) -> None:
        self._db.set_setting("min_free_mb", str(max(0, value)))

    # ------------------------------------------------------------ video

    @property
    def video_default_quality(self) -> str:
        """The quality preselected in the panel ("Best", "1080p", "MP3", …)."""
        return self._db.get_setting("video_default_quality") or "Best"

    @video_default_quality.setter
    def video_default_quality(self, value: str) -> None:
        self._db.set_setting("video_default_quality", value.strip() or "Best")

    @property
    def audio_bitrate(self) -> str:
        """Target bitrate (kbps) for MP3 extraction."""
        raw = self._db.get_setting("audio_bitrate")
        return raw if raw in ("128", "192", "256", "320") else "192"

    @audio_bitrate.setter
    def audio_bitrate(self, value: str) -> None:
        self._db.set_setting("audio_bitrate", value)

    @property
    def cookies_file(self) -> str:
        """A cookies.txt handed to yt-dlp for every video download (blank = off)."""
        return self._db.get_setting("cookies_file") or ""

    @cookies_file.setter
    def cookies_file(self, value: str) -> None:
        self._db.set_setting("cookies_file", value.strip())

    # ---------------------------------------------------------- torrent

    @property
    def torrent_encryption(self) -> str:
        """Peer encryption: "prefer" (default), "require", or "off"."""
        raw = self._db.get_setting("torrent_encryption")
        return raw if raw in ("prefer", "require", "off") else "prefer"

    @torrent_encryption.setter
    def torrent_encryption(self, value: str) -> None:
        if value not in ("prefer", "require", "off"):
            raise ValueError(f"unknown encryption mode: {value}")
        self._db.set_setting("torrent_encryption", value)

    @property
    def torrent_seed_minutes(self) -> int:
        """Stop seeding after this many minutes (0 = no time limit)."""
        return max(0, self._get_int("torrent_seed_minutes", 0))

    @torrent_seed_minutes.setter
    def torrent_seed_minutes(self, value: int) -> None:
        self._db.set_setting("torrent_seed_minutes", str(max(0, value)))

    @property
    def torrent_trackers(self) -> tuple[str, ...]:
        """Default tracker URLs offered when creating a torrent."""
        return self._get_str_list("torrent_trackers")

    @torrent_trackers.setter
    def torrent_trackers(self, value: Sequence[str]) -> None:
        deduped = list(dict.fromkeys(v.strip() for v in value if v.strip()))
        self._db.set_setting("torrent_trackers", json.dumps(deduped))

    # ---------------------------------------------------------- archive

    @property
    def extract_to_subfolder(self) -> bool:
        """Extract into a folder named after the archive (default: next to it)."""
        return self._get_bool("extract_to_subfolder", False)

    @extract_to_subfolder.setter
    def extract_to_subfolder(self, value: bool) -> None:
        self._set_bool("extract_to_subfolder", value)

    @property
    def delete_archive_after_extract(self) -> bool:
        """Remove the archive once it extracted cleanly."""
        return self._get_bool("delete_archive_after_extract", False)

    @delete_archive_after_extract.setter
    def delete_archive_after_extract(self, value: bool) -> None:
        self._set_bool("delete_archive_after_extract", value)

    # -------------------------------------------------- file management

    @property
    def default_tags(self) -> str:
        """Tags applied to every new download (comma separated; blank = none)."""
        return self._db.get_setting("default_tags") or ""

    @default_tags.setter
    def default_tags(self, value: str) -> None:
        self._db.set_setting("default_tags", value.strip())

    # ------------------------------------------------------------ queue

    @property
    def default_queue_id(self) -> int:
        """Queue for new downloads when no category rule claims them (0 = none)."""
        return max(0, self._get_int("default_queue_id", 0))

    @default_queue_id.setter
    def default_queue_id(self, value: int) -> None:
        self._db.set_setting("default_queue_id", str(max(0, value)))

    # -------------------------------------------------------- scheduler

    @property
    def battery_min_percent(self) -> int:
        """With battery pause on: only pause below this charge (0 = always)."""
        return max(0, min(100, self._get_int("battery_min_percent", 0)))

    @battery_min_percent.setter
    def battery_min_percent(self, value: int) -> None:
        self._db.set_setting("battery_min_percent", str(max(0, min(100, value))))

    # ---------------------------------------------------------- network

    @property
    def proxy_bypass(self) -> tuple[str, ...]:
        """Hosts that connect directly even when a proxy is set."""
        return self._get_str_list("proxy_bypass")

    @proxy_bypass.setter
    def proxy_bypass(self, value: Sequence[str]) -> None:
        deduped = list(dict.fromkeys(v.strip().lower() for v in value if v.strip()))
        self._db.set_setting("proxy_bypass", json.dumps(deduped))

    @property
    def user_agent(self) -> str:
        """A custom User-Agent for plain downloads (blank = default)."""
        return self._db.get_setting("user_agent") or ""

    @user_agent.setter
    def user_agent(self, value: str) -> None:
        self._db.set_setting("user_agent", value.strip())

    # --------------------------------------------------------- security

    @property
    def scanner_pref(self) -> str:
        """Virus scanner: "auto" (first found), "defender", or "clamav"."""
        raw = self._db.get_setting("scanner_pref")
        return raw if raw in ("auto", "defender", "clamav") else "auto"

    @scanner_pref.setter
    def scanner_pref(self, value: str) -> None:
        if value not in ("auto", "defender", "clamav"):
            raise ValueError(f"unknown scanner: {value}")
        self._db.set_setting("scanner_pref", value)

    @property
    def scan_extensions(self) -> str:
        """Only security-check these suffixes (comma separated; blank = all)."""
        return self._db.get_setting("scan_extensions") or ""

    @scan_extensions.setter
    def scan_extensions(self, value: str) -> None:
        self._db.set_setting("scan_extensions", value.strip())

    # ---------------------------------------------------- notifications

    @property
    def notify_on_failed(self) -> bool:
        return self._get_bool("notify_on_failed", True)

    @notify_on_failed.setter
    def notify_on_failed(self, value: bool) -> None:
        self._set_bool("notify_on_failed", value)

    @property
    def notify_queue_done(self) -> bool:
        """Notify when the whole queue drains."""
        return self._get_bool("notify_queue_done", False)

    @notify_queue_done.setter
    def notify_queue_done(self, value: bool) -> None:
        self._set_bool("notify_queue_done", value)

    @property
    def toast_seconds(self) -> int:
        """How long notifications stay up."""
        return max(1, min(30, self._get_int("toast_seconds", 4)))

    @toast_seconds.setter
    def toast_seconds(self, value: int) -> None:
        self._db.set_setting("toast_seconds", str(max(1, min(30, value))))

    @property
    def quiet_enabled(self) -> bool:
        """Suppress notifications and sounds inside the quiet window."""
        return self._get_bool("quiet_enabled", False)

    @quiet_enabled.setter
    def quiet_enabled(self, value: bool) -> None:
        self._set_bool("quiet_enabled", value)

    @property
    def quiet_from(self) -> str:
        return self._get_time("quiet_from", "22:00")

    @quiet_from.setter
    def quiet_from(self, value: str) -> None:
        self._db.set_setting("quiet_from", value)

    @property
    def quiet_to(self) -> str:
        return self._get_time("quiet_to", "08:00")

    @quiet_to.setter
    def quiet_to(self, value: str) -> None:
        self._db.set_setting("quiet_to", value)

    def in_quiet_hours(self) -> bool:
        """True when notifications should stay silent right now."""
        if not self.quiet_enabled:
            return False
        from datetime import datetime

        now = datetime.now().strftime("%H:%M")
        start, end = self.quiet_from, self.quiet_to
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # a window that crosses midnight

    # ------------------------------------------------------- statistics

    @property
    def stats_enabled(self) -> bool:
        """Record download statistics for the dashboard (always local-only)."""
        return self._get_bool("stats_enabled", True)

    @stats_enabled.setter
    def stats_enabled(self, value: bool) -> None:
        self._set_bool("stats_enabled", value)

    @property
    def stats_retention_days(self) -> int:
        """Prune per-day statistics older than this (0 = keep forever)."""
        return max(0, self._get_int("stats_retention_days", 0))

    @stats_retention_days.setter
    def stats_retention_days(self, value: int) -> None:
        self._db.set_setting("stats_retention_days", str(max(0, value)))

    @property
    def dashboard_refresh_ms(self) -> int:
        """Dashboard sampling interval."""
        return max(100, min(5000, self._get_int("dashboard_refresh_ms", 500)))

    @dashboard_refresh_ms.setter
    def dashboard_refresh_ms(self, value: int) -> None:
        self._db.set_setting("dashboard_refresh_ms", str(max(100, min(5000, value))))

    # ------------------------------------------------------- appearance

    @property
    def accent_color(self) -> str:
        """Accent override: "" = the brand blue, else a preset hex."""
        return self._db.get_setting("accent_color") or ""

    @accent_color.setter
    def accent_color(self, value: str) -> None:
        self._db.set_setting("accent_color", value.strip())

    @property
    def ui_density(self) -> str:
        raw = self._db.get_setting("ui_density")
        return raw if raw in ("comfortable", "compact") else "comfortable"

    @ui_density.setter
    def ui_density(self, value: str) -> None:
        if value not in ("comfortable", "compact"):
            raise ValueError(f"unknown density: {value}")
        self._db.set_setting("ui_density", value)

    @property
    def hidden_columns(self) -> tuple[str, ...]:
        """Downloads-list columns the user switched off ("size", "speed", …)."""
        return self._get_str_list("hidden_columns")

    @hidden_columns.setter
    def hidden_columns(self, value: Sequence[str]) -> None:
        self._db.set_setting("hidden_columns", json.dumps(sorted(set(value))))

    # --------------------------------------------------------- advanced

    @property
    def log_level(self) -> str:
        raw = self._db.get_setting("log_level")
        return raw if raw in ("debug", "info", "warning", "error") else "info"

    @log_level.setter
    def log_level(self, value: str) -> None:
        if value not in ("debug", "info", "warning", "error"):
            raise ValueError(f"unknown log level: {value}")
        self._db.set_setting("log_level", value)

    @property
    def log_to_file(self) -> bool:
        """Also write the log to grabline.log in the data folder."""
        return self._get_bool("log_to_file", False)

    @log_to_file.setter
    def log_to_file(self, value: bool) -> None:
        self._set_bool("log_to_file", value)
