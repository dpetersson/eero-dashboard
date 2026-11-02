import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from features.watch import service, store, tuya_import  # noqa: E402


def _dev(mac, ip, **extra):
    d = {"mac": mac, "ip": ip, "hostname": "esp" + mac[-8:].replace(":", ""), "manufacturer": "Espressif Inc.",
         "connected": True, "usage": {"up_mbps": 0, "down_mbps": 0}, "url": "/2.2/devices/" + mac[-2:]}
    d.update(extra)
    return d


class TuyaImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        store.use_path(root / "watch.sqlite3")
        tuya_import.use_dir(root / "tuya")
        (root / "tuya").mkdir()
        self.env = patch.dict(os.environ, {"WATCH_WEBHOOK": ""})
        self.env.start()
        service.poll("net1", [_dev("c4:4f:33:00:11:22", "192.168.7.49"),
                              _dev("3c:61:05:00:00:01", "192.168.7.179"),
                              _dev("70:03:9f:00:00:02", "192.168.7.52")])
        store.label_device("70:03:9f:00:00:02", name="Already named")

    def tearDown(self):
        self.env.stop()
        tuya_import.use_dir(None)
        store.use_path(None)
        self.tmp.cleanup()

    def _write(self, devices, snapshot=None):
        (Path(self.tmp.name) / "tuya" / "devices.json").write_text(json.dumps(devices))
        if snapshot is not None:
            (Path(self.tmp.name) / "tuya" / "snapshot.json").write_text(json.dumps(snapshot))

    def test_preview_matches_by_mac_then_ip_and_never_leaks_keys(self):
        self._write([
            {"name": "Kitchen island dimmer", "id": "aaa111", "key": "SECRET1", "mac": "C4-4F-33-00-11-22", "product_name": "Feit Smart Dimmer", "category": "tgq"},
            {"name": "Porch switch", "id": "bbb222", "key": "SECRET2", "product_name": "Feit Smart Switch", "category": "kg"},
            {"name": "Unknown thing", "id": "ccc333", "key": "SECRET3"},
        ], snapshot={"devices": [{"id": "bbb222", "ip": "192.168.7.179", "ver": "3.3"}]})
        pv = tuya_import.preview("net1")
        self.assertTrue(pv["file"]); self.assertEqual(pv["count"], 3); self.assertEqual(pv["with_key"], 3)
        by = {m["mac"]: m for m in pv["matches"]}
        self.assertEqual(by["c4:4f:33:00:11:22"]["matched_by"], "mac")
        self.assertEqual(by["3c:61:05:00:00:01"]["matched_by"], "ip")
        self.assertEqual(by["3c:61:05:00:00:01"]["tuya"]["version"], "3.3")
        self.assertEqual([t["id"] for t in pv["unmatched"]], ["ccc333"])
        self.assertNotIn("SECRET", json.dumps(pv))

    def test_apply_names_trusts_tags_and_respects_only_unnamed(self):
        self._write([
            {"name": "Kitchen island dimmer", "id": "aaa111", "key": "k", "mac": "c4:4f:33:00:11:22", "product_name": "Feit Smart Dimmer", "category": "tgq"},
            {"name": "Hall switch", "id": "ddd444", "key": "k", "mac": "70:03:9f:00:00:02"},
        ])
        store.label_device("c4:4f:33:00:11:22", trusted=False)
        import asyncio
        with patch.object(service, "set_eero_nickname", AsyncMock(return_value={"ok": True})) as nick:
            res = asyncio.run(tuya_import.apply("net1", only_unnamed=True, eero_nickname=True))
        self.assertEqual([r["mac"] for r in res["renamed"]], ["c4:4f:33:00:11:22"])
        self.assertEqual(res["skipped"], ["70:03:9f:00:00:02"])   # already named, left alone
        rec = store.get_device("c4:4f:33:00:11:22")
        self.assertEqual(rec["name"], "Kitchen island dimmer")
        self.assertTrue(rec["trusted"])
        self.assertEqual(rec["tuya_id"], "aaa111")
        self.assertIn("Feit Smart Dimmer", rec["notes"])
        self.assertEqual(nick.await_count, 1)
        self.assertEqual(res["eero_nicknames_set"], 1)
        self.assertIn("tuya_import", [e["type"] for e in store.list_events("net1")])
        # second run with only_unnamed=False renames the already-named device too
        with patch.object(service, "set_eero_nickname", AsyncMock(return_value={"ok": False, "error": "not logged in"})):
            res = asyncio.run(tuya_import.apply("net1", only_unnamed=False))
        self.assertEqual(store.get_device("70:03:9f:00:00:02")["name"], "Hall switch")
        self.assertEqual(len(res["eero_failed"]), 2)

    def test_local_key_lookup_is_file_only(self):
        self._write([{"name": "x", "id": "aaa111", "key": "SECRET1", "mac": "c4:4f:33:00:11:22"}])
        self.assertEqual(tuya_import.local_key("aaa111"), "SECRET1")
        self.assertIsNone(tuya_import.local_key("nope"))

    def test_missing_file(self):
        self.assertFalse(tuya_import.available())
        pv = tuya_import.preview("net1")
        self.assertFalse(pv["file"]); self.assertEqual(pv["count"], 0)


if __name__ == "__main__":
    unittest.main()
