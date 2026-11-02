from pydantic import BaseModel, Field


class LabelRequest(BaseModel):
    name: str | None = None
    owner: str | None = None
    notes: str | None = None
    trusted: bool | None = None
    eero_nickname: bool = False   # also write `name` to eero as the device nickname


class IdentifyStartRequest(BaseModel):
    macs: list[str] | None = None   # None = every unnamed online device with an address


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
