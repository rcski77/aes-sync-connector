#!/usr/bin/env python3
"""
AES Sync Client Probe  —  port 17471
=====================================
Connects to the AES Scheduler sync server as a client (ClientInit),
completes the handshake, then tries common registration commands and
logs everything the server sends back. Run this while AES Scheduler
is open with the server running.

Usage:
    python aes_sync_probe.py
    python aes_sync_probe.py --host 10.0.0.101 --port 17471 --password test123
"""

import os, sys, socket, struct, hashlib, base64, gzip, zlib, time, datetime
import argparse
import xml.etree.ElementTree as ET

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
except ImportError:
    print("ERROR: pip install pycryptodome"); sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────
NCC_PUBLIC_KEY       = 0x11
NCC_SYMMETRIC_KEY    = 0x12
NCC_REQUEST_PASSWORD = 0x13
NCC_SUBMIT_PASSWORD  = 0x14
NCC_PASSWORD_VALID   = 0x15
NCC_PASSWORD_INVALID = 0x16
NCC_NO_DATA          = 0x21   # CommandWithNoData
NCC_BINARY           = 0x22   # CommandWithBinaryData
NCC_OBJECT           = 0x23   # CommandWithObjectData
NCC_STRING           = 0x24   # CommandWithStringData
NCC_BEGIN_V1         = 0xA0
NCC_CONN_INIT        = 0xFE
NCC_END              = 0xFF
DATA_COMPRESSED      = 1

# SchedulerNetCommand enum values (all candidates worth trying)
CMDS = {
    256:   "ReceivedBadCommand",
    8192:  "AuthenticationCommit",
    8208:  "AuthenticationValid",
    8224:  "AuthenticationFailed",
    8448:  "RequestEventList",
    8464:  "EventListAttached",
    8704:  "RequestEventSync",
    8720:  "EventSyncAttached",
    8736:  "EventSyncFailed",
    9472:  "RegisterServer",
    9488:  "RegisterServerSuccessful",
    9504:  "RegisterServerFailed",
    9728:  "RegisterClient",
    9744:  "RegisterClientSuccessful",
    9760:  "RegisterClientFailed",
    9761:  "RegisterClientDenied",
    9984:  "UnregisterServer",
    9985:  "UnregisterClient",
    10064: "ClientSessionTerminated",
    12288: "PlasmaDisplayModeSelected",
    12289: "KioskDisplayModeSelected",
    16400: "EventUpdateAttached",
    16640: "RemoteEntryUpdateAttached",
    16641: "ScoreKioskUpdateAttached",
    17153: "AutoPrintMatchesAttached",
    38144: "RequestServerDate",
    38145: "ServerDateAttached",
}

def cmd_name(n): return CMDS.get(n, f"Unknown({n}/0x{n:04X})")
def ts(): return datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]

# ── Cipher ─────────────────────────────────────────────────────────────────────
class CS:
    def __init__(self): self.key = b''; self.iv = b''; self.pos = 0
    def gen(self): self.key = os.urandom(64); self.iv = os.urandom(32)

def xor(data, s):
    iv, pos, out = bytearray(s.iv), s.pos, bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ iv[pos]; pos += 1
        if pos == len(iv):
            iv = bytearray(hashlib.sha256(bytes(iv) + s.key).digest()); pos = 0
    s.iv = bytes(iv); s.pos = pos
    return bytes(out)

def recv_n(sock, n):
    buf = b''
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c: raise ConnectionError("Server closed connection")
        buf += c
    return buf

def decomp(d):
    for fn in [gzip.decompress, lambda x: zlib.decompress(x, -15)]:
        try: return fn(d)
        except: pass
    raise ValueError("Cannot decompress")

# ── Wire helpers ───────────────────────────────────────────────────────────────
def raw_rcmd(s): return struct.unpack('<I', recv_n(s,1) + b'\x00\x00\x00')[0]
def raw_wcmd(s, c): s.sendall(struct.pack('<I', c)[:1])
def raw_rdata(s):
    n = struct.unpack('<I', recv_n(s,3) + b'\x00')[0]
    f = struct.unpack('<I', recv_n(s,1) + b'\x00\x00\x00')[0]
    d = recv_n(s,n) if n else b''
    return decomp(d) if f & DATA_COMPRESSED else d
def raw_wdata(s, data=None):
    if data is None: data = b''
    f = 0
    if len(data) > 4096: data = gzip.compress(data); f = DATA_COMPRESSED
    s.sendall(struct.pack('<I', len(data))[:3] + struct.pack('<I', f)[:1] + data)

