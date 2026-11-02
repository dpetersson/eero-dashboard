import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

import core.client as core_client  # noqa: E402
from core import demo  # noqa: E402
from core import lan_guard  # noqa: E402
from features.watch import service, store  # noqa: E402


def _dev(mac, ip="192.168.1.10", up=0, connected=True, **extra):
    d = {"mac": mac, "ip": ip, "hostname": "host-" + mac[-2:], "manufacturer": "Acme",
         "connected": connected, "usage": {"up": up, "down": 100}}
    d.update(extra)
    return d


class WatchStoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store.use_path(Path(self.tmp.name) / "watch.sqlite3")
        self.env = patch.dict(os.environ, {"WATCH_WEBHOOK": "", "WATCH_UPLOAD_SAMPLES": "3"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        store.use_path(None)
        self.tmp.cleanup()


class PollTests(WatchStoreBase):
    def test_first_poll_is_baseline_and_trusts_everything(self):
        result = service.poll("net1", [_dev("AA:BB:CC:DD:EE:01"), _dev("aa-bb-cc-dd-ee-02")])
        self.assertTrue(result["first_run"])
        self.assertEqual(result["online"], 2)
        self.assertEqual(result["new"], [])
        devices = {d["mac"]: d for d in store.list_devices("net1")}
        self.assertEqual(set(devices), {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"})
        self.assertTrue(all(d["trusted"] for d in devices.values()))
        self.assertEqual([e["type"] for e in store.list_events("net1")], ["baseline"])

    def test_second_poll_flags_new_device_untrusted(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01")])
        result = service.poll("net1", [_dev("aa:bb:cc:dd:ee:01"), _dev("aa:bb:cc:dd:ee:03", ip="192.168.1.33")])
        self.assertEqual(result["new"], ["aa:bb:cc:dd:ee:03"])
        rec = store.get_device("aa:bb:cc:dd:ee:03")
        self.assertFalse(rec["trusted"])
        self.assertEqual(rec["last_ip"], "192.168.1.33")
        types = [e["type"] for e in store.list_events("net1")]
        self.assertIn("new_device", types)

    def test_disconnected_devices_are_ignored(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01")])
        result = service.poll("net1", [_dev("aa:bb:cc:dd:ee:01"), _dev("aa:bb:cc:dd:ee:09", connected=False)])
        self.assertEqual(result["new"], [])
        self.assertIsNone(store.get_device("aa:bb:cc:dd:ee:09"))

    def test_returned_after_absence(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01")])
        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        with store._lock:
            store._db().execute("UPDATE devices SET last_seen = ? WHERE mac = ?", (old, "aa:bb:cc:dd:ee:01"))
            store._db().commit()
        result = service.poll("net1", [_dev("aa:bb:cc:dd:ee:01")])
        self.assertEqual(result["returned"], ["aa:bb:cc:dd:ee:01"])

    def test_steady_upload_rule_fires_once_per_day(self):
        for _ in range(3):
            service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", up=500), _dev("aa:bb:cc:dd:ee:02", up=0)])
        types = [(e["type"], e["mac"]) for e in store.list_events("net1")]
        self.assertIn(("steady_upload", "aa:bb:cc:dd:ee:01"), types)
        self.assertNotIn(("steady_upload", "aa:bb:cc:dd:ee:02"), types)
        count_before = types.count(("steady_upload", "aa:bb:cc:dd:ee:01"))
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", up=500)])
        types = [(e["type"], e["mac"]) for e in store.list_events("net1")]
        self.assertEqual(types.count(("steady_upload", "aa:bb:cc:dd:ee:01")), count_before)

    def test_ok_note_mutes_upload_rule(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", up=500)])
        store.label_device("aa:bb:cc:dd:ee:01", notes="ok: backup NAS uploads overnight", trusted=True)
        for _ in range(3):
            service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", up=500)])
        types = [e["type"] for e in store.list_events("net1")]
        self.assertNotIn("steady_upload", types)

    def test_summary_shape(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", up=10)])
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", up=10), _dev("aa:bb:cc:dd:ee:02")])
        s = service.summary("net1")
        self.assertEqual(s["counts"]["total"], 2)
        self.assertEqual(s["counts"]["untrusted"], 1)
        self.assertEqual(s["untrusted"][0]["mac"], "aa:bb:cc:dd:ee:02")
        self.assertTrue(s["untrusted"][0]["online"])
        self.assertIn("display_name", s["untrusted"][0])
        self.assertIn("probe_available", s["config"])

    def test_usage_shape_variants(self):
        self.assertEqual(service._usage({"usage": {"up_mbps": 1.5, "down_mbps": 2}}), (1.5, 2.0))
        self.assertEqual(service._usage({"usage": {"up": "7", "down": None}}), (7.0, None))
        self.assertEqual(service._usage({}), (None, None))
        self.assertEqual(service.norm_mac("A-B-C-D-E-F"), "0a:0b:0c:0d:0e:0f")
        self.assertIsNone(service.norm_mac("not-a-mac"))


class WatchApiTests(WatchStoreBase):
    def setUp(self):
        super().setUp()
        self.env_demo = patch.dict(os.environ, {"EERO_DEMO_DATA": "true", "WATCH_ENABLED": "false"})
        self.env_demo.start()
        demo.reset_for_tests()
        self._ensure = patch.object(core_client, "ensure_client")
        self._ensure.start()
        import main
        self.http = TestClient(main.app)
        self.base = "/api/networks/%s/watch" % __import__("core.demo_data", fromlist=["NETWORK_ID"]).NETWORK_ID

    def tearDown(self):
        self.http.close()
        self._ensure.stop()
        self.env_demo.stop()
        demo.reset_for_tests()
        super().tearDown()

    def test_poll_label_and_summary_round_trip_in_demo_mode(self):
        r = self.http.post(self.base + "/poll")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["results"][0]["first_run"])

        r = self.http.get(self.base + "/summary")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreater(body["counts"]["total"], 0)
        self.assertEqual(body["counts"]["untrusted"], 0)

        mac = store.list_devices(body and self._network_id())[0]["mac"]
        r = self.http.patch("%s/devices/%s" % (self.base, mac),
                            json={"name": "Hall camera", "owner": "house", "trusted": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "Hall camera")
        self.assertFalse(r.json()["trusted"])

        r = self.http.get(self.base + "/summary")
        self.assertEqual(r.json()["counts"]["untrusted"], 1)

        r = self.http.get("%s/devices/%s" % (self.base, mac))
        self.assertEqual(r.status_code, 200)
        self.assertIn("samples", r.json())

        r = self.http.post(self.base + "/baseline")
        self.assertEqual(r.json()["trusted"], 1)

        r = self.http.get("%s/devices/%s" % (self.base, "00:00:00:00:00:00"))
        self.assertEqual(r.status_code, 404)

    def _network_id(self):
        from core import demo_data
        return demo_data.NETWORK_ID


class LanGuardTests(unittest.TestCase):
    def test_private_and_loopback_allowed_by_default(self):
        with patch.dict(os.environ, {"WATCH_ALLOWED_CIDRS": ""}):
            self.assertTrue(lan_guard._allowed("127.0.0.1"))
            self.assertTrue(lan_guard._allowed("192.168.7.84"))
            self.assertTrue(lan_guard._allowed("10.0.0.5"))
            self.assertFalse(lan_guard._allowed("8.8.8.8"))
            self.assertFalse(lan_guard._allowed(None))
            self.assertFalse(lan_guard._allowed("garbage"))

    def test_allowed_cidrs_tightens(self):
        with patch.dict(os.environ, {"WATCH_ALLOWED_CIDRS": "192.168.7.0/24"}):
            self.assertTrue(lan_guard._allowed("192.168.7.84"))
            self.assertTrue(lan_guard._allowed("127.0.0.1"))
            self.assertFalse(lan_guard._allowed("192.168.4.20"))
            self.assertFalse(lan_guard._allowed("10.0.0.5"))


if __name__ == "__main__":
    unittest.main()
