"""Optional nmap probe of a single device for camera / NVR services.

Only works when nmap is installed in the container (or host) and the backend
runs on the same LAN as the device. Unprivileged TCP connect scans are used
so no root is required.
"""

from __future__ import annotations

import asyncio
import re
import shutil

# 554/8554 RTSP, 1935 RTMP, 8000 Hikvision, 37777 Dahua, 34567 XMeye, 9000 misc NVR,
# 80/443/8080/8443 web UI.
CAMERA_PORTS = "80,443,554,1935,8000,8080,8443,8554,9000,34567,37777"
CAMERA_HINTS = {
    554: "RTSP video stream",
    8554: "RTSP video stream (alt)",
    1935: "RTMP video stream",
    8000: "Hikvision SDK port",
    37777: "Dahua SDK port",
    34567: "XMeye / generic DVR port",
}

_PORT_RE = re.compile(r"^(\d+)/tcp\s+(\S+)\s+(\S+)\s*(.*)$")


def available() -> bool:
    return shutil.which("nmap") is not None


async def probe(ip: str, timeout: int = 120) -> dict:
    if not available():
        return {"ok": False, "error": "nmap is not installed on the dashboard host"}
    cmd = ["nmap", "-Pn", "-sV", "--version-light", "-p", CAMERA_PORTS, "--host-timeout", "%ds" % timeout, ip]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 15)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "nmap timed out"}
    except OSError as e:
        return {"ok": False, "error": str(e)}

    ports = []
    for line in out.decode(errors="replace").splitlines():
        m = _PORT_RE.match(line.strip())
        if not m:
            continue
        port = int(m.group(1))
        ports.append({
            "port": port,
            "state": m.group(2),
            "service": m.group(3),
            "version": m.group(4).strip(),
            "hint": CAMERA_HINTS.get(port, ""),
        })
    open_ports = [p for p in ports if p["state"] == "open"]
    camera_like = [p for p in open_ports if p["port"] in CAMERA_HINTS]
    return {
        "ok": proc.returncode == 0,
        "ip": ip,
        "ports": ports,
        "open_count": len(open_ports),
        "camera_like": [p["port"] for p in camera_like],
        "verdict": (
            "camera/NVR service detected" if camera_like
            else "no camera service; cloud-only cameras expose no ports" if open_ports
            else "no open ports; cloud-only cameras expose no ports"
        ),
        "stderr": err.decode(errors="replace")[-500:],
    }
