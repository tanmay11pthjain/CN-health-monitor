# SDN controller for the health monitoring project (os-ken, OpenFlow 1.3).
#
# Two modes, so we can compare them:
#
#   simple  = our baseline. A normal learning switch. Every packet is treated
#             the same. It still answers the monitor on port 9999.
#
#   smart   = our project. On top of the learning switch it does 4 things
#             by itself while the system runs:
#     1. PRIORITY  alert packets (UDP port 5001) are marked urgent (DSCP 46)
#                  inside the switch. Every switch port has two lanes and
#                  the fast lane only takes packets with that mark, so
#                  alerts never wait behind other traffic. All other
#                  packets get their mark cleared, so a host can't cheat.
#     2. WATCHING  every 2 seconds we ask the switch how many bytes came in
#                  on each port and work out the speed of every host.
#     3. SLOWING   a host that sends faster than "slow_above_mbps" (two
#                  checks in a row) gets an OpenFlow meter that limits it to
#                  "slow_rate_mbps". When it stays under "calm_below_mbps"
#                  for three checks the limit is removed again (recovery).
#     4. BLOCKING  when the monitor asks (a host floods it with packets) we
#                  add a "drop" rule for that host. The rule removes itself
#                  after "block_seconds" and we tell the monitor (recovery).
#
# Talking to the monitor: the monitor sends UDP packets to the fake address
# 10.0.0.254:9999. The switch hands those packets to us, we act on them and
# send a reply back through the switch. We also tell the monitor about
# network events (cable down, host slowed down, host unblocked).
#
# The switch has two tables:
#   table 0 = "priority table": control channel, blocking, alert marking,
#             slowing down, then go to table 1
#   table 1 = "forwarding table": normal learning switch rules
#
# Run:  ./venv/bin/osken-manager controller.py
# Mode: "mode" in config.json, or HMON_MODE=simple ./venv/bin/osken-manager controller.py

import ipaddress
import os
import sys
import time

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER,
                                       set_ev_cls)
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, arp, ipv4, udp, ether_types, in_proto

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, read_msg, make_msg, ConfigError, is_number  # noqa: E402

# made-up MAC address for the controller's fake IP 10.0.0.254
CTRL_MAC = "00:00:00:00:00:fe"

PRIORITY_TABLE = 0
FORWARD_TABLE = 1

URGENT_DSCP = 46          # "EF" = expedited forwarding. topo.py's fast lane takes these.

# rule priorities inside table 0
P_CONTROL = 100
P_BLOCK = 90
P_ALERT = 50
P_SLOW_IP = 41
P_SLOW = 40
P_CLEAR_MARK = 1

BLOCK_COOKIE = 0xB1 << 32   # marks our block rules, so we know them when they expire


