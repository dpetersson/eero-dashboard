"""Every eero client method this app calls must exist on the installed eero-api.

eero-api 8.x renamed or removed get_profile_schedule, get_blocked_applications,
the activity getters, SQM setters and set_device_priority. This test turns a
silent runtime 500 into a loud install-time failure when the pin drifts.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eero import EeroClient  # noqa: E402

FEATURES = Path(__file__).resolve().parents[1] / "features"
# Methods the app degrades gracefully without (checked with hasattr at call time).
OPTIONAL = {"reboot_network"}
# Names matched by the regex that are not eero client calls.
NOT_CLIENT = {"ensure_client"}


class ClientSurfaceTests(unittest.TestCase):
    def test_all_called_methods_exist(self):
        have = {n for n in dir(EeroClient) if not n.startswith("_")}
        used = {}
        for path in FEATURES.rglob("*.py"):
            for m in re.finditer(r"\bclient\.(\w+)\(", path.read_text()):
                used.setdefault(m.group(1), set()).add(path.relative_to(FEATURES).as_posix())
        missing = {k: sorted(v) for k, v in used.items() if k not in have and k not in OPTIONAL | NOT_CLIENT}
        self.assertEqual(missing, {}, "eero-api is missing methods the app calls; check the version pin in requirements.txt")
        self.assertGreater(len(used), 40)


if __name__ == "__main__":
    unittest.main()
