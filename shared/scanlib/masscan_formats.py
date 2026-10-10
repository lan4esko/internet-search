"""Masscan result formats, normalized into the same endpoint records.

Shapes follow upstream out-json.c, out-text.c and out-xml.c. Samples are
illustrations using a documentation address, never discovered assets.
"""
import ipaddress
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


OUTPUT_EXTENSIONS = {"json": "json", "list": "txt", "xml": "xml"}
SAMPLE_BYTES = 16 * 1024


def output_example(output_format: str) -> str:
    if output_format == "json":
        return '[\n  {"ip":"192.0.2.10","timestamp":"1700000000","ports":[{"port":10443,"proto":"tcp","status":"open","reason":"syn-ack","ttl":64}]}\n]\n'
    if output_format == "list":
        return "#masscan\nopen tcp 10443 192.0.2.10 1700000000\n# end\n"
    if output_format == "xml":
        return '<?xml version="1.0"?>\n<nmaprun scanner="masscan">\n  <host endtime="1700000000"><address addr="192.0.2.10" addrtype="ipv4"/><ports><port protocol="tcp" portid="10443"><state state="open" reason="syn-ack" reason_ttl="64"/></port></ports></host>\n</nmaprun>\n'
    raise ValueError("Unsupported Masscan output format")


def read_output_sample(path: Path) -> tuple[str, int]:
    if not path.exists():
        return "", 0
    with path.open("rb") as source:
        raw = source.read(SAMPLE_BYTES)
    # NUL cannot be stored in PostgreSQL text. Other bytes remain readable;
    # the debug artifact, when enabled, preserves the original bytes.
    return raw.decode("utf-8", errors="replace").replace("\x00", "\\x00"), path.stat().st_size


def _decode_banner(value: str) -> str:
    # Masscan escapes banner bytes as \xNN in XML/list output. Decode only
    # that syntax; unicode_escape would corrupt non-ASCII literal text.
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), value).replace("\x00", "\\x00")


def _json_events(path: Path):
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("Masscan JSON must contain a list of records")
    for item in payload:
        for port in item.get("ports", []):
            service = port.get("service")
            if port.get("status") == "open" or (port.get("status") is None and isinstance(service, dict)):
                yield item["ip"], port["port"], port.get("proto", "tcp"), service


def _list_events(path: Path):
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 6)
            if len(parts) < 5 or parts[0] not in {"open", "closed", "banner", "up", "unknown"}:
                raise ValueError(f"Invalid Masscan list record at line {line_number}")
            state, protocol, port, address = parts[:4]
            if state == "open":
                yield address, port, protocol, None
            elif state == "banner":
                if len(parts) < 6:
                    raise ValueError(f"Invalid Masscan banner at line {line_number}")
                yield address, port, protocol, {"name": parts[5], "banner": _decode_banner(parts[6]) if len(parts) == 7 else ""}


class _NoEntitiesReader:
    """Reject DTD/entity declarations before passing chunks into Expat.

    Masscan never emits these constructs. Keeping a short carry handles a
    declaration split across file reads, without reading a whole XML file.
    """
    def __init__(self, source):
        self.source = source
        self.carry = b""

    def read(self, size=-1):
        chunk = self.source.read(size)
        if b"\x00" in chunk:
            raise ValueError("Masscan XML must use UTF-8 without NUL bytes")
        upper = (self.carry + chunk).upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise ValueError("Masscan XML must not contain DTD or entity declarations")
        self.carry = upper[-16:]
        return chunk


def _xml_events(path: Path):
    with path.open("rb") as source:
        root_seen = False
        for event, host in ET.iterparse(_NoEntitiesReader(source), events=("start", "end")):
            if not root_seen:
                root_seen = True
                if host.tag != "nmaprun":
                    raise ValueError("Masscan XML must contain an nmaprun root")
            if event != "end" or host.tag != "host":
                continue
            address = host.find("address")
            if address is None or not address.get("addr"):
                raise ValueError("Masscan XML host has no address")
            for port in host.findall("./ports/port"):
                state = port.find("state")
                if state is None or state.get("state") != "open":
                    continue
                service = port.find("service")
                data = None if service is None else {
                    "name": service.get("name"), "banner": _decode_banner(service.get("banner", "")),
                }
                yield address.get("addr"), port.get("portid"), port.get("protocol", "tcp"), data
            host.clear()


def parse_records(path: Path, output_format: str = "json") -> list[dict]:
    readers = {"json": _json_events, "list": _list_events, "xml": _xml_events}
    if output_format not in readers:
        raise ValueError("Unsupported Masscan output format")
    if not path.exists() or not path.stat().st_size:
        return []
    observations = {}
    for address, port, protocol, service in readers[output_format](path):
        address, port = str(ipaddress.ip_address(address)), int(port)
        if not 1 <= port <= 65535:
            raise ValueError("Masscan reported an invalid endpoint port")
        key = (address, port, protocol)
        record = observations.setdefault(key, {
            "ip_address": address, "port": port, "protocol": protocol,
            "service_name": None, "banner": None,
        })
        if isinstance(service, dict):
            name, banner = service.get("name"), service.get("banner")
            if isinstance(name, str) and name:
                record["service_name"] = name[:255]
            if isinstance(banner, str) and banner:
                previous = record["banner"]
                if not previous:
                    record["banner"] = banner.replace("\x00", "\\x00")
                elif banner != previous:
                    record["banner"] = f"{previous}\n{banner}".replace("\x00", "\\x00")
    return list(observations.values())