class HealthController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.cfg = load_config()
        except ConfigError as e:
            print("ERROR: %s" % e, flush=True)
            raise SystemExit(2)
        self.mode = os.environ.get("HMON_MODE", self.cfg["mode"]).strip().lower()
        if self.mode not in ("simple", "smart"):
            print("ERROR: HMON_MODE must be simple or smart, not %r" % self.mode, flush=True)
            raise SystemExit(2)
        self.ctrl_ip = self.cfg["controller_ip"]
        self.ctrl_port = self.cfg["control_port"]
        self.alert_port = self.cfg["alert_port"]
        self.monitor_ip = self.cfg["monitor_ip"]

        self.datapaths = {}     # dpid -> datapath (connected switches)
        self.mac_to_port = {}   # dpid -> {mac: port}
        self.ip_to_mac = {}     # ip -> mac
        self.monitor = None     # where the monitor program is (filled on its first message)

        # traffic watching
        self.last_bytes = {}    # (dpid, port) -> (rx bytes, time)
        self.speed = {}         # (dpid, port) -> Mbit/s in the last check
        self.over = {}          # (dpid, port) -> checks in a row above the limit
        self.calm = {}          # (dpid, port) -> checks in a row under the calm level
        self.slowed = {}        # (dpid, port) -> time we slowed it down
        self.blocked = {}       # ip -> time it was blocked
        self.port_down = {}     # (dpid, port) -> True if the cable is unplugged

        self.say("controller started, mode = %s" % self.mode)
        if self.mode == "smart":
            self.say("traffic watching every %.1f s: slow a host above %.1f Mbit/s to %.1f Mbit/s"
                     % (self.cfg["watch_every"], self.cfg["slow_above_mbps"],
                        self.cfg["slow_rate_mbps"]))
            self.watcher = hub.spawn(self.watch_loop)

    def say(self, text):
        line = "[%s] [CONTROLLER] %s" % (time.strftime("%H:%M:%S"), text)
        print(line, flush=True)

    # ------------------------------------------------------------------
    # switch connects / disconnects
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_changed(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER and dp.id is not None:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.mac_to_port.pop(dp.id, None)
            for d in (self.last_bytes, self.speed, self.over, self.calm, self.slowed,
                      self.port_down):
                for key in [k for k in d if k[0] == dp.id]:
                    del d[key]
            if self.monitor and self.monitor[0] is dp:
                self.monitor = None
            self.say("switch s%d disconnected" % dp.id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_connected(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        self.mac_to_port[dp.id] = {}

        # start clean: no old rules, no old meters
        self.delete_flows(dp, parser.OFPMatch(), ofp.OFPTT_ALL)
        dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE, flags=0,
                                       meter_id=ofp.OFPM_ALL, bands=[]))
        for d in (self.last_bytes, self.speed, self.over, self.calm, self.slowed,
                  self.port_down):
            for key in [k for k in d if k[0] == dp.id]:
                del d[key]
        self.blocked.clear()        # the block rules were just deleted too

        to_controller = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]

        # control channel: monitor -> controller (UDP to 10.0.0.254:9999)
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=in_proto.IPPROTO_UDP,
                                ipv4_dst=self.ctrl_ip, udp_dst=self.ctrl_port)
        self.add_flow(dp, PRIORITY_TABLE, P_CONTROL, match, actions=to_controller)

        # ARP questions about 10.0.0.254 are answered by us
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP, arp_tpa=self.ctrl_ip)
        self.add_flow(dp, PRIORITY_TABLE, P_CONTROL, match, actions=to_controller)

        if self.mode == "smart":
            # 1. alert fast lane: alerts (to port 5001) and their ACKs (from port 5001)
            #    get the urgent mark. The switch port's fast lane takes them.
            for field in ("udp_dst", "udp_src"):
                match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                        ip_proto=in_proto.IPPROTO_UDP,
                                        **{field: self.alert_port})
                self.add_flow(dp, PRIORITY_TABLE, P_ALERT, match,
                              actions=[parser.OFPActionSetField(ip_dscp=URGENT_DSCP)],
                              goto=FORWARD_TABLE)
            # every other IP packet: clear the mark, so no host can use the fast lane itself
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP)
            self.add_flow(dp, PRIORITY_TABLE, P_CLEAR_MARK, match,
                          actions=[parser.OFPActionSetField(ip_dscp=0)], goto=FORWARD_TABLE)

        # everything else: go on to the forwarding table
        self.add_flow(dp, PRIORITY_TABLE, 0, parser.OFPMatch(), goto=FORWARD_TABLE)

        # forwarding table miss: ask the controller
        self.add_flow(dp, FORWARD_TABLE, 0, parser.OFPMatch(), actions=to_controller)

        self.say("switch s%d connected, starting rules installed" % dp.id)

    # ------------------------------------------------------------------
    # packets the switch does not know what to do with
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.match["in_port"]

        try:
            pkt = packet.Packet(msg.data)
        except Exception:      # a broken packet must never stop the controller
            return
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return

        # learn where this MAC (and IP) lives
        self.mac_to_port.setdefault(dp.id, {})[eth.src] = in_port
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if arp_pkt:
            self.ip_to_mac[arp_pkt.src_ip] = arp_pkt.src_mac
        elif ip_pkt:
            self.ip_to_mac[ip_pkt.src] = eth.src

        # 1) ARP for the controller's fake address
        if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == self.ctrl_ip:
            self.answer_arp(dp, in_port, arp_pkt)
            return

        # 2) a message from the monitor program
        udp_pkt = pkt.get_protocol(udp.udp)
        if ip_pkt and udp_pkt and ip_pkt.dst == self.ctrl_ip and udp_pkt.dst_port == self.ctrl_port:
            self.handle_monitor_msg(dp, in_port, eth, ip_pkt, udp_pkt, pkt.protocols[-1])
            return
        if ip_pkt and ip_pkt.dst == self.ctrl_ip:
            return   # something else for our fake address: ignore it

        # 3) normal learning switch
        out_port = self.mac_to_port[dp.id].get(eth.dst, ofp.OFPP_FLOOD)
        if out_port == in_port:
            return   # never send a packet back where it came from
        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=eth.src, eth_dst=eth.dst)
            self.add_flow(dp, FORWARD_TABLE, 10, match, actions=actions, idle_timeout=60)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))

    # ------------------------------------------------------------------
    # talking to the monitor program
    # ------------------------------------------------------------------

    def handle_monitor_msg(self, dp, in_port, eth, ip_pkt, udp_pkt, payload):
        if not isinstance(payload, (bytes, bytearray)):
            return
        if ip_pkt.src != self.monitor_ip:
            # only the real monitor may give us orders (for example "block h3")
            self.say("ignored a control message from %s (not the monitor)" % ip_pkt.src)
            return
        msg = read_msg(bytes(payload))
        if msg is None:
            self.say("ignored a broken control message from %s" % ip_pkt.src)
            return

        # remember where the monitor is, so we can send it events later
        self.monitor = (dp, in_port, eth.src, ip_pkt.src, udp_pkt.src_port)
        kind = msg["type"]
        target = str(msg.get("target", ""))[:32]
        target_ip = msg.get("target_ip", "")
        if not isinstance(target_ip, str):
            target_ip = ""
        extra = {}

        if kind == "HELLO":
            text = "hello monitor, mode=%s" % self.mode
        elif kind == "NODE_DOWN":
            n = self.remove_host_flows(dp, target_ip)
            text = "%s is down, removed its %d flow rule(s)" % (target, n)
        elif kind == "NODE_UP":
            text = "%s is back, its rules will be learned again" % target
        elif kind == "BLOCK":
            text, extra = self.block_host(dp, target, target_ip, msg.get("seconds"))
        else:
            text = "unknown request %s" % kind[:20]

        self.say("monitor says %s %s -> %s" % (kind[:20], target, text))
        self.send_to_monitor("CTRL_REPLY", text, reply_to=msg["seq"], target=target,
                             target_ip=target_ip, mode=self.mode, **extra)

    def send_to_monitor(self, kind, text, **fields):
        if self.monitor is None:
            return
        dp, port, mac, ip, udp_port = self.monitor
        try:
            data = make_msg(kind, "controller", 0, text=text, **fields)
        except ValueError:
            return
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(dst=mac, src=CTRL_MAC,
                                           ethertype=ether_types.ETH_TYPE_IP))
        # smart mode: our own messages use the fast lane too, so the monitor still
        # hears from us when its cable is full (for example "h6 slowed down")
        tos = URGENT_DSCP << 2 if self.mode == "smart" else 0
        pkt.add_protocol(ipv4.ipv4(dst=ip, src=self.ctrl_ip, proto=in_proto.IPPROTO_UDP, tos=tos))
        pkt.add_protocol(udp.udp(src_port=self.ctrl_port, dst_port=udp_port))
        pkt.add_protocol(data)
        self.send_packet(dp, port, pkt)

    def answer_arp(self, dp, port, req):
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(dst=req.src_mac, src=CTRL_MAC,
                                           ethertype=ether_types.ETH_TYPE_ARP))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=CTRL_MAC, src_ip=self.ctrl_ip,
                                 dst_mac=req.src_mac, dst_ip=req.src_ip))
        self.send_packet(dp, port, pkt)

    def send_packet(self, dp, port, pkt):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        pkt.serialize()
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=ofp.OFPP_CONTROLLER,
                                        actions=[parser.OFPActionOutput(port)],
                                        data=pkt.data))

    def remove_host_flows(self, dp, ip):
        """Delete forwarding rules to and from a host that went down."""
        mac = self.ip_to_mac.get(ip)
        if mac is None:
            return 0
        parser = dp.ofproto_parser
        count = 0
        for match in (parser.OFPMatch(eth_dst=mac), parser.OFPMatch(eth_src=mac)):
            self.delete_flows(dp, match, FORWARD_TABLE)
            count += 1
        for table in self.mac_to_port.values():
            table.pop(mac, None)
        return count

    # ------------------------------------------------------------------
    # 4. blocking a flooding host (the monitor asks for it)
    # ------------------------------------------------------------------

    def block_host(self, dp, name, ip, seconds):
        if self.mode != "smart":
            return ("simple mode: blocking is off, %s keeps flooding" % name,
                    {"action": "block_refused"})
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            return "can't block %r: not an IP address" % ip[:20], {"action": "block_refused"}
        if ip in (self.monitor_ip, self.ctrl_ip) or addr.is_multicast \
                or addr.is_unspecified or str(addr).endswith(".255"):
            return "refused to block %s (%s)" % (name, ip), {"action": "block_refused"}
        if not is_number(seconds) or not 1 <= seconds <= 3600:
            seconds = self.cfg["block_seconds"]
        seconds = int(seconds)

        parser = dp.ofproto_parser
        ofp = dp.ofproto
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=ip)
        # no actions = drop. hard_timeout = remove itself later, and tell us then.
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=PRIORITY_TABLE, priority=P_BLOCK,
                                      match=match, instructions=[],
                                      hard_timeout=seconds, cookie=BLOCK_COOKIE | int(addr),
                                      flags=ofp.OFPFF_SEND_FLOW_REM))
        again = ip in self.blocked
        self.blocked[ip] = time.time()
        return ("%s %s (%s) for %d s, its packets are dropped in the switch"
                % ("still blocking" if again else "BLOCKED", name, ip, seconds),
                {"action": "blocked"})

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed(self, ev):
        msg = ev.msg
        if msg.cookie & (0xFFFFFFFF << 32) != BLOCK_COOKIE:
            return
        if msg.reason != msg.datapath.ofproto.OFPRR_HARD_TIMEOUT:
            return   # deleted for another reason (for example the switch restarted)
        ip = str(ipaddress.IPv4Address(msg.cookie & 0xFFFFFFFF))
        self.blocked.pop(ip, None)
        text = "%s is unblocked after %d s" % (ip, msg.duration_sec)
        self.say(text)
        self.send_to_monitor("NET_EVENT", text, target=ip, target_ip=ip, action="unblocked")

    # ------------------------------------------------------------------
    # 2 + 3. watching traffic and slowing down a noisy host
    # ------------------------------------------------------------------

    def watch_loop(self):
        while True:
            for dp in list(self.datapaths.values()):
                try:
                    parser = dp.ofproto_parser
                    dp.send_msg(parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
                except Exception as e:     # switch just went away: try next time
                    self.say("could not ask s%s for port stats: %s" % (dp.id, e))
            hub.sleep(self.cfg["watch_every"])

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats(self, ev):
        dp = ev.msg.datapath
        now = time.time()
        for st in ev.msg.body:
            port = st.port_no
            if port > dp.ofproto.OFPP_MAX:
                continue                   # the switch's own internal port
            key = (dp.id, port)
            old = self.last_bytes.get(key)
            self.last_bytes[key] = (st.rx_bytes, now)
            if old is None or now <= old[1] or st.rx_bytes < old[0]:
                continue                   # first look, or the counter was reset
            mbps = (st.rx_bytes - old[0]) * 8 / 1e6 / (now - old[1])
            self.speed[key] = mbps
            self.check_speed(dp, port, mbps)

    def check_speed(self, dp, port, mbps):
        key = (dp.id, port)
        if key not in self.slowed:
            if mbps > self.cfg["slow_above_mbps"] and not self.is_monitor_port(dp.id, port):
                self.over[key] = self.over.get(key, 0) + 1
                if self.over[key] >= 2:          # two checks in a row, not just a short burst
                    self.over[key] = 0
                    self.slow_down(dp, port, self.host_on_port(dp.id, port), mbps)
            else:
                self.over[key] = 0
        else:
            if mbps < self.cfg["calm_below_mbps"]:
                self.calm[key] = self.calm.get(key, 0) + 1
                if self.calm[key] >= 3:
                    self.calm[key] = 0
                    self.remove_limit(dp, port, self.host_on_port(dp.id, port), mbps)
            else:
                self.calm[key] = 0

    def slow_down(self, dp, port, who, mbps):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        kbps = int(self.cfg["slow_rate_mbps"] * 1000)
        meter_id = port                     # one meter per switch port
        band = parser.OFPMeterBandDrop(rate=kbps, burst_size=max(kbps // 4, 50))
        dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD,
                                       flags=ofp.OFPMF_KBPS | ofp.OFPMF_BURST,
                                       meter_id=meter_id, bands=[band]))
        # IP packets: limit them and clear their mark. Other packets (ARP): only limit.
        meter = parser.OFPInstructionMeter(meter_id)
        self.add_flow(dp, PRIORITY_TABLE, P_SLOW_IP,
                      parser.OFPMatch(in_port=port, eth_type=ether_types.ETH_TYPE_IP),
                      actions=[parser.OFPActionSetField(ip_dscp=0)], goto=FORWARD_TABLE,
                      first=meter)
        self.add_flow(dp, PRIORITY_TABLE, P_SLOW, parser.OFPMatch(in_port=port),
                      goto=FORWARD_TABLE, first=meter)
        self.slowed[(dp.id, port)] = time.time()
        self.calm[(dp.id, port)] = 0
        text = "%s sends %.1f Mbit/s, SLOWED DOWN to %.1f Mbit/s" % (
            who, mbps, self.cfg["slow_rate_mbps"])
        self.say(text)
        self.send_to_monitor("NET_EVENT", text, target=who, action="slowed")

    def remove_limit(self, dp, port, who, mbps):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        for prio, match in ((P_SLOW_IP, parser.OFPMatch(in_port=port,
                                                        eth_type=ether_types.ETH_TYPE_IP)),
                            (P_SLOW, parser.OFPMatch(in_port=port))):
            dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=PRIORITY_TABLE, priority=prio,
                                          command=ofp.OFPFC_DELETE_STRICT,
                                          out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                          match=match))
        dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE, flags=0,
                                       meter_id=port, bands=[]))
        since = time.time() - self.slowed.pop((dp.id, port), time.time())
        text = "%s is calm now (%.2f Mbit/s), speed limit REMOVED after %.0f s" % (
            who, mbps, since)
        self.say(text)
        self.send_to_monitor("NET_EVENT", text, target=who, action="unslowed")

    def host_on_port(self, dpid, port):
        """Name a switch port by the host behind it, for example 'h6 (10.0.0.6)'."""
        macs = [m for m, p in self.mac_to_port.get(dpid, {}).items() if p == port]
        for ip, mac in self.ip_to_mac.items():
            if mac in macs:
                last = ip.rsplit(".", 1)[-1]
                return "h%s (%s)" % (last, ip)
        return "port %d" % port

    def is_monitor_port(self, dpid, port):
        mac = self.ip_to_mac.get(self.monitor_ip)
        return mac is not None and self.mac_to_port.get(dpid, {}).get(mac) == port

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def switch_error(self, ev):
        msg = ev.msg
        self.say("switch reported an error: type=%d code=%d" % (msg.type, msg.code))
        if msg.type == msg.datapath.ofproto.OFPET_METER_MOD_FAILED:
            self.send_to_monitor("NET_EVENT", "switch does not support meters, "
                                 "can't slow hosts down", action="meter_error")

    # ------------------------------------------------------------------
    # a cable (switch port) went down or came back
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_changed(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        port = msg.desc.port_no
        name = msg.desc.name.decode(errors="ignore") if isinstance(msg.desc.name, bytes) \
            else str(msg.desc.name)
        who = self.host_on_port(dp.id, port)
        down = msg.reason == ofp.OFPPR_DELETE or bool(msg.desc.state & ofp.OFPPS_LINK_DOWN)

        # the switch can send several messages for one change: only report real changes
        key = (dp.id, port)
        if self.port_down.get(key) == down or (key not in self.port_down and not down):
            self.port_down[key] = down
            return
        self.port_down[key] = down

        if down:
            text = "cable on switch port %s (%s, %s) is DOWN" % (port, name, who)
            # forget hosts behind this port and their rules
            for mac, p in list(self.mac_to_port.get(dp.id, {}).items()):
                if p == port:
                    self.delete_flows(dp, dp.ofproto_parser.OFPMatch(eth_dst=mac), FORWARD_TABLE)
                    del self.mac_to_port[dp.id][mac]
            self.last_bytes.pop((dp.id, port), None)
        else:
            text = "cable on switch port %s (%s) is UP" % (port, name)
        self.say(text)
        self.send_to_monitor("NET_EVENT", text, target=name)

    # ------------------------------------------------------------------
    # small helpers for flow rules
    # ------------------------------------------------------------------

    def add_flow(self, dp, table, priority, match, actions=None, goto=None, idle_timeout=0,
                 first=None):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = []
        if first is not None:
            inst.append(first)          # a meter must come first
        if actions:
            inst.append(parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        if goto is not None:
            inst.append(parser.OFPInstructionGotoTable(goto))
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=table, priority=priority,
                                      match=match, instructions=inst,
                                      idle_timeout=idle_timeout))

    def delete_flows(self, dp, match, table):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=table, command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=match))
