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


class LanScanParseTests(unittest.TestCase):
    def test_parse_arp_darwin(self):
        from features.watch import lanscan
        text = (
            "? (192.168.7.1) at 98:ed:7e:1:2:3 on en0 ifscope [ethernet]\n"
            "? (192.168.7.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]\n"
            "? (192.168.7.50) at (incomplete) on en0 ifscope [ethernet]\n"
            "? (192.168.7.84) at b2:99:89:aa:bb:cc on en0 ifscope permanent [ethernet]\n"
        )
        table = lanscan.parse_arp_darwin(text)
        self.assertEqual(table, {"192.168.7.1": "98:ed:7e:01:02:03", "192.168.7.84": "b2:99:89:aa:bb:cc"})

    def test_parse_arp_linux(self):
        from features.watch import lanscan
        text = (
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.7.1      0x1         0x2         98:ed:7e:01:02:03     *        eth0\n"
            "192.168.7.50     0x1         0x0         00:00:00:00:00:00     *        eth0\n"
            "192.168.7.60     0x1         0x2         c4:4f:33:00:11:22     *        eth0\n"
        )
        table = lanscan.parse_arp_linux(text)
        self.assertEqual(table, {"192.168.7.1": "98:ed:7e:01:02:03", "192.168.7.60": "c4:4f:33:00:11:22"})

    def test_vendor_for_private_mac(self):
        from features.watch import lanscan
        self.assertEqual(lanscan.vendor_for("b2:99:89:aa:bb:cc"), "(randomised/private MAC)")
        self.assertEqual(lanscan.vendor_for(None), "")


class LanIngestTests(WatchStoreBase):
    def _scan(self, devices, cidr="192.168.7.0/24"):
        return {"source": "laptop", "network": {"cidr": cidr, "gateway_ip": "192.168.7.1",
                                                "gateway_mac": "98:ed:7e:01:02:03", "scanner_ip": "192.168.7.84"},
                "devices": devices}

    def test_lan_device_unknown_to_eero_is_flagged(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", ip="192.168.7.10")])
        result = service.ingest_lan_scan("net1", self._scan([
            {"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.7.10", "vendor": "Acme", "hostname": "host-1"},
            {"mac": "c4:4f:33:00:11:22", "ip": "192.168.7.60", "vendor": "Espressif Inc.", "hostname": ""},
        ]))
        self.assertEqual(result["new"], ["c4:4f:33:00:11:22"])
        self.assertEqual(result["count"], 2)
        rec = store.get_device("c4:4f:33:00:11:22")
        self.assertFalse(rec["trusted"])
        self.assertEqual(rec["first_source"], "lan")
        self.assertEqual(rec["oui_vendor"], "Espressif Inc.")
        self.assertEqual(rec["lan_ip"], "192.168.7.60")
        known = store.get_device("aa:bb:cc:dd:ee:01")
        self.assertEqual(known["lan_hostname"], "host-1")
        self.assertEqual(known["first_source"], "eero")
        self.assertIn("new_device_lan", [e["type"] for e in store.list_events("net1")])

    def test_cross_reference_in_summary(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", ip="192.168.7.10"),
                              _dev("aa:bb:cc:dd:ee:02", ip="192.168.7.11"),
                              _dev("aa:bb:cc:dd:ee:03", ip="192.168.11.5")])  # other subnet
        service.ingest_lan_scan("net1", self._scan([
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.7.10"},
            {"mac": "c4:4f:33:00:11:22", "ip": "192.168.7.60"},
        ]))
        s = service.summary("net1")
        cross = s["cross"]
        self.assertEqual(cross["last_scan"]["source"], "laptop")
        self.assertEqual(cross["both"], 1)            # ee:01 seen by both
        self.assertEqual(cross["eero_only"], 1)       # ee:02 on the scanned subnet but silent
        self.assertEqual([d["mac"] for d in cross["lan_only"]], ["c4:4f:33:00:11:22"])
        # ee:03 is on another subnet: not counted either way
        self.assertEqual(cross["both"] + cross["eero_only"] + len(cross["lan_only"]), 3)
        self.assertEqual(s["counts"]["untrusted"], 1)

    def test_lan_first_then_eero_baseline_trusts_it(self):
        service.ingest_lan_scan("net1", self._scan([{"mac": "c4:4f:33:00:11:22", "ip": "192.168.7.60"}]))
        self.assertFalse(store.get_device("c4:4f:33:00:11:22")["trusted"])
        result = service.poll("net1", [_dev("c4:4f:33:00:11:22", ip="192.168.7.60")])
        self.assertTrue(result["first_run"])
        self.assertTrue(store.get_device("c4:4f:33:00:11:22")["trusted"])

    def test_vendor_fallback_from_make_model(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", manufacturer="", inferred_make="Shelly", inferred_model="Plus 1")])
        rec = store.get_device("aa:bb:cc:dd:ee:01")
        self.assertEqual(service.best_vendor(rec), "Shelly Plus 1")
        self.assertEqual(rec["make_model"], "Shelly Plus 1")
        # and the summary surfaces it as `vendor` on any listed device
        store.label_device("aa:bb:cc:dd:ee:01", trusted=False)
        self.assertEqual(service.summary("net1")["untrusted"][0]["vendor"], "Shelly Plus 1")

    def test_push_endpoint_without_network_id(self):
        with patch.dict(os.environ, {"EERO_DEMO_DATA": "true", "WATCH_ENABLED": "false"}):
            demo.reset_for_tests()
            with patch.object(core_client, "ensure_client"):
                import main
                http = TestClient(main.app)
                r = http.post("/api/watch/lan-scan", json=self._scan([{"mac": "c4:4f:33:00:11:22", "ip": "192.168.7.60"}]))
                self.assertEqual(r.status_code, 409)  # no network id known yet
                http.post("/api/networks/fake-net-001/watch/poll")
                r = http.post("/api/watch/lan-scan", json=self._scan([{"mac": "c4:4f:33:00:11:22", "ip": "192.168.7.60"}]))
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["network_id"], "fake-net-001")
                self.assertEqual(r.json()["new"], ["c4:4f:33:00:11:22"])
                r = http.get("/api/networks/fake-net-001/watch/lan-scan")
                self.assertEqual(r.json()["last_scan"]["count"], 1)
                http.close()
            demo.reset_for_tests()


