"""LAN sweep: the second, independent sighting source for the watch inventory.

Ported from the standalone ``netscan.py``. Pings every address in the local
subnet (which forces an ARP exchange), reads the ARP table, looks vendors up
in the OUI table, and resolves names through reverse DNS and mDNS. Needs no
root and no third-party tools. Works on macOS and Linux (host networking in a
container).

Why a second source: eero's cloud list knows every DHCP client and its usage,
but it is eero's opinion. An ARP sweep is what is actually answering on the
wire, it fills vendor names eero leaves blank, and it keeps working when the
eero cloud is unreachable.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import csv
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("eero.watch.lanscan")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
OUI_FILE = DATA_DIR / "oui.csv"
OUI_URL = "https://standards-oui.ieee.org/oui/oui.csv"
NMAP_PREFIX_PATHS = [
    "/opt/homebrew/share/nmap/nmap-mac-prefixes",
    "/usr/local/share/nmap/nmap-mac-prefixes",
    "/usr/share/nmap/nmap-mac-prefixes",
]
IS_DARWIN = sys.platform == "darwin"


# --------------------------------------------------------------------------- helpers

def norm_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    parts = mac.strip().lower().replace("-", ":").split(":")
    if len(parts) != 6:
        return None
    try:
        return ":".join("%02x" % int(p, 16) for p in parts)
    except ValueError:
        return None


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def available() -> bool:
    """True when the host can run a sweep (ping present and a default route exists)."""
    return shutil.which("ping") is not None and detect_network(strict=False) is not None


# --------------------------------------------------------------------------- network detection

def detect_network(subnet_override: str | None = None, strict: bool = True) -> dict[str, Any] | None:
    """Interface, address, CIDR, gateway and the gateway's MAC."""
    try:
        if IS_DARWIN:
            info = _detect_darwin()
        else:
            info = _detect_linux()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        if strict:
            raise
        logger.debug("detect_network failed: %s", e)
        return None
    if info is None:
        return None
    if subnet_override:
        info["cidr"] = str(ipaddress.ip_network(subnet_override, strict=False))
    if info.get("gateway_ip"):
        _ping_one(info["gateway_ip"])
        info["gateway_mac"] = read_arp_table().get(info["gateway_ip"])
    else:
        info["gateway_mac"] = None
    return info


def _detect_darwin() -> dict[str, Any] | None:
    r = _run(["route", "-n", "get", "default"])
    iface = gateway = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("interface:"):
            iface = line.split(":", 1)[1].strip()
        elif line.startswith("gateway:"):
            gateway = line.split(":", 1)[1].strip()
    if not iface:
        return None
    r = _run(["ifconfig", iface])
    ip = netmask = self_mac = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            m = re.match(r"inet (\S+) netmask (0x[0-9a-f]+)", line)
            if m:
                ip, netmask = m.group(1), int(m.group(2), 16)
        elif line.startswith("ether "):
            self_mac = norm_mac(line.split()[1])
    if not ip or netmask is None:
        return None
    prefix = bin(netmask).count("1")
    net = ipaddress.ip_network("%s/%d" % (ip, prefix), strict=False)
    return {"iface": iface, "ip": ip, "cidr": str(net), "gateway_ip": gateway, "self_mac": self_mac}


def _detect_linux() -> dict[str, Any] | None:
    r = _run(["ip", "-4", "route", "show", "default"])
    m = re.search(r"default via (\S+) dev (\S+)", r.stdout)
    if not m:
        return None
    gateway, iface = m.group(1), m.group(2)
    r = _run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    m = re.search(r"inet (\S+)/(\d+)", r.stdout)
    if not m:
        return None
    ip, prefix = m.group(1), int(m.group(2))
    net = ipaddress.ip_network("%s/%d" % (ip, prefix), strict=False)
    self_mac = None
    try:
        self_mac = norm_mac(Path("/sys/class/net/%s/address" % iface).read_text().strip())
    except OSError:
        pass
    return {"iface": iface, "ip": ip, "cidr": str(net), "gateway_ip": gateway, "self_mac": self_mac}


# --------------------------------------------------------------------------- ARP table

_ARP_DARWIN_RE = re.compile(r"\S+ \((\S+)\) at ([0-9a-f:]+) on (\S+)", re.I)


def parse_arp_darwin(text: str) -> dict[str, str]:
    table = {}
    for line in text.splitlines():
        m = _ARP_DARWIN_RE.match(line)
        if not m:
            continue
        mac = norm_mac(m.group(2))
        if mac and mac != "ff:ff:ff:ff:ff:ff":
            table[m.group(1)] = mac
    return table


def parse_arp_linux(text: str) -> dict[str, str]:
    """/proc/net/arp: IP address, HW type, Flags, HW address, Mask, Device."""
    table = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        ip, flags, hw = parts[0], parts[2], parts[3]
        if flags == "0x0" or hw == "00:00:00:00:00:00":
            continue
        mac = norm_mac(hw)
        if mac and mac != "ff:ff:ff:ff:ff:ff":
            table[ip] = mac
    return table


