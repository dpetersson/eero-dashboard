from pydantic import BaseModel, Field


class LabelRequest(BaseModel):
    name: str | None = None
    owner: str | None = None
    notes: str | None = None
    trusted: bool | None = None


class LanScanDevice(BaseModel):
    mac: str
    ip: str = ""
    vendor: str = ""
    hostname: str = ""


class LanScanNetwork(BaseModel):
    cidr: str = ""
    gateway_ip: str = ""
    gateway_mac: str | None = ""
    iface: str = ""
    scanner_ip: str = ""


class LanScanPush(BaseModel):
    source: str = "netscan"
    network: LanScanNetwork = Field(default_factory=LanScanNetwork)
    devices: list[LanScanDevice] = Field(default_factory=list)
