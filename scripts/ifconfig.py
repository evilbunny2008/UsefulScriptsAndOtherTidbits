#!/usr/bin/env python3
"""
Mimic net-tools `ifconfig` output using modern Linux sources.
Run with -h for usage.

Do the following to add an alias for ifconfig in ~/.bashrc

cp -a ifconfig.py /usr/bin/
chmod 755 /usr/bin/ifconfig.py
echo -e "\nalias ifconfig='/usr/bin/ifconfig.py'" >> ~/.bashrc
source ~/.bashrc

"""

HELP_HEADER = """\
{prog} - mimic net-tools `ifconfig` output using modern Linux sources.

Data sources:
  * /sys/class/net/<if>/...   flags, mtu, MAC, type, txqueuelen, statistics
  * `ip -j addr show`          IPv4/IPv6 addresses (JSON output from iproute2)
  * Fallbacks if `ip` is missing: ioctl for the primary IPv4 address and
    /proc/net/if_inet6 for IPv6.

Usage:"""

HELP_USAGE = [
    ("", "interfaces with an IP address assigned"),
    (None, "  (IPv4, or IPv6 other than link-local fe80::)"),
    ("-a", "all interfaces"),
    ("eth0", "a single interface"),
    ("-s", "short summary table (like `ifconfig -s`)"),
    ("-h", "this help"),
]

import fcntl
import ipaddress
import json
import os
import shutil
import socket
import struct
import subprocess
import sys

SYS_NET = "/sys/class/net"

# Interface flags, in the order net-tools prints them
IFF_FLAGS = [
    (0x1, "UP"), (0x2, "BROADCAST"), (0x4, "DEBUG"), (0x8, "LOOPBACK"),
    (0x10, "POINTOPOINT"), (0x20, "NOTRAILERS"), (0x40, "RUNNING"),
    (0x80, "NOARP"), (0x100, "PROMISC"), (0x200, "ALLMULTI"),
    (0x400, "MASTER"), (0x800, "SLAVE"), (0x1000, "MULTICAST"),
    (0x2000, "PORTSEL"), (0x4000, "AUTOMEDIA"), (0x8000, "DYNAMIC"),
]
IFF_UP, IFF_RUNNING = 0x1, 0x40

# ARPHRD_* hardware types -> (keyword, description)
HW_TYPES = {
    1: ("ether", "Ethernet"),
    772: ("loop", "Local Loopback"),
    776: ("sit", "IPv6-in-IPv4"),
    778: ("unspec", "IPIP Tunnel"),
    65534: ("unspec", "UNSPEC"),
    512: ("unspec", "Point-to-Point Protocol"),
    32: ("infiniband", "InfiniBand"),
    801: ("ether", "Ethernet"),  # 802.11 (rare in practice)
}

SCOPE_IDS = {"global": (0x0, "global"), "host": (0x10, "host"),
             "link": (0x20, "link"), "site": (0x40, "site"),
             "universe": (0x0, "global")}


# ---------------------------------------------------------------- helpers

def read_sys(iface, name, default=None):
    try:
        with open(os.path.join(SYS_NET, iface, name)) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(iface, name, default=0):
    val = read_sys(iface, name)
    if val is None:
        return default
    try:
        return int(val, 0)
    except ValueError:
        return default


def human_bytes(n):
    """net-tools style: decimal units, one decimal place."""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1000 or unit == "PB":
            return f"{n:.1f} {unit}"
        n /= 1000.0


def list_interfaces():
    try:
        names = os.listdir(SYS_NET)
    except OSError:
        sys.exit(f"cannot read {SYS_NET}")
    # Order by ifindex, like ip/ifconfig do
    return sorted(names, key=lambda n: (read_int(n, "ifindex", 1 << 30), n))


def get_flags(iface):
    flags = read_int(iface, "flags")
    # RUNNING isn't in the sysfs flags word; the kernel derives it from carrier
    if flags & IFF_UP and read_sys(iface, "carrier") == "1":
        flags |= IFF_RUNNING
    return flags


def flag_string(flags):
    names = [name for bit, name in IFF_FLAGS if flags & bit]
    return f"flags={flags}<{','.join(names)}>"


# ---------------------------------------------------------------- addresses

