"""The image build records the vendor-shipped units (SB1, 2026-10-01): see
test_secrules_procs for what procview does with them."""
import base64
import json
import re
import subprocess
from pathlib import Path

from backend.vm import builder_guest, images, procview

ROOT = Path(__file__).resolve().parents[1]


def test_the_base_image_build_records_units_vendor(tmp_path):
    script = (ROOT / "vm" / "build_base.sh").read_text()
    assert "emit units_vendor" in script
    code = re.search(r"# --- baseline-parse.*?# --- end baseline-parse ---", script, re.S).group(0)
    b = lambda s: base64.b64encode(s.encode()).decode()   # noqa: E731
    log = tmp_path / "console.log"
    log.write_text(f"JAV3-BASELINE dpkg {b('git\t1:2\n')}\n"
                   f"JAV3-BASELINE units_vendor {b('apt-listchanges.service\nfstrim.service\n')}\n"
                   "JAV3-BASELINE-END\n")
    (tmp_path / "p.py").write_text(code)
    dst = tmp_path / "base-v9.baseline.json"
    r = subprocess.run(["python3", str(tmp_path / "p.py"), str(log), str(dst), "base-v9.qcow2"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    bl = json.loads(dst.read_text())
    assert bl["units_vendor"] == ["apt-listchanges.service", "fstrim.service"]
    # ...and procview trusts them
    assert procview.parse_baseline(bl).matches("/usr/bin/apt-get", "apt-listchanges.service")


def test_a_layer_build_and_the_host_keep_units_vendor():
    assert "units_vendor" in Path(builder_guest.__file__).read_text()
    kept = images._sanitize_baseline({"units_vendor": [f"u{i}.service" for i in range(3000)],
                                      "units_enabled": ["a.service"]})
    assert len(kept["units_vendor"]) == 3000 and kept["units_enabled"] == ["a.service"]
