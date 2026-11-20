"""The Ecosystem map: everything from the internet to the ESXi hosts, joined up from several sources.

Columns, outside to inside:
  0 Outside      where traffic goes outside the network (NetFlow, grouped by provider), and the Zscaler cloud
  1 Internet     the FortiGates' internet (WAN) interfaces with their public addresses, and IPsec tunnels
  2 Firewall     each FortiGate
  3 Networks     the FortiGates' inside interfaces: VLANs/subnets by IP address
  4 Core         switches one step from a firewall (SolarWinds topology)
  5 Access       switches further in, and switches only the ESXi hosts report (CDP/LLDP)
  6 Hosts        ESXi hosts (vCenter, with the switch port each uplink plugs into) and the Zscaler ZPA connectors

Every link carries traffic both ways: "in" flows toward the inside (left to right), "out" toward the internet.
"""

from __future__ import annotations

import ipaddress
import re

COLUMNS = ["Outside the network", "Internet links & VPN", "Firewall", "Networks (IP)", "Core switches",
           "Access switches", "ESXi hosts & ZPA"]


def norm(name: str | None) -> str:
    """Switch names as different sources write them: 'SW-CORE-01.btc.local(FOC123)' -> 'sw-core-01'."""
    n = (name or "").split("(")[0].strip().lower()
    return n.split(".")[0] if not re.fullmatch(r"[\d.]+", n) else n


def _rate(rx=0.0, tx=0.0):
    return {"in": float(rx or 0), "out": float(tx or 0)}


FIREWALL_WORDS = ("fortinet", "fortigate", "palo alto", "sonicwall", "check point", "checkpoint", "watchguard", "asa", "firepower")
WAN_WORDS = re.compile(r"wan|internet|isp|outside|untrust|external", re.I)


def is_firewall(d: dict) -> bool:
    text = f" {d.get('vendor') or ''} {d.get('model') or ''} ".lower()
    return any(re.search(rf"\b{w}\b", text) for w in FIREWALL_WORDS) or bool(re.match(r"(fgt|fw)[-_]", (d.get("name") or "").lower()))


def _firewall_from_solarwinds(fid, k, d, ifaces, node, link, primary_wan):
    """A firewall SolarWinds monitors: its internet-facing interfaces and its inside networks (by IP)."""
    st = {"up": "ok", "down": "down", "warning": "warn"}.get(d["status"], "unknown")
    wans = [i for i in ifaces if WAN_WORDS.search(f"{i['name']} {i.get('caption') or ''}")]
    node(fid, 2, "firewall", d["name"], " · ".join(x for x in [d.get("model") or d.get("vendor"), "via SolarWinds"] if x),
         "warn" if st == "ok" and any(i["status"] == "down" for i in wans) else st, address=d.get("ip"), vendor=d.get("vendor"))
    for i in ifaces:
        down = i["status"] == "down"
        if i in wans:
            iid = f"isp:sw{k}|{d['id']}|{i['name']}"
            label = (i.get("caption") or i["name"]).split(" · ")[-1]
            node(iid, 1, "isp", label, ", ".join(i["ips"]) or i["name"], "down" if down else "ok", interface=i["name"])
            # What the firewall receives on an internet port came in from the internet.
            link(iid, fid, _rate(i["rx"], i["tx"]), i["speed"], "down" if down else "ok", label=i["name"])
            busy = (i["rx"] or 0) + (i["tx"] or 0)
            if not down and (primary_wan is None or busy > primary_wan[1]):
                primary_wan = (iid, busy)
        elif i["ips"]:
            nid = f"net:sw{k}|{d['id']}|{i['name']}"
            nets = [str(ipaddress.ip_interface(a).network) for a in i["ips"]]
            node(nid, 3, "network", (i.get("caption") or i["name"]).split(" · ")[-1], ", ".join(nets),
                 "down" if down else "ok", interface=i["name"], address=", ".join(i["ips"]))
            link(fid, nid, _rate(i["tx"], i["rx"]), i["speed"], "down" if down else "ok", label=i["name"])
    return primary_wan