def addrs_from_ip():
    """Return {ifname: [addr_info dicts]} from `ip -j addr`, or None."""
    if not shutil.which("ip"):
        return None
    try:
        out = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True,
                             text=True, check=True).stdout
        data = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    return {d["ifname"]: d.get("addr_info", []) for d in data}


def addrs_fallback(iface):
    """Primary IPv4 via ioctl + IPv6 from /proc/net/if_inet6."""
    result = []
    SIOCGIFADDR, SIOCGIFNETMASK, SIOCGIFBRDADDR, SIOCGIFDSTADDR = \
        0x8915, 0x891b, 0x8919, 0x8917
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    req = struct.pack("256s", iface.encode()[:15])

    def ioctl_ip(code):
        try:
            return socket.inet_ntoa(fcntl.ioctl(s.fileno(), code, req)[20:24])
        except OSError:
            return None

    addr = ioctl_ip(SIOCGIFADDR)
    if addr:
        mask = ioctl_ip(SIOCGIFNETMASK) or "255.255.255.255"
        info = {"family": "inet", "local": addr,
                "prefixlen": ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen}
        flags = get_flags(iface)
        if flags & 0x2:
            info["broadcast"] = ioctl_ip(SIOCGIFBRDADDR)
        if flags & 0x10:
            info["address"] = ioctl_ip(SIOCGIFDSTADDR)
        result.append(info)
    s.close()

    scope_map = {0x00: "global", 0x10: "host", 0x20: "link", 0x40: "site"}
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                hexaddr, _idx, plen, scope, _fl, name = line.split()
                if name != iface:
                    continue
                ip6 = ipaddress.IPv6Address(bytes.fromhex(hexaddr))
                result.append({"family": "inet6", "local": str(ip6),
                               "prefixlen": int(plen, 16),
                               "scope": scope_map.get(int(scope, 16), "global")})
    except OSError:
        pass
    return result


# ---------------------------------------------------------------- output

def stats(iface):
    def st(name):
        return read_int(iface, f"statistics/{name}")
    return {k: st(k) for k in (
        "rx_packets", "rx_bytes", "rx_errors", "rx_dropped",
        "rx_fifo_errors", "rx_frame_errors",
        "tx_packets", "tx_bytes", "tx_errors", "tx_dropped",
        "tx_fifo_errors", "tx_carrier_errors", "collisions")}


def ip_sort_key(a):
    """Sort addresses numerically (192.168.1.9 before 192.168.1.10)."""
    try:
        ip = ipaddress.ip_address(a.get("local", ""))
        return (ip.version, int(ip))
    except ValueError:
        return (99, 0)  # unparsable: keep at the end


def has_ip(addr_info):
    """True if the interface has an IPv4 or a non-link-local IPv6 address.
    IPv6 link-local (fe80::/10) is ignored because the kernel assigns one to
    almost every interface automatically (veths, bridges, tunnels...)."""
    for a in addr_info:
        if a.get("family") == "inet":
            return True
        if a.get("family") == "inet6" and a.get("scope") != "link":
            return True
    return False


