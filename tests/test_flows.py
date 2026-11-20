"""The NetFlow/IPFIX collector: parsing each format, adding up, and listening on UDP."""

import socket
import struct
import time

from app.flows import FlowCollector, parse_v5

T0 = 1_800_000_000.0


def ip(a):
    return socket.inet_aton(a)


def v5(records, sampling=0):
    head = struct.pack("!HHIIIIBBH", 5, len(records), 0, int(T0), 0, 1, 0, 0, sampling)
    body = b""
    for src, dst, sport, dport, proto, octets, pkts in records:
        body += ip(src) + ip(dst) + ip("0.0.0.0") + struct.pack("!HHIIII", 3, 1, pkts, octets, 0, 0)
        body += struct.pack("!HHBBBBHHBBH", sport, dport, 0, 0, proto, 0, 0, 0, 24, 0, 0)
    return head + body


def v9(template_id=260, with_template=True, records=()):
    fields = [(8, 4), (12, 4), (7, 2), (11, 2), (4, 1), (1, 4), (2, 4), (10, 2), (14, 2)]
    sets = b""
    if with_template:
        t = struct.pack("!HH", template_id, len(fields)) + b"".join(struct.pack("!HH", a, b) for a, b in fields)
        sets += struct.pack("!HH", 0, 4 + len(t)) + t
    if records:
        d = b"".join(ip(s) + ip(dst) + struct.pack("!HHBIIHH", sp, dp, pr, by, pk, 3, 1) for s, dst, sp, dp, pr, by, pk in records)
        sets += struct.pack("!HH", template_id, 4 + len(d)) + d
    return struct.pack("!HHIIII", 9, 2, 0, int(T0), 1, 7) + sets


def ipfix(records):
    # Template 300: src, dst, a variable-length enterprise field, dport, proto, octetDeltaCount.
    fields = struct.pack("!HH", 8, 4) + struct.pack("!HH", 12, 4) + struct.pack("!HHI", 0x8000 | 100, 0xFFFF, 12356) \
        + struct.pack("!HH", 11, 2) + struct.pack("!HH", 4, 1) + struct.pack("!HH", 1, 8)
    tmpl = struct.pack("!HH", 300, 6) + fields
    sets = struct.pack("!HH", 2, 4 + len(tmpl)) + tmpl
    d = b""
    for src, dst, dport, proto, octets in records:
        d += ip(src) + ip(dst) + bytes([5]) + b"forti" + struct.pack("!HBQ", dport, proto, octets)
    sets += struct.pack("!HH", 300, 4 + len(d)) + d
    return struct.pack("!HHIII", 10, 16 + len(sets), int(T0), 1, 9) + sets


def test_v5_with_sampling():
    recs = parse_v5(v5([("10.50.241.20", "52.96.1.10", 50000, 443, 6, 1500, 2)], sampling=10))
    assert recs == [{"src": "10.50.241.20", "dst": "52.96.1.10", "sport": 50000, "dport": 443, "proto": 6,
                     "in_if": 3, "out_if": 1, "bytes": 15000, "packets": 20}]


def test_v9_templates_and_ipfix(tmp_path):
    c = FlowCollector(tmp_path)
    # Data that arrives before its template is dropped; after the template it's read.
    assert c.ingest(v9(with_template=False, records=[("10.0.16.5", "8.8.8.8", 1, 53, 17, 100, 1)]), "10.0.19.250", now=T0) == 0
    assert c.ingest(v9(), "10.0.19.250", now=T0) == 0
    assert c.ingest(v9(with_template=False, records=[("10.0.16.5", "8.8.8.8", 1, 53, 17, 100, 1)]), "10.0.19.250", now=T0) == 1
    assert c.ingest(ipfix([("10.50.241.155", "165.225.0.5", 443, 6, 6_000_000)]), "10.0.19.251", now=T0) == 1
    s = c.summary(minutes=1, now=T0)
    assert s["exporters"]["10.0.19.250"]["version"] == 9 and s["exporters"]["10.0.19.251"]["version"] == 10
    top = s["destinations"][0]
    assert top["ip"] == "165.225.0.5" and top["out_bps"] == 6_000_000 * 8 / 60 and top["ports"] == ["TCP/443"]
    assert top["inside"] == ["10.50.241.155"]


def test_inside_outside_and_internal_ranges(tmp_path):
    c = FlowCollector(tmp_path)
    c.ingest(v5([("10.50.241.20", "10.0.16.5", 1, 445, 6, 6000, 4),           # inside to inside
                 ("10.50.241.20", "40.97.1.1", 1, 443, 6, 60_000, 40),        # out to Microsoft-ish
                 ("40.97.1.1", "10.50.241.20", 443, 1, 6, 600_000, 400),      # and the reply
                 ("100.64.1.130", "10.50.241.60", 1, 389, 6, 1200, 10)]), "fw", now=T0)  # Zscaler CGNAT range: inside
    s = c.summary(minutes=1, now=T0)
    assert s["internal_bps"] == (6000 + 1200) * 8 / 60
    assert s["out_bps"] == 60_000 * 8 / 60 and s["in_bps"] == 600_000 * 8 / 60
    assert [d["ip"] for d in s["destinations"]] == ["40.97.1.1"]
    c.save_settings({"internal": ["40.97.0.0/16"]})                           # e.g. a partner range treated as inside
    assert c.summary(minutes=1, now=T0)["destinations"] == []
    assert FlowCollector.provider("outlook-namsouth.office365.com.akadns.net") == "Microsoft 365"
    assert FlowCollector.provider("a23-1-2-3.deploy.static.akamaitechnologies.com") == "Akamai"
    assert FlowCollector.provider("ord37s32-in-f14.1e100.net") == "Google"


def test_listens_on_udp(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = FlowCollector(tmp_path)
    c.save_settings({"enabled": True, "port": port})
    try:
        assert c.summary()["listening"]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as out:
            out.sendto(v5([("10.50.241.20", "1.1.1.1", 1, 53, 17, 500, 1)]), ("127.0.0.1", port))
            out.sendto(b"\x00\x05garbage", ("127.0.0.1", port))                 # a broken packet doesn't stop it
            out.sendto(v5([("10.50.241.20", "1.1.1.1", 1, 53, 17, 500, 1)]), ("127.0.0.1", port))
        for _ in range(50):
            if c.summary()["exporters"].get("127.0.0.1", {}).get("flows") == 2:
                break
            time.sleep(0.05)
        e = c.summary()["exporters"]["127.0.0.1"]
        assert e["flows"] == 2 and e["bad"] == 1
    finally:
        c.save_settings({"enabled": False})
    assert not c.summary()["listening"]
