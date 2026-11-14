"""Windows first-chance COM exceptions must not walk concurrently mutating frames."""

import faulthandler
import sys

import pytest

from app import __main__ as entry
from app.core import paths


@pytest.mark.parametrize(("platform", "expected_all_threads"), [("win32", False), ("linux", True)])
def test_main_configures_safe_native_traceback_scope(
    tmp_path, monkeypatch, platform, expected_all_threads
):
    class StopAfterConfiguration(BaseException):
        pass

    observed = []

    def capture(stream, *, all_threads=True):
        observed.append(all_threads)
        stream.close()
        raise StopAfterConfiguration

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(faulthandler, "enable", capture)
    with pytest.raises(StopAfterConfiguration):
        entry.main()
    assert observed == [expected_all_threads]