def format_iface(iface, addr_info):
    addr_info = sorted(addr_info, key=ip_sort_key)
    flags = get_flags(iface)
    mtu = read_int(iface, "mtu")
    lines = [f"{iface}: {flag_string(flags)}  mtu {mtu}"]
    pad = " " * 8

    for a in addr_info:
        if a.get("family") == "inet":
            plen = a.get("prefixlen", 32)
            mask = str(ipaddress.IPv4Network(f"0.0.0.0/{plen}").netmask)
            line = f"{pad}inet {a['local']}  netmask {mask}"
            if a.get("broadcast"):
                line += f"  broadcast {a['broadcast']}"
            if a.get("address") and a.get("address") != a.get("local"):
                line += f"  destination {a['address']}"
            lines.append(line)

    for a in addr_info:
        if a.get("family") == "inet6":
            sid, sname = SCOPE_IDS.get(a.get("scope", "global"), (0x0, "global"))
            lines.append(f"{pad}inet6 {a['local']}  prefixlen "
                         f"{a.get('prefixlen', 128)}  scopeid {sid:#x}<{sname}>")

    hwtype = read_int(iface, "type")
    kw, desc = HW_TYPES.get(hwtype, ("unspec", f"UNSPEC type {hwtype}"))
    txq = read_int(iface, "tx_queue_len")
    mac = read_sys(iface, "address", "")
    if kw == "loop":
        lines.append(f"{pad}loop  txqueuelen {txq}  ({desc})")
    elif kw == "unspec":
        lines.append(f"{pad}unspec {'-'.join(['00'] * 16)}  "
                     f"txqueuelen {txq}  ({desc})")
    else:
        lines.append(f"{pad}{kw} {mac}  txqueuelen {txq}  ({desc})")

    s = stats(iface)
    lines.append(f"{pad}RX packets {s['rx_packets']}  bytes {s['rx_bytes']} "
                 f"({human_bytes(s['rx_bytes'])})")
    lines.append(f"{pad}RX errors {s['rx_errors']}  dropped {s['rx_dropped']}  "
                 f"overruns {s['rx_fifo_errors']}  frame {s['rx_frame_errors']}")
    lines.append(f"{pad}TX packets {s['tx_packets']}  bytes {s['tx_bytes']} "
                 f"({human_bytes(s['tx_bytes'])})")
    lines.append(f"{pad}TX errors {s['tx_errors']}  dropped {s['tx_dropped']} "
                 f"overruns {s['tx_fifo_errors']}  carrier "
                 f"{s['tx_carrier_errors']}  collisions {s['collisions']}")
    return "\n".join(lines) + "\n"


def short_table(ifaces):
    print(f"{'Iface':<16}{'MTU':>6} {'RX-OK':>8} {'RX-ERR':>6} {'RX-DRP':>6} "
          f"{'RX-OVR':>6} {'TX-OK':>8} {'TX-ERR':>6} {'TX-DRP':>6} "
          f"{'TX-OVR':>6} Flg")
    for i in ifaces:
        s = stats(i)
        f = get_flags(i)
        flg = "".join(c for bit, c in ((0x2, "B"), (0x8, "L"), (0x1000, "M"),
                                        (0x100, "P"), (0x80, "O"), (0x40, "R"),
                                        (0x1, "U")) if f & bit)
        print(f"{i:<16}{read_int(i, 'mtu'):>6} {s['rx_packets']:>8} "
              f"{s['rx_errors']:>6} {s['rx_dropped']:>6} {s['rx_fifo_errors']:>6} "
              f"{s['tx_packets']:>8} {s['tx_errors']:>6} {s['tx_dropped']:>6} "
              f"{s['tx_fifo_errors']:>6} {flg}")


def print_help():
    prog = sys.argv[0]  # the name/path the script was invoked as
    cmds = [f"{prog} {arg}".rstrip() if arg is not None else ""
            for arg, _ in HELP_USAGE]
    width = max(len(c) for c in cmds) + 2
    print(HELP_HEADER.format(prog=prog))
    for cmd, (_, desc) in zip(cmds, HELP_USAGE):
        print(f"  {cmd:<{width}}# {desc}")


def main(argv):
    show_all = short = False
    names = []
    for arg in argv:
        if arg in ("-a", "--all"):
            show_all = True
        elif arg in ("-s", "--short"):
            short = True
        elif arg in ("-h", "--help"):
            print_help()
            return 0
        elif arg.startswith("-"):
            print(f"{sys.argv[0]}: unknown option {arg}\n"
                  f"Try '{sys.argv[0]} -h' for help.", file=sys.stderr)
            return 1
        else:
            names.append(arg)

    if names:
        for n in names:
            if not os.path.isdir(os.path.join(SYS_NET, n)):
                print(f"{n}: error fetching interface information: "
                      f"Device not found", file=sys.stderr)
                return 1
    ip_addrs = addrs_from_ip()

    def addrs_for(i):
        return ip_addrs.get(i, []) if ip_addrs is not None else addrs_fallback(i)

    if names:
        ifaces = names
    elif show_all:
        ifaces = list_interfaces()
    else:
        ifaces = [i for i in list_interfaces() if has_ip(addrs_for(i))]

    if short:
        short_table(ifaces)
        return 0

    for i in ifaces:
        print(format_iface(i, addrs_for(i)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        # Output piped into something like `head` that exited early
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