class NodesAndBaselineTests(WatchStoreBase):
    def test_eero_nodes_are_trusted_and_never_new(self):
        nodes = service.nodes_as_devices([
            {"mac_address": "98:ED:7E:00:00:01", "ip_address": "192.168.7.1", "location": "Living Room",
             "model": "eero Pro 6E", "status": "green", "url": "/2.2/eeros/1"},
            {"mac_address": None, "location": "broken"},
        ])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["nickname"], "eero Living Room")
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01")])
        result = service.poll("net1", [_dev("aa:bb:cc:dd:ee:01")] + nodes)
        self.assertEqual(result["new"], [])
        rec = store.get_device("98:ed:7e:00:00:01")
        self.assertTrue(rec["trusted"])
        self.assertEqual(rec["device_type"], "eero_node")
        self.assertNotIn("new_device", [e["type"] for e in store.list_events("net1")])

    def test_lan_seen_node_becomes_trusted_on_next_poll(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01")])
        service.ingest_lan_scan("net1", {"source": "x", "network": {"cidr": "192.168.7.0/24"},
                                         "devices": [{"mac": "98:ed:7e:00:00:01", "ip": "192.168.7.1"}]})
        self.assertFalse(store.get_device("98:ed:7e:00:00:01")["trusted"])
        nodes = service.nodes_as_devices([{"mac_address": "98:ed:7e:00:00:01", "ip_address": "192.168.7.1", "location": "Hall"}])
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01")] + nodes)
        self.assertTrue(store.get_device("98:ed:7e:00:00:01")["trusted"])

    def test_baseline_devices_are_not_new(self):
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01"), _dev("aa:bb:cc:dd:ee:02")])
        self.assertEqual(service.summary("net1")["counts"]["new"], 0)
        # real polls are minutes apart; the test runs inside one second, so age the baseline
        earlier = (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(microsecond=0).isoformat()
        store.set_meta("baseline_at:net1", earlier)
        with store._lock:
            store._db().execute("UPDATE devices SET first_seen = ?", (earlier,))
            store._db().commit()
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01"), _dev("aa:bb:cc:dd:ee:02"), _dev("aa:bb:cc:dd:ee:03")])
        s = service.summary("net1")
        self.assertEqual(s["counts"]["new"], 1)
        self.assertEqual(s["new"][0]["mac"], "aa:bb:cc:dd:ee:03")


class NodeLanAliasTests(WatchStoreBase):
    def test_wire_side_node_interface_is_matched_by_ip_and_by_hostname(self):
        nodes = service.nodes_as_devices([{"mac_address": "98:ed:7e:00:00:01", "ip_address": "192.168.7.1", "location": "Office"}])
        service.poll("net1", [_dev("aa:bb:cc:dd:ee:01", ip="192.168.7.10")] + nodes)
        earlier = (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(microsecond=0).isoformat()
        store.set_meta("baseline_at:net1", earlier)
        with store._lock:
            store._db().execute("UPDATE devices SET first_seen = ?", (earlier,))
            store._db().commit()
        result = service.ingest_lan_scan("net1", {"source": "x", "network": {"cidr": "192.168.7.0/24"}, "devices": [
            {"mac": "98:ed:7e:00:00:02", "ip": "192.168.7.1", "vendor": "eero inc.", "hostname": "eero-xe7d.local"},   # by IP
            {"mac": "98:ed:7e:00:00:09", "ip": "192.168.7.250", "vendor": "eero inc.", "hostname": "eero-45j5.local"}, # by vendor+hostname
            {"mac": "c4:4f:33:00:11:22", "ip": "192.168.7.60", "vendor": "Espressif Inc.", "hostname": ""},            # genuinely unknown
        ]})
        self.assertEqual(result["new"], ["c4:4f:33:00:11:22"])
        a = store.get_device("98:ed:7e:00:00:02")
        self.assertTrue(a["trusted"]); self.assertEqual(a["device_type"], "eero_node"); self.assertIn("Office", a["name"])
        b = store.get_device("98:ed:7e:00:00:09")
        self.assertTrue(b["trusted"]); self.assertEqual(b["device_type"], "eero_node")
        s = service.summary("net1")
        self.assertEqual([d["mac"] for d in s["cross"]["lan_only"]], ["c4:4f:33:00:11:22"])
        self.assertEqual(s["counts"]["new"], 1)  # node interfaces are never "new"
        self.assertEqual([e["mac"] for e in store.list_events("net1") if e["type"] == "new_device_lan"], ["c4:4f:33:00:11:22"])
