"""Fetch small upstream RAR test fixtures by pinned revision and SHA-256."""

from __future__ import annotations

import httpx
from common import ROOT, case, digest, save

commit = "d2f7df6fc843dae356fd6b0a85971dc36fd6e757"
fixtures = {
    "rar3-subdirs.rar": "d3d4bd8c90b22fa01c11f460589499de06f9d28e076ec3762eea738c0903e874",
    "rar5-crc.rar": "9e42359428fe8d6c364536a3fe2ff4b705b4af63dfe0f56bd5cac8d042abd803",
}
root = ROOT / "rar-fixtures"
root.mkdir(exist_ok=True)
provenance = []
with case("ARCHIVE-FIXTURE-PROVENANCE", "pinned upstream binary fixture identity") as evidence:
    for name, expected in fixtures.items():
        path = root / name
        url = f"https://raw.githubusercontent.com/markokr/rarfile/{commit}/test/files/{name}"
        if not path.exists():
            response = httpx.get(url, timeout=20, follow_redirects=True)
            response.raise_for_status()
            assert __import__("hashlib").sha256(response.content).hexdigest() == expected
            path.write_bytes(response.content)
        assert digest(path) == expected
        provenance.append({"name": name, "commit": commit, "url": url, "sha256": expected})
    save("rar-fixtures/provenance.json", provenance)
    evidence["fixtures"] = provenance
