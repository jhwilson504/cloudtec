"""A small NetFlow/IPFIX collector, so the Ecosystem map can show where traffic leaves the network.

The edge FortiGate (or any router) sends flow records to KasTec over UDP (default port 2055):
    config system netflow
        set collector-ip <KasTec server IP>
        set collector-port 2055
    end
    config system interface / edit wan1 / set netflow-sampler both
NetFlow v5, v9 and IPFIX (v10) are understood. KasTec keeps the last hour in memory, one bucket per minute,
summed by source, destination, protocol and port. Addresses in the internal ranges (private, CGNAT and any
you add) are "inside"; everything else is "outside", named by reverse DNS where possible.

Only addresses, ports and byte counts are kept: no packet contents.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import struct
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_INTERNAL = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]
KEEP_MINUTES = 60
MAX_KEYS_PER_MINUTE = 50_000
# A few well-known providers, by the end of their reverse DNS names.
PROVIDERS = [("zscaler", "Zscaler"), ("zscloud.net", "Zscaler"), ("zpa", "Zscaler"), ("microsoft.com", "Microsoft"),
             ("msedge.net", "Microsoft"), ("office365", "Microsoft 365"), ("office.com", "Microsoft 365"), ("outlook.com", "Microsoft 365"),
             ("windows.net", "Microsoft Azure"), ("azure", "Microsoft Azure"), ("1e100.net", "Google"), ("google", "Google"),
             ("amazonaws.com", "Amazon AWS"), ("cloudfront.net", "Amazon CloudFront"), ("akamai", "Akamai"), ("akadns", "Akamai"),
             ("cloudflare", "Cloudflare"), ("fastly", "Fastly"), ("facebook", "Meta"), ("fbcdn", "Meta"),
             ("apple.com", "Apple"), ("salesforce", "Salesforce"), ("servicenow", "ServiceNow"), ("vmware", "VMware"),
             ("broadcom", "Broadcom"), ("fortinet", "Fortinet"), ("solarwinds", "SolarWinds"), ("dell", "Dell")]


# --- parsing --------------------------------------------------------------------------------------

V9_FIELDS = {1: "bytes", 2: "packets", 4: "proto", 7: "sport", 8: "src", 10: "in_if", 11: "dport", 12: "dst", 14: "out_if",
             23: "out_bytes", 24: "out_packets", 85: "bytes", 86: "packets", 27: "src6", 28: "dst6"}


def _ip4(b: bytes) -> str:
    return socket.inet_ntoa(b)


def _value(field: str, raw: bytes):
    if field in ("src", "dst"):
        return _ip4(raw) if len(raw) == 4 else None
    if field in ("src6", "dst6"):
        return str(ipaddress.IPv6Address(raw)) if len(raw) == 16 else None
    return int.from_bytes(raw, "big")


def parse_v5(data: bytes) -> list[dict]:
    count = struct.unpack("!H", data[2:4])[0]
    sampling = struct.unpack("!H", data[22:24])[0] & 0x3FFF or 1
    out = []
    for i in range(count):
        r = data[24 + i * 48: 72 + i * 48]
        if len(r) < 48:
            break
        src, dst = _ip4(r[0:4]), _ip4(r[4:8])
        in_if, out_if, pkts, octets = struct.unpack("!HHII", r[12:24])
        sport, dport = struct.unpack("!HH", r[32:36])
        out.append({"src": src, "dst": dst, "sport": sport, "dport": dport, "proto": r[38], "in_if": in_if, "out_if": out_if,
                    "bytes": octets * sampling, "packets": pkts * sampling})
    return out


class TemplateCache:
    def __init__(self):
        self.templates: dict[tuple, list[tuple[int, int, str | None]]] = {}

    def parse(self, data: bytes, exporter: str) -> list[dict]:
        version = struct.unpack("!H", data[0:2])[0]
        if version == 9:
            domain, pos, tmpl_id, opt_id = struct.unpack("!I", data[16:20])[0], 20, 0, 1
        elif version == 10:
            domain, pos, tmpl_id, opt_id = struct.unpack("!I", data[12:16])[0], 16, 2, 3
        else:
            return []
        end = len(data) if version == 9 else min(len(data), struct.unpack("!H", data[2:4])[0])
        records = []
        while pos + 4 <= end:
            set_id, length = struct.unpack("!HH", data[pos:pos + 4])
            if length < 4:
                break
            body, pos = data[pos + 4:pos + length], pos + length
            if set_id == tmpl_id:
                self._templates(body, exporter, domain, version)
            elif set_id == opt_id:
                continue   # options templates (sampling etc.) aren't needed for the map
            elif set_id >= 256:
                records += self._data(body, self.templates.get((exporter, domain, set_id)))
        return records

    def _templates(self, body: bytes, exporter: str, domain: int, version: int) -> None:
        p = 0
        while p + 4 <= len(body):
            tid, count = struct.unpack("!HH", body[p:p + 4])
            p += 4
            fields = []
            for _ in range(count):
                ftype, flen = struct.unpack("!HH", body[p:p + 4])
                p += 4
                if version == 10 and ftype & 0x8000:   # enterprise-specific field: skip its enterprise number
                    ftype &= 0x7FFF
                    p += 4
                    fields.append((ftype, flen, None))
                else:
                    fields.append((ftype, flen, V9_FIELDS.get(ftype)))
            if tid >= 256:
                self.templates[(exporter, domain, tid)] = fields

    @staticmethod
    def _data(body: bytes, fields) -> list[dict]:
        if not fields:
            return []   # data before its template arrived: dropped until the exporter resends the template
        out, p = [], 0
        fixed = all(f[1] != 0xFFFF for f in fields)
        size = sum(f[1] for f in fields) if fixed else None
        while p < len(body) and (not fixed or p + size <= len(body)):
            rec, start = {}, p
            for _ftype, flen, name in fields:
                if flen == 0xFFFF:   # IPFIX variable length
                    flen = body[p]
                    p += 1
                    if flen == 255:
                        flen = struct.unpack("!H", body[p:p + 2])[0]
                        p += 2
                raw = body[p:p + flen]
                p += flen
                if name and raw:
                    v = _value(name, raw)
                    if name in ("bytes", "packets") and name in rec:
                        rec[name] += v or 0
                    else:
                        rec[name] = v
            if p == start or p > len(body):
                break
            rec["src"] = rec.get("src") or rec.pop("src6", None)
            rec["dst"] = rec.get("dst") or rec.pop("dst6", None)
            rec["bytes"] = (rec.get("bytes") or 0) + (rec.pop("out_bytes", 0) or 0)
            rec["packets"] = (rec.get("packets") or 0) + (rec.pop("out_packets", 0) or 0)
            if rec.get("src") and rec.get("dst"):
                out.append(rec)
        return out


# --- the collector --------------------------------------------------------------------------------

def default_settings() -> dict:
    return {"enabled": False, "port": 2055, "internal": [], "zpa_connectors": ["ZSCALERAPP001", "BSPDRPZPACON02"]}


class FlowCollector:
    def __init__(self, data_dir: Path):
        self.settings_path = data_dir / "flows.json"
        self._lock = threading.Lock()
        self._minutes: dict[int, dict[tuple, list]] = defaultdict(dict)
        self._exporters: dict[str, dict] = {}
        self._templates = TemplateCache()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._names: dict[str, str | None] = {}
        self._resolving: set[str] = set()
        self._dns = ThreadPoolExecutor(max_workers=4)
        self._nets = self._networks()
        self.error: str | None = None

    # settings
    def settings(self) -> dict:
        try:
            return {**default_settings(), **json.loads(self.settings_path.read_text())}
        except (OSError, ValueError):
            return default_settings()

    def save_settings(self, new: dict) -> dict:
        s = self.settings()
        if "enabled" in new:
            s["enabled"] = bool(new["enabled"])
        if "port" in new:
            s["port"] = max(1, min(65535, int(new["port"])))
        if "internal" in new:
            nets = []
            for n in new["internal"] or []:
                n = str(n).strip()
                if n:
                    nets.append(str(ipaddress.ip_network(n, strict=False)))   # raises ValueError on rubbish
            s["internal"] = nets
        if "zpa_connectors" in new:
            s["zpa_connectors"] = [str(x).strip() for x in new["zpa_connectors"] or [] if str(x).strip()][:20]
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2))
        tmp.replace(self.settings_path)
        self._nets = self._networks()
        self.restart()
        return s

    def _networks(self):
        return [ipaddress.ip_network(n) for n in DEFAULT_INTERNAL + self.settings().get("internal", [])]

    def is_internal(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return True
        if a.is_loopback or a.is_multicast or a.is_link_local or a.is_unspecified:
            return True
        if a.version == 6:
            return a.is_private
        return any(a in n for n in self._nets if n.version == 4)

    # listening
    def restart(self) -> None:
        self.stop()
        s = self.settings()
        if s["enabled"]:
            self.start(s["port"])

    def start(self, port: int) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.bind(("0.0.0.0", port))
            sock.settimeout(1.0)
        except OSError as e:
            self.error = f"Couldn't listen on UDP port {port}: {e}"
            return
        self.error, self._sock = None, sock
        self._thread = threading.Thread(target=self._listen, args=(sock,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        sock, self._sock = self._sock, None
        if sock:
            sock.close()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _listen(self, sock: socket.socket) -> None:
        while self._sock is sock:
            try:
                data, (addr, _port) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.ingest(data, addr)
            except Exception:  # noqa: BLE001 - a malformed packet must never stop the collector
                with self._lock:
                    self._exporters.setdefault(addr, {"packets": 0, "flows": 0, "bad": 0, "last": 0})["bad"] += 1

    # adding up
    def ingest(self, data: bytes, exporter: str, now: float | None = None) -> int:
        version = struct.unpack("!H", data[0:2])[0] if len(data) >= 2 else 0
        recs = parse_v5(data) if version == 5 else self._templates.parse(data, exporter) if version in (9, 10) else []
        minute = int((now or time.time()) // 60)
        with self._lock:
            e = self._exporters.setdefault(exporter, {"packets": 0, "flows": 0, "bad": 0, "last": 0, "version": version})
            e.update(packets=e["packets"] + 1, flows=e["flows"] + len(recs), last=now or time.time(), version=version)
            bucket = self._minutes[minute]
            for r in recs:
                key = (r["src"], r["dst"], r.get("proto", 0), r.get("dport", 0) if r.get("proto") in (6, 17) else 0,
                       r.get("in_if", 0), r.get("out_if", 0))
                if key in bucket:
                    bucket[key][0] += r["bytes"]
                    bucket[key][1] += r["packets"]
                elif len(bucket) < MAX_KEYS_PER_MINUTE:
                    bucket[key] = [r["bytes"], r["packets"], exporter]
            for old in [m for m in self._minutes if m < minute - KEEP_MINUTES]:
                del self._minutes[old]
        return len(recs)

    def name(self, ip: str) -> str | None:
        """Reverse DNS for an outside address, looked up in the background and remembered."""
        if ip in self._names:
            return self._names[ip]
        if ip not in self._resolving and len(self._resolving) < 200:
            self._resolving.add(ip)
            self._dns.submit(self._lookup, ip)
        return None

    def _lookup(self, ip: str) -> None:
        try:
            self._names[ip] = socket.gethostbyaddr(ip)[0].lower()
        except OSError:
            self._names[ip] = None
        self._resolving.discard(ip)

    @staticmethod
    def provider(hostname: str | None) -> str | None:
        h = (hostname or "").lower()
        return next((label for key, label in PROVIDERS if key in h), None)

    def summary(self, minutes: int = 5, now: float | None = None, top: int = 25) -> dict:
        """Traffic of the last few minutes: totals in/out of the network and the top outside destinations."""
        now = now or time.time()
        last = int(now // 60)
        span = range(last - minutes + 1, last + 1)
        with self._lock:
            buckets = [dict(self._minutes.get(m, {})) for m in span]
            exporters = {k: dict(v) for k, v in self._exporters.items()}
        seconds = max(60, minutes * 60)
        dests: dict[str, dict] = {}
        out_bytes = in_bytes = internal = 0
        by_iface: dict[tuple, int] = defaultdict(int)
        talkers: dict[str, int] = defaultdict(int)
        for b in buckets:
            for (src, dst, proto, dport, in_if, out_if), (nbytes, _pk, exporter) in b.items():
                si, di = self.is_internal(src), self.is_internal(dst)
                if si and di:
                    internal += nbytes
                    continue
                outside, inside, direction = (dst, src, "out") if si else (src, dst, "in")
                d = dests.setdefault(outside, {"ip": outside, "out": 0, "in": 0, "ports": defaultdict(int), "inside": defaultdict(int)})
                d[direction] += nbytes
                d["ports"][f"{_proto(proto)}/{dport}" if dport else _proto(proto)] += nbytes
                d["inside"][inside] += nbytes
                talkers[inside] += nbytes
                if direction == "out":
                    out_bytes += nbytes
                    by_iface[(exporter, out_if)] += nbytes
                else:
                    in_bytes += nbytes
                    by_iface[(exporter, in_if)] += nbytes
        rows = []
        for d in sorted(dests.values(), key=lambda d: -(d["out"] + d["in"]))[:top]:
            host = self.name(d["ip"])
            rows.append({"ip": d["ip"], "name": host, "provider": self.provider(host),
                         "out_bps": d["out"] * 8 / seconds, "in_bps": d["in"] * 8 / seconds,
                         "ports": [p for p, _ in sorted(d["ports"].items(), key=lambda x: -x[1])[:3]],
                         "inside": [ip for ip, _ in sorted(d["inside"].items(), key=lambda x: -x[1])[:3]]})
        s = self.settings()
        return {"enabled": s["enabled"], "port": s["port"], "listening": self._sock is not None, "error": self.error,
                "minutes": minutes, "exporters": exporters, "out_bps": out_bytes * 8 / seconds, "in_bps": in_bytes * 8 / seconds,
                "internal_bps": internal * 8 / seconds, "destinations": rows,
                "top_inside": [{"ip": ip, "bps": n * 8 / seconds} for ip, n in sorted(talkers.items(), key=lambda x: -x[1])[:10]],
                "by_interface": [{"exporter": e, "if_index": i, "bps": n * 8 / seconds} for (e, i), n in by_iface.items()]}


def _proto(p: int) -> str:
    return {6: "TCP", 17: "UDP", 1: "ICMP", 50: "ESP", 47: "GRE"}.get(p, f"proto {p}")


def demo_summary(now: float | None = None) -> dict:
    """Made-up outside traffic for the demo connections, rising and falling like the rest of the demo."""
    import math
    t = (now or time.time()) / 40
    rows = [("165.225.62.32", "zs-ord1.zscloud.net", "Zscaler", 42, 9, ["TCP/443"], ["10.50.241.155", "10.50.241.156"]),
            ("52.96.164.178", "outlook.office365.com", "Microsoft 365", 26, 6, ["TCP/443"], ["10.50.241.44", "10.0.16.48"]),
            ("20.190.151.7", "login.microsoftonline.com", "Microsoft", 4, 1.5, ["TCP/443"], ["10.50.241.81"]),
            ("142.250.72.110", "ord38s29-in-f14.1e100.net", "Google", 12, 2, ["TCP/443", "UDP/443"], ["10.0.16.105"]),
            ("13.107.42.14", "teams.microsoft.com", "Microsoft 365", 9, 7, ["UDP/3478", "TCP/443"], ["10.50.241.49"]),
            ("54.239.28.85", "s3.amazonaws.com", "Amazon AWS", 7, 22, ["TCP/443"], ["10.50.241.231"]),
            ("96.45.33.73", "update.fortiguard.net", "Fortinet", 3, 0.3, ["TCP/443"], ["10.0.19.250"]),
            ("104.18.32.47", "cdn.cloudflare.net", "Cloudflare", 5, 0.8, ["TCP/443"], ["10.50.241.88"]),
            ("23.215.0.136", "a23-215-0-136.deploy.akamaitechnologies.com", "Akamai", 8, 0.6, ["TCP/443", "TCP/80"], ["10.0.17.111"]),
            ("216.58.193.67", "dns.google", "Google", 0.4, 0.2, ["UDP/53"], ["10.50.241.81", "10.50.241.60"])]
    dests, out_t, in_t = [], 0.0, 0.0
    for i, (ip, name, prov, mb_in, mb_out, ports, inside) in enumerate(rows):
        w = 1 + 0.5 * math.sin(t + i * 1.7)
        dests.append({"ip": ip, "name": name, "provider": prov, "in_bps": mb_in * 1e6 * w, "out_bps": mb_out * 1e6 * w,
                      "ports": ports, "inside": inside})
        in_t, out_t = in_t + mb_in * 1e6 * w, out_t + mb_out * 1e6 * w
    return {"enabled": True, "port": 2055, "listening": True, "error": None, "minutes": 5, "demo": True,
            "exporters": {"10.0.19.250": {"packets": 1200, "flows": 48000, "bad": 0, "last": now or time.time(), "version": 9}},
            "out_bps": out_t, "in_bps": in_t, "internal_bps": 2.1e8, "destinations": dests,
            "top_inside": [{"ip": "10.50.241.155", "bps": 4e7}, {"ip": "10.50.241.44", "bps": 2.6e7}],
            "by_interface": [{"exporter": "10.0.19.250", "if_index": 1, "bps": (in_t + out_t) * 0.97},
                             {"exporter": "10.0.19.250", "if_index": 2, "bps": (in_t + out_t) * 0.03}]}