class Chan:
    def __init__(self, sock, state): self.s = sock; self.st = state
    def r(self, n): return xor(recv_n(self.s, n), self.st)
    def w(self, d): self.s.sendall(xor(d, self.st))
    def rcmd(self): return struct.unpack('<I', self.r(1) + b'\x00\x00\x00')[0]
    def wcmd(self, c): self.w(struct.pack('<I', c)[:1])
    def rint(self): return struct.unpack('<I', self.r(4))[0]
    def rdata(self):
        n = struct.unpack('<I', self.r(3) + b'\x00')[0]
        f = struct.unpack('<I', self.r(1) + b'\x00\x00\x00')[0]
        d = self.r(n) if n else b''
        return decomp(d) if f & DATA_COMPRESSED else d
    def wdata(self, data=None):
        if data is None: data = b''
        f = 0
        if len(data) > 4096: data = gzip.compress(data); f = DATA_COMPRESSED
        self.w(struct.pack('<I', len(data))[:3] + struct.pack('<I', f)[:1] + data)
    def send(self, nc, cc, data=None):
        self.wcmd(nc); self.w(struct.pack('<I', cc)); self.wdata(data)

# ── RSA ────────────────────────────────────────────────────────────────────────
def to_xml(k):
    p = k.publickey()
    n64 = base64.b64encode(p.n.to_bytes((p.n.bit_length()+7)//8,'big')).decode()
    e64 = base64.b64encode(p.e.to_bytes((p.e.bit_length()+7)//8,'big')).decode()
    return f'<RSAKeyValue><Modulus>{n64}</Modulus><Exponent>{e64}</Exponent></RSAKeyValue>'

def from_xml(x):
    r = ET.fromstring(x)
    n = int.from_bytes(base64.b64decode(r.find('Modulus').text), 'big')
    e = int.from_bytes(base64.b64decode(r.find('Exponent').text), 'big')
    return RSA.construct((n, e))

def renc(k, d): return PKCS1_v1_5.new(k).encrypt(d)
def rdec(k, d):
    r = PKCS1_v1_5.new(k).decrypt(d, None)
    if r is None: raise ValueError("RSA decrypt failed")
    return r

# ── ClientInit ─────────────────────────────────────────────────────────────────
def client_init(sock, password):
    """
    Mirror of NetConnection.ClientInit — WE connect to AES.
    Phase 1: we send 0xa0, server echoes 0xa0
    Phase 2: server sends RSA public key, we send ours
    Phase 3: we send our session key (encrypted with server key)
             server sends its session key (encrypted with our key)
    Phase 4: server challenges password, we respond
    Phase 5: server confirms, we echo ConnectionInitialized
    """
    sock.settimeout(15)

    # Phase 1
    raw_wcmd(sock, NCC_BEGIN_V1)
    cmd = raw_rcmd(sock)
    if cmd != NCC_BEGIN_V1: raise ValueError(f"Phase 1: expected 0xA0, got {hex(cmd)}")
    print(f"  ✓ Phase 1: BeginConnection  (0xA0 ↔ 0xA0)")

    # Phase 2: RSA key exchange
    our_rsa = RSA.generate(2048)
    s1 = CS(); s1.gen()   # OUR session state (server will read with this)
    s2 = CS()             # SERVER's session state (we will read with this)

    # Receive server's public key
    cmd = raw_rcmd(sock)
    if cmd != NCC_PUBLIC_KEY: raise ValueError(f"Phase 2: expected PublicKey, got {hex(cmd)}")
    server_rsa = from_xml(raw_rdata(sock).decode())
    print(f"  ✓ Phase 2: Received server RSA-2048 public key")

    # Send our public key
    raw_wcmd(sock, NCC_PUBLIC_KEY)
    raw_wdata(sock, to_xml(our_rsa).encode())

    # Phase 3: Send our session key encrypted with server's public key
    raw_wcmd(sock, NCC_SYMMETRIC_KEY)
    raw_wdata(sock, renc(server_rsa, s1.iv))
    raw_wdata(sock, renc(server_rsa, s1.key))

    # Receive server's session key encrypted with our public key
    cmd = raw_rcmd(sock)
    if cmd != NCC_SYMMETRIC_KEY: raise ValueError(f"Phase 3: expected SymmetricKey, got {hex(cmd)}")
    s2.iv  = rdec(our_rsa, raw_rdata(sock))
    s2.key = rdec(our_rsa, raw_rdata(sock))
    print(f"  ✓ Phase 3: Session keys exchanged")

    # Set up cipher channels
    # cin uses OUR state  (server writes with s1, we read with s1)
    # cout uses SERVER state (we write with s2, server reads with s2)
    cin  = Chan(sock, s1)
    cout = Chan(sock, s2)

    # Phase 4: Password
    cmd = cin.rcmd()
    if cmd != NCC_REQUEST_PASSWORD: raise ValueError(f"Phase 4: expected RequestPassword, got {hex(cmd)}")
    cout.wcmd(NCC_SUBMIT_PASSWORD)
    cout.wdata(password.encode('utf-8'))
    print(f"  ✓ Phase 4: Submitted password '{password}'")

    # Phase 5: Confirm
    resp = cin.rcmd()
    if resp == NCC_PASSWORD_INVALID:
        cin.rcmd()  # EndConnection
        raise ValueError("PASSWORD REJECTED — check server password in AES Server Setup")
    if resp != NCC_PASSWORD_VALID:
        raise ValueError(f"Phase 5: expected PasswordValid, got {hex(resp)}")

    cmd = cin.rcmd()
    if cmd != NCC_CONN_INIT: raise ValueError(f"Phase 5: expected ConnectionInitialized, got {hex(cmd)}")
    cout.wcmd(NCC_CONN_INIT)
    print(f"  ✓ Phase 5: Connection initialized")

    sock.settimeout(None)
    return cin, cout

# ── Receive one message ────────────────────────────────────────────────────────
def recv_msg(cin):
    nc   = cin.rcmd()
    cc   = cin.rint()
    data = cin.rdata()
    return nc, cc, data

# ── Log a received message ─────────────────────────────────────────────────────
def log_msg(nc, cc, data, label="←"):
    nc_name = {
        NCC_NO_DATA: "NoData", NCC_BINARY: "Binary",
        NCC_OBJECT: "Object", NCC_STRING: "String",
    }.get(nc, f"0x{nc:02X}")
    print(f"  {label} [{ts()}] {nc_name} / {cmd_name(cc)}  ({len(data):,} bytes)")
    if data and len(data) <= 200 and all(32 <= b < 127 or b in (9,10,13) for b in data):
        print(f"       text: {data.decode('ascii','replace')}")
    elif data:
        print(f"       hex:  {data[:32].hex()}{'...' if len(data)>32 else ''}")

# ── Main probe ─────────────────────────────────────────────────────────────────
def probe(host, port, password):
    print(f"\n{'═'*60}")
    print(f"  AES Sync Client Probe")
    print(f"{'═'*60}")
    print(f"  Target:   {host}:{port}")
    print(f"  Password: {password}")
    print(f"{'─'*60}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    print(f"  Connecting to {host}:{port}...")
    sock.connect((host, port))
    print(f"  Connected ✓")
    print(f"\n  ── Handshake ──")

    cin, cout = client_init(sock, password)

    print(f"\n  ── Listening for server's opening message ──")
    print(f"  (AES may push state immediately on connect)\n")

    # Give server 3 seconds to send anything spontaneously
    sock.settimeout(3)
    spontaneous = []
    try:
        while True:
            nc, cc, data = recv_msg(cin)
            spontaneous.append((nc, cc, data))
            log_msg(nc, cc, data, "← PUSH")
    except socket.timeout:
        pass
    sock.settimeout(None)

    if not spontaneous:
        print(f"  (No spontaneous push — server waiting for client command)")

    # Now try commands in order
    attempts = [
        ("RegisterClient",   NCC_NO_DATA, 9728,  None),
        ("RequestEventList", NCC_NO_DATA, 8448,  None),
        ("RequestEventSync", NCC_NO_DATA, 8704,  None),
        ("RequestServerDate",NCC_NO_DATA, 38144, None),
    ]

    for label, nc, cc, data in attempts:
        print(f"\n  ── Sending {label} ({cc}) ──")
        cout.send(nc, cc, data)
        print(f"  → [{ts()}] {label}")

        # Wait up to 5 seconds for response(s)
        sock.settimeout(5)
        got_response = False
        try:
            while True:
                rnc, rcc, rdata = recv_msg(cin)
                log_msg(rnc, rcc, rdata)
                got_response = True
                # If we got an update/sync payload, save it
                if rcc in (16400, 8720, 16640, 16641) and rdata:
                    path = f'sync_payload_{rcc}.bin'
                    with open(path, 'wb') as f: f.write(rdata)
                    print(f"       → Saved {len(rdata):,} bytes to {path}")
                # Stop after first meaningful response
                if rcc not in (256,):  # not just "bad command"
                    break
        except socket.timeout:
            if not got_response:
                print(f"  (no response within 5s)")
        sock.settimeout(None)

    print(f"\n  ── Now listening for any further pushes (30s) ──")
    print(f"  Try entering a score in AES now...")
    sock.settimeout(30)
    try:
        while True:
            nc, cc, data = recv_msg(cin)
            log_msg(nc, cc, data, "← LIVE")
            if cc in (16400, 8720, 16640, 16641) and data:
                path = f'live_payload_{cc}_{int(time.time())}.bin'
                with open(path, 'wb') as f: f.write(data)
                print(f"       → Saved to {path}")
    except socket.timeout:
        print(f"  (30s elapsed, no further pushes)")
    except ConnectionError:
        print(f"  Server closed connection")

    sock.close()
    print(f"\n  Probe complete.")

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--host',     default='127.0.0.1')
    p.add_argument('--port',     type=int, default=17471)
    p.add_argument('--password', default='test123')
    args = p.parse_args()
    probe(args.host, args.port, args.password)