def _networks_from_vcenter(vcs, nodes, node, link, sw_by_name):
    """No firewall connected: networks by IP and their gateways, from what VMware Tools reports on each VM."""
    nets: dict = {}
    for vc in vcs:
        topo, traffic = vc["topo"], vc.get("traffic") or {"vms": {}}
        pgs = {p["id"]: p for p in topo["portgroups"]}
        host_sw: dict = {}
        for u in topo["uplinks"]:
            sid = sw_by_name.get(norm(u.get("physical"))) if u.get("physical") else None
            if sid:
                host_sw.setdefault(u["host"], set()).add(sid)
        for v in topo["vms"]:
            r = traffic["vms"].get(v["name"]) or {}
            with_ip = [n for n in v["nics"] if n.get("ips")]
            for n in with_ip:
                for ip in n["ips"]:
                    try:
                        net = ipaddress.ip_interface(ip).network
                    except ValueError:
                        continue
                    if net.prefixlen == 32 or net.is_link_local:
                        continue
                    e = nets.setdefault(str(net), {"pgs": set(), "vms": set(), "gws": {}, "rx": 0.0, "tx": 0.0, "sw": {}, "hosts": {}})
                    if n.get("portgroup") in pgs:
                        p = pgs[n["portgroup"]]
                        e["pgs"].add(p["name"] + (f" (VLAN {p['vlan']})" if p.get("vlan") not in (None, "", "0") else ""))
                    if v["name"] not in e["vms"]:
                        e["vms"].add(v["name"])
                        share = 1 / len(with_ip)   # a VM on two networks: split its traffic between them
                        rx, tx = r.get("rx", 0) * share, r.get("tx", 0) * share
                        e["rx"] += rx
                        e["tx"] += tx
                        sws = host_sw.get(v.get("host")) or set()
                        for sid in sws:
                            t = e["sw"].setdefault(sid, [0.0, 0.0])
                            t[0] += rx / len(sws)
                            t[1] += tx / len(sws)
                        if not sws and v.get("host"):
                            t = e["hosts"].setdefault(v["host"], [0.0, 0.0])
                            t[0] += rx
                            t[1] += tx
                    gw = v.get("gateway")
                    if gw:
                        try:
                            if ipaddress.ip_address(gw) in net:
                                e["gws"][gw] = e["gws"].get(gw, 0) + 1
                        except ValueError:
                            pass
    if not nets:
        return
    gateways = {net: max(e["gws"], key=e["gws"].get) for net, e in nets.items() if e["gws"]}
    node("fw:gateways", 2, "firewall", "Firewall / router", f"{len(set(gateways.values()))} gateway address(es), from the VMs",
         "unknown", subnets=[f"{g} for {n}" for n, g in sorted(gateways.items())])
    link("isp:internet", "fw:gateways", None, None, "ok", label="the way out to the internet")
    for net, e in sorted(nets.items(), key=lambda kv: ipaddress.ip_network(kv[0])):
        nid = f"net:vc|{net}"
        label = ", ".join(sorted(e["pgs"])) or net
        node(nid, 3, "network", label, f"{net} · {len(e['vms'])} VM(s)", "ok",
             address=f"gateway {gateways[net]}" if net in gateways else "no gateway reported")
        link("fw:gateways", nid, _rate(e["rx"], e["tx"]), None, "ok" if net in gateways else "unknown",
             label=f"via {gateways.get(net, 'unknown gateway')} · traffic of the VMs in this network")
        for sid, (rx, tx) in e["sw"].items():
            link(nid, sid, _rate(rx, tx), None, label=f"{net} through this switch")
        for h, (rx, tx) in e["hosts"].items():
            link(nid, f"host:{h}", _rate(rx, tx), None, label=f"{net} on this host")


