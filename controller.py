# SDN controller for the health monitoring project (os-ken, OpenFlow 1.3).
#
# What it does:
#  1. Works as a learning switch: it learns which host is on which port
#     and installs flow rules so the switch can forward by itself.
#  2. Alert fast lane (only in "smart" mode): every UDP packet to or from
#     the alert port (5001) is put into queue 1 of the switch port.
#     Queue 1 has a guaranteed speed, so alerts don't wait behind other traffic.
#  3. Talks to the monitor program over UDP port 9999. The monitor sends
#     packets to the fake address 10.0.0.254. The switch hands those packets
#     to us, we act on them and send a reply back through the switch.
#  4. Tells the monitor when a switch port (a cable) goes down or comes back.
#
# The switch has two tables:
#   table 0 = "priority table": control channel + alert fast lane, then go to table 1
#   table 1 = "forwarding table": normal learning switch rules
#
# Run:  ./venv/bin/osken-manager controller.py
# Mode: "mode" in config.json, or HMON_MODE=simple ./venv/bin/osken-manager controller.py

import os
import sys
import time

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, arp, ipv4, udp, ether_types, in_proto

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, read_msg, make_msg  # noqa: E402

# made-up MAC address for the controller's fake IP 10.0.0.254
CTRL_MAC = "00:00:00:00:00:fe"

PRIORITY_TABLE = 0
FORWARD_TABLE = 1
ALERT_QUEUE = 1


class HealthController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = load_config()
        self.mode = os.environ.get("HMON_MODE", self.cfg.get("mode", "smart"))
        self.ctrl_ip = self.cfg["controller_ip"]
        self.ctrl_port = self.cfg["control_port"]
        self.alert_port = self.cfg["alert_port"]

        self.mac_to_port = {}   # dpid -> {mac: port}
        self.ip_to_mac = {}     # ip -> mac
        self.monitor = None     # where the monitor program is (filled on HELLO)
        self.say("controller started, mode = %s" % self.mode)

    def say(self, text):
        line = "[%s] [CONTROLLER] %s" % (time.strftime("%H:%M:%S"), text)
        print(line, flush=True)

    # ------------------------------------------------------------------
    # switch connects: install the starting rules
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_connected(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        self.mac_to_port.setdefault(dp.id, {})

        # start clean
        self.delete_flows(dp, parser.OFPMatch(), ofp.OFPTT_ALL)

        to_controller = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]

        # control channel: monitor -> controller (UDP to 10.0.0.254:9999)
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=in_proto.IPPROTO_UDP,
                                ipv4_dst=self.ctrl_ip, udp_dst=self.ctrl_port)
        self.add_flow(dp, PRIORITY_TABLE, 100, match, actions=to_controller)

        # ARP questions about 10.0.0.254 are answered by us
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP, arp_tpa=self.ctrl_ip)
        self.add_flow(dp, PRIORITY_TABLE, 100, match, actions=to_controller)

        if self.mode == "smart":
            # alert fast lane: alerts (to port 5001) and their ACKs (from port 5001)
            for field in ("udp_dst", "udp_src"):
                match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                        ip_proto=in_proto.IPPROTO_UDP,
                                        **{field: self.alert_port})
                self.add_flow(dp, PRIORITY_TABLE, 50, match,
                              actions=[parser.OFPActionSetQueue(ALERT_QUEUE)],
                              goto=FORWARD_TABLE)

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

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
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

        # 3) normal learning switch
        out_port = self.mac_to_port[dp.id].get(eth.dst, ofp.OFPP_FLOOD)
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
        msg = read_msg(bytes(payload))
        if msg is None:
            self.say("ignored a broken control message from %s" % ip_pkt.src)
            return

        # remember where the monitor is, so we can send it events later
        self.monitor = (dp, in_port, eth.src, ip_pkt.src, udp_pkt.src_port)
        kind = msg["type"]
        target = msg.get("target", "")
        target_ip = msg.get("target_ip", "")

        if kind == "HELLO":
            text = "hello monitor, mode=%s" % self.mode
        elif kind == "NODE_DOWN":
            n = self.remove_host_flows(dp, target_ip)
            text = "%s is down, removed its %d flow rule(s)" % (target, n)
        elif kind == "NODE_UP":
            text = "%s is back, its rules will be learned again" % target
        else:
            text = "unknown request %s" % kind

        self.say("monitor says %s %s -> %s" % (kind, target, text))
        self.send_to_monitor("CTRL_REPLY", text, reply_to=msg["seq"], target=target,
                             mode=self.mode)

    def send_to_monitor(self, kind, text, **fields):
        if self.monitor is None:
            return
        dp, port, mac, ip, udp_port = self.monitor
        data = make_msg(kind, "controller", 0, text=text, **fields)

        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(dst=mac, src=CTRL_MAC,
                                           ethertype=ether_types.ETH_TYPE_IP))
        pkt.add_protocol(ipv4.ipv4(dst=ip, src=self.ctrl_ip, proto=in_proto.IPPROTO_UDP))
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
    # a cable (switch port) went down or came back
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_changed(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        port = msg.desc.port_no
        name = msg.desc.name.decode(errors="ignore") if isinstance(msg.desc.name, bytes) else msg.desc.name

        if msg.reason == ofp.OFPPR_DELETE or msg.desc.state & ofp.OFPPS_LINK_DOWN:
            text = "cable on switch port %s (%s) is DOWN" % (port, name)
            # forget hosts behind this port and their rules
            for mac, p in list(self.mac_to_port.get(dp.id, {}).items()):
                if p == port:
                    self.delete_flows(dp, dp.ofproto_parser.OFPMatch(eth_dst=mac), FORWARD_TABLE)
                    del self.mac_to_port[dp.id][mac]
        elif msg.reason == ofp.OFPPR_ADD or not msg.desc.state & ofp.OFPPS_LINK_DOWN:
            text = "cable on switch port %s (%s) is UP" % (port, name)
        else:
            return
        self.say(text)
        self.send_to_monitor("NET_EVENT", text, target=name)

    # ------------------------------------------------------------------
    # small helpers for flow rules
    # ------------------------------------------------------------------

    def add_flow(self, dp, table, priority, match, actions=None, goto=None, idle_timeout=0):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = []
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