def read_arp_table() -> dict[str, str]:
    if IS_DARWIN:
        return parse_arp_darwin(_run(["arp", "-an"]).stdout)
    try:
        return parse_arp_linux(Path("/proc/net/arp").read_text())
    except OSError:
        r = _run(["ip", "-4", "neigh"])
        table = {}
        for line in r.stdout.splitlines():
            m = re.match(r"(\S+) dev \S+ lladdr ([0-9a-f:]+) (\S+)", line)
            if m and m.group(3) not in ("FAILED", "INCOMPLETE"):
                mac = norm_mac(m.group(2))
                if mac:
                    table[m.group(1)] = mac
        return table


# --------------------------------------------------------------------------- vendor lookup

_oui_cache: dict[str, str] | None = None


def load_oui() -> dict[str, str]:
    global _oui_cache
    if _oui_cache is not None:
        return _oui_cache
    table: dict[str, str] = {}
    for path in NMAP_PREFIX_PATHS:
        if os.path.exists(path):
            with open(path, errors="replace") as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    prefix, _, vendor = line.strip().partition(" ")
                    table[prefix.lower()] = vendor
            break
    if OUI_FILE.exists():
        with open(OUI_FILE, newline="", errors="replace") as f:
            for row in csv.DictReader(f):
                try:
                    table[row["Assignment"].lower()] = row["Organization Name"].strip()
                except KeyError:
                    break
    _oui_cache = table
    return table


def vendor_for(mac: str | None) -> str:
    if not mac:
        return ""
    v = load_oui().get(mac.replace(":", "")[:6], "")
    if not v and int(mac[:2], 16) & 0x02:
        return "(randomised/private MAC)"
    return v


def oui_available() -> bool:
    return bool(load_oui())


def update_oui() -> int:
    """Download the IEEE OUI table into the data dir. Returns bytes written."""
    global _oui_cache
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(OUI_URL, headers={"User-Agent": "eero-dashboard-watch/1.0"})
    tmp = OUI_FILE.with_suffix(".tmp")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    os.replace(tmp, OUI_FILE)
    _oui_cache = None
    return OUI_FILE.stat().st_size


# --------------------------------------------------------------------------- sweep

def _ping_one(ip: str) -> None:
    cmd = ["ping", "-c", "1", "-q"]
    cmd += ["-W", "500"] if IS_DARWIN else ["-W", "1"]  # macOS: ms, Linux: seconds
    try:
        subprocess.run(cmd + [ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (subprocess.SubprocessError, OSError):
        pass


def _resolve_name(ip: str) -> str:
    dig = shutil.which("dig")
    if dig:
        for extra in ([], ["-p", "5353", "@224.0.0.251"]):
            try:
                r = _run([dig, "+short", "+time=1", "+tries=1", *extra, "-x", ip], timeout=5)
            except subprocess.SubprocessError:
                continue
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[0].rstrip(".")
        return ""
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return ""


def sweep(net_info: dict[str, Any], passes: int = 2, resolve: bool = True) -> list[dict[str, Any]]:
    """Ping every host (twice: WiFi devices in power-save miss the first), read ARP."""
    net = ipaddress.ip_network(net_info["cidr"])
    hosts = [str(h) for h in net.hosts()]
    if len(hosts) > 4096:
        raise ValueError("subnet %s too large to sweep (%d hosts)" % (net, len(hosts)))
    for _ in range(passes):
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            list(ex.map(_ping_one, hosts))

    found: dict[str, dict[str, Any]] = {}
    for ip, mac in read_arp_table().items():
        try:
            if ipaddress.ip_address(ip) not in net:
                continue
        except ValueError:
            continue
        found.setdefault(mac, {"ip": ip, "mac": mac, "vendor": vendor_for(mac), "hostname": ""})
    if net_info.get("self_mac") and net_info["self_mac"] not in found:
        found[net_info["self_mac"]] = {"ip": net_info["ip"], "mac": net_info["self_mac"],
                                       "vendor": vendor_for(net_info["self_mac"]),
                                       "hostname": socket.gethostname()}

    devices = sorted(found.values(), key=lambda d: ipaddress.ip_address(d["ip"]))
    if resolve:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            names = list(ex.map(lambda d: _resolve_name(d["ip"]), devices))
        for d, name in zip(devices, names):
            if name and not d["hostname"]:
                d["hostname"] = name
    return devices


def scan(subnet_override: str | None = None, source: str | None = None) -> dict[str, Any]:
    """Full synchronous scan. Returns the same shape netscan.py pushes."""
    net = detect_network(subnet_override)
    if net is None:
        raise RuntimeError("no default route; is this host on a network?")
    devices = sweep(net)
    return {
        "source": source or socket.gethostname(),
        "network": {"cidr": net["cidr"], "gateway_ip": net["gateway_ip"],
                    "gateway_mac": net["gateway_mac"], "iface": net["iface"], "scanner_ip": net["ip"]},
        "devices": devices,
    }


async def scan_async(subnet_override: str | None = None, source: str | None = None) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, scan, subnet_override, source)