def build(fws: list[dict], sws: list[dict], vcs: list[dict], flows: dict | None, zpa_names: list[str]) -> dict:
    nodes: dict[str, dict] = {}
    links: list[dict] = []
    notes: list[str] = []

    def node(nid, col, kind, label, sub="", status="ok", **details):
        if nid not in nodes:
            nodes[nid] = {"id": nid, "col": col, "kind": kind, "label": label, "sub": sub, "status": status, "details": details}
        return nodes[nid]

    def link(a, b, rate=None, speed=None, status="ok", kind="wire", label=""):
        links.append({"a": a, "b": b, "rate": rate, "speed": speed, "status": status, "kind": kind, "label": label})

    # --- firewalls -------------------------------------------------------------------------
    fw_by_ip, fw_ifs, primary_wan = {}, {}, None
    for fw in fws:
        n, fid = fw["net"], f"fw:{fw['conn']}"
        wans = [i for i in n["interfaces"] if i["role"] == "wan"]
        down = [i for i in wans if i["link"] is False]
        node(fid, 2, "firewall", n["hostname"], f"{n['model']} · {n['version']}",
             "warn" if down else "ok", serial=n.get("serial"), connection=fw["conn"])
        fw_by_ip[fw.get("host", "").lower()] = fid
        fw_by_ip[n["hostname"].lower()] = fid
        gw = {r["interface"]: r["gateway"] for r in n.get("routes", []) if r.get("dest") == "0.0.0.0/0"}
        for i in n["interfaces"]:
            if i["ip"]:
                fw_by_ip[i["ip"].split("/")[0]] = fid
            fw_ifs[(fid, i["name"].lower())] = i
            r = i.get("rate") or {}
            if i["role"] == "wan":
                iid = f"isp:{fw['conn']}|{i['name']}"
                node(iid, 1, "isp", i["alias"] or i["name"], f"{i['ip'] or 'no address'}" + (f" → {gw[i['name']]}" if gw.get(i["name"]) else ""),
                     "down" if i["link"] is False else "ok", interface=i["name"], gateway=gw.get(i["name"]), speed=i.get("speed"))
                link(iid, fid, _rate(r.get("rx"), r.get("tx")) if r else None, (i.get("speed") or 0) * 1e6 or None,
                     "down" if i["link"] is False else "ok", label=i["name"])
                if i["link"] is not False and (primary_wan is None or (r.get("rx", 0) + r.get("tx", 0)) > primary_wan[1]):
                    primary_wan = (iid, r.get("rx", 0) + r.get("tx", 0))
            elif i["ip"] and i.get("type") not in ("loopback", "tunnel"):
                nid = f"net:{fw['conn']}|{i['name']}"
                node(nid, 3, "network", i["alias"] or i["name"], f"{i['network']}" + (f" · VLAN {i['vlan']}" if i.get("vlan") else ""),
                     "down" if i["link"] is False else "ok", interface=i["name"], address=i["ip"], role=i["role"])
                # Traffic the firewall sends out of an inside port goes in, toward the servers.
                link(fid, nid, _rate(r.get("tx"), r.get("rx")) if r else None, (i.get("speed") or 0) * 1e6 or None,
                     "down" if i["link"] is False else "ok", label=i["name"])
        for t in n.get("tunnels", []):
            tid = f"vpn:{fw['conn']}|{t['name']}"
            r = t.get("rate") or {}
            node(tid, 1, "vpn", t["name"], f"IPsec to {t['peer']}", "ok" if t["up"] else "down", peer=t["peer"], subnets=t["subnets"])
            link(tid, fid, _rate(r.get("rx"), r.get("tx")) if r else None, None, "ok" if t["up"] else "down", kind="tunnel")

    # --- switches (SolarWinds) ---------------------------------------------------------------
    sw_by_name = {}
    for k, sw in enumerate(sws):
        ifs = {int(i): v for i, v in sw["interfaces"].items()} if sw["interfaces"] else {}
        firewalls = {}
        for d in sw["nodes"]:
            fid = fw_by_ip.get((d.get("ip") or "").lower()) or fw_by_ip.get((d.get("name") or "").lower())
            if fid:
                firewalls[d["id"]] = fid
            elif not fws and is_firewall(d):
                # No firewall connection: draw it from what SolarWinds sees over SNMP.
                fid = firewalls[d["id"]] = f"fw:sw{k}|{d['id']}"
                primary_wan = _firewall_from_solarwinds(fid, k, d, [i for i in ifs.values() if i["node"] == d["id"]],
                                                        node, link, primary_wan)
        devices = {d["id"]: d for d in sw["nodes"] if d["category"] == "network" and d["id"] not in firewalls}
        adj: dict = {}
        for l in sw["links"]:
            adj.setdefault(l["a_node"], set()).add(l["b_node"])
            adj.setdefault(l["b_node"], set()).add(l["a_node"])
        depth, frontier = {}, [f for f in firewalls]
        for f in frontier:
            depth[f] = 0
        while frontier:
            nxt = []
            for f in frontier:
                for m in adj.get(f, ()):
                    if m not in depth:
                        depth[m] = depth[f] + 1
                        nxt.append(m)
            frontier = nxt
        for did, d in devices.items():
            sid = f"sw:{k}|{did}"
            col = 4 if depth.get(did, 1) <= 1 else 5
            node(sid, col, "switch", d["name"], " · ".join(x for x in [d.get("ip"), d.get("model")] if x),
                 {"up": "ok", "down": "down", "warning": "warn"}.get(d["status"], "unknown"), vendor=d.get("vendor"))
            sw_by_name[norm(d["name"])] = sid
        for l in sw["links"]:
            a, b = l["a_node"], l["b_node"]
            ia, ib = ifs.get(l["a_if"]), ifs.get(l["b_if"])
            if a in firewalls or b in firewalls:
                f_node, f_if, s_node, s_if = (a, ia, b, ib) if a in firewalls else (b, ib, a, ia)
                if s_node not in devices:
                    continue
                fid = firewalls[f_node]
                net = next((nid for nid in nodes if nid.startswith("net:") and "fw:" + nid[4:].rsplit("|", 1)[0] == fid
                            and f_if and nodes[nid]["details"].get("interface", "").lower() == (f_if["name"] or "").lower()), None)
                rate = _rate(s_if["rx"], s_if["tx"]) if s_if else None   # what the switch receives from the firewall goes in
                link(net or fid, f"sw:{k}|{s_node}", rate, s_if["speed"] if s_if else None,
                     "down" if s_if and s_if["status"] == "down" else "ok", label=f"{f_if['name'] if f_if else '?'} ↔ {s_if['name'] if s_if else '?'}")
            elif a in devices and b in devices:
                # Draw from the switch nearer the firewall; what it sends goes in.
                up, dn, iu, idn = (a, b, ia, ib) if depth.get(a, 9) <= depth.get(b, 9) else (b, a, ib, ia)
                rate = _rate(iu["tx"], iu["rx"]) if iu else (_rate(idn["rx"], idn["tx"]) if idn else None)
                speed = (iu or idn or {}).get("speed")
                link(f"sw:{k}|{up}", f"sw:{k}|{dn}", rate, speed, "down" if (iu and iu["status"] == "down") else "ok",
                     label=f"{iu['name'] if iu else '?'} ↔ {idn['name'] if idn else '?'}")
    if not sws:
        notes.append("Add SolarWinds (with Network Performance Monitor) to show the switches, how they connect and their traffic.")
    elif not any(sw["npm"] for sw in sws):
        notes.append("SolarWinds doesn't have Network Performance Monitor, so switch links and traffic can't be shown.")

    # --- ESXi hosts (vCenter, with CDP/LLDP) and the ZPA connectors --------------------------------
    zpa = {n.lower() for n in zpa_names}
    zpa_vms = {}
    for vc in vcs:
        topo, traffic = vc["topo"], vc.get("traffic") or {"uplinks": {}, "vms": {}}
        for h in topo["hosts"]:
            node(f"host:{h['name']}", 6, "host", h["name"].split(".")[0], f"{h['cluster']} · ESXi {h.get('version') or '?'}",
                 "ok" if (h.get("state") or "").lower() == "connected" else "down", fqdn=h["name"])
        pairs: dict = {}
        for u in topo["uplinks"]:
            if not u.get("physical"):
                continue
            sid = sw_by_name.get(norm(u["physical"]))
            if not sid:
                sid = f"cdp:{norm(u['physical'])}"
                node(sid, 5 if sws else 4, "switch", u["physical"].split("(")[0], "seen by the ESXi hosts (CDP/LLDP)", "unknown")
                sw_by_name[norm(u["physical"])] = sid
            p = pairs.setdefault((sid, u["host"]), {"rx": 0.0, "tx": 0.0, "speed": 0, "ports": [], "up": 0, "n": 0})
            r = traffic["uplinks"].get(u["id"]) or {}
            p["rx"] += r.get("rx", 0)
            p["tx"] += r.get("tx", 0)
            p["speed"] += (u.get("speed") or 0) * 1e6
            p["ports"].append(f"{u['name']}→{u.get('port') or '?'}")
            p["n"] += 1
            p["up"] += 1 if u.get("speed") else 0
        for (sid, host), p in pairs.items():
            link(sid, f"host:{host}", _rate(p["rx"], p["tx"]), p["speed"] or None,
                 "down" if not p["up"] else ("warn" if p["up"] < p["n"] else "ok"), label=", ".join(p["ports"]))
        for v in topo["vms"]:
            if v["name"].lower() in zpa:
                zpa_vms[v["name"].lower()] = (v, traffic["vms"].get(v["name"]))
    no_firewall = not any(n["kind"] == "firewall" for n in nodes.values())
    if no_firewall and vcs:
        node("isp:internet", 1, "isp", "Internet", "no firewall connected", "unknown")
        _networks_from_vcenter(vcs, nodes, node, link, sw_by_name)
    if not any(n["kind"] == "firewall" for n in nodes.values()):
        notes.append("No firewall found: add your FortiGate on the Connections page, or have SolarWinds monitor it, "
                     "to show the firewall, its internet links and networks.")
    elif "fw:gateways" in nodes:
        notes.append("Without the FortiGate or SolarWinds, the networks and gateways come from vCenter (what VMware Tools reports "
                     "on each VM), and traffic is the VMs' own traffic. A read-only FortiGate API token or SolarWinds access from "
                     "your network team would add the internet links and the switches in between.")
    elif not fws:
        notes.append("The firewall is drawn from SolarWinds. Adding the FortiGate itself (a read-only API token from your network "
                     "team) would also show its routes and VPN tunnels.")
    if not vcs:
        notes.append("Add your vCenter (and PowerCLI on the KasTec computer) to show the ESXi hosts and which switch ports they use.")

    # --- outside: NetFlow destinations, grouped; and Zscaler -----------------------------------------
    edge = primary_wan[0] if primary_wan else None
    if not edge:
        edge = "isp:internet"
        node(edge, 1, "isp", "Internet", "no firewall connected", "unknown")
    zs_rate = {"in": 0.0, "out": 0.0}
    if flows and flows.get("destinations") is not None:
        groups: dict[str, dict] = {}
        for d in flows["destinations"]:
            if d.get("provider") == "Zscaler":
                zs_rate["in"] += d["in_bps"]
                zs_rate["out"] += d["out_bps"]
                continue
            g = d.get("provider") or d.get("name") or d["ip"]
            e = groups.setdefault(g, {"in": 0.0, "out": 0.0, "dests": []})
            e["in"] += d["in_bps"]
            e["out"] += d["out_bps"]
            e["dests"].append(d)
        ranked = sorted(groups.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))
        for g, e in ranked[:8]:
            did = f"dest:{g}"
            ips = ", ".join(x["ip"] for x in e["dests"][:3])
            node(did, 0, "destination", g, ips, "ok", destinations=[{k: x[k] for k in ("ip", "name", "ports", "inside")} for x in e["dests"][:6]])
            link(did, edge, {"in": e["in"], "out": e["out"]}, None, kind="internet")
        rest = ranked[8:]
        if rest:
            node("dest:other", 0, "destination", f"{len(rest)} other destinations", "smaller flows", "ok")
            link("dest:other", edge, {"in": sum(e["in"] for _, e in rest), "out": sum(e["out"] for _, e in rest)}, None, kind="internet")
    elif flows is not None:
        notes.append("NetFlow is off: turn it on below and point the FortiGate's NetFlow at KasTec to see where traffic goes outside.")
    if (zpa and vcs) or zs_rate["in"] or zs_rate["out"]:
        node("zscaler", 0, "cloud", "Zscaler cloud", "ZPA / ZIA", "ok")
        if zs_rate["in"] or zs_rate["out"]:
            link("zscaler", edge, dict(zs_rate), None, kind="internet")
        for name in zpa_names if vcs else []:   # the connectors are VMs: they need a vCenter to be found
            found = zpa_vms.get(name.lower())
            vm, r = found if found else (None, None)
            zid = f"zpa:{name}"
            node(zid, 6, "zpa", name, f"ZPA connector on {vm['host'].split('.')[0]}" if vm and vm.get("host") else
                 ("ZPA connector" if vm else "ZPA connector (not found in vCenter)"),
                 "ok" if vm and vm.get("power") == "poweredOn" else ("down" if vm else "unknown"))
            # The connector's tunnel to Zscaler: what it receives came in from Zscaler.
            link("zscaler", zid, _rate((r or {}).get("rx"), (r or {}).get("tx")) if r else None, None,
                 "ok" if vm and vm.get("power") == "poweredOn" else "unknown", kind="tunnel", label="ZPA tunnel")

    if "fw:gateways" in nodes:   # the vCenter note already says what SolarWinds would add
        notes[:] = [n for n in notes if not n.startswith("Add SolarWinds")]
    summary = {
        "outside_in": flows.get("in_bps") if flows else None, "outside_out": flows.get("out_bps") if flows else None,
        "firewalls": sum(1 for n in nodes.values() if n["kind"] == "firewall" and n["id"] != "fw:gateways"),
        "networks": sum(1 for n in nodes.values() if n["kind"] == "network"),
        "internet_links": sum(1 for n in nodes.values() if n["kind"] == "isp" and n["id"] != "isp:internet"),
        "switches": sum(1 for n in nodes.values() if n["kind"] == "switch"),
        "hosts": sum(1 for n in nodes.values() if n["kind"] == "host"),
        "problems": sum(1 for n in nodes.values() if n["status"] == "down") + sum(1 for l in links if l["status"] == "down"),
    }
    return {"columns": COLUMNS, "nodes": list(nodes.values()), "links": links, "summary": summary, "notes": notes}
