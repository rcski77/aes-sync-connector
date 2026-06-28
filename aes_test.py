#!/usr/bin/env python3
"""
AES Display Protocol - CLI Connection Test
==========================================
Run this on the machine where AES Scheduler is running (or on the same LAN).
It broadcasts the beacon, waits for AES Scheduler to connect, completes the
handshake, receives one data push, then prints a summary and exits.

Usage:
    pip install pycryptodome
    python aes_test.py

    # Optionally override port or nickname:
    python aes_test.py --port 19211 --nickname TestDash
"""

import os, sys, socket, struct, hashlib, threading, time, base64, gzip, zlib
import argparse
import xml.etree.ElementTree as ET

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
except ImportError:
    print("ERROR: pycryptodome not installed.")
    print("       Run: pip install pycryptodome")
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────
DISPLAY_PASSWORD    = "Display Protocol v3.0"
BEACON_PORT         = 19212
SCHEDULER_VERSION   = 660209715   # 0x2754AFC3 — magic bytes in every SchedulerFile

NCC_PUBLIC_KEY           = 0x11
NCC_SYMMETRIC_KEY        = 0x12
NCC_REQUEST_PASSWORD     = 0x13
NCC_SUBMIT_PASSWORD      = 0x14
NCC_PASSWORD_VALID       = 0x15
NCC_PASSWORD_INVALID     = 0x16
NCC_COMMAND_NO_DATA      = 0x21
NCC_COMMAND_BINARY       = 0x22
NCC_BEGIN_CONNECTION_V1  = 0xA0
NCC_CONNECTION_INIT      = 0xFE
NCC_END_CONNECTION       = 0xFF

SCHED_PLASMA_SELECTED    = 12288   # 0x3000
SCHED_EVENT_UPDATE       = 16400   # 0x4010
SCHED_EVENT_REFRESH      = 12547   # 0x3103
SCHED_INACTIVATE         = 12544   # 0x3100
DATA_FLAG_COMPRESSED     = 1

# ── Logging helpers ────────────────────────────────────────────────────────────

def log(msg):
    print(f"  {msg}")

def ok(msg):
    print(f"  ✓ {msg}")

def fail(msg):
    print(f"  ✗ {msg}")

def section(title):
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")

# ── Stream cipher ──────────────────────────────────────────────────────────────

class CipherState:
    def __init__(self):
        self.key = b''
        self.iv  = b''
        self.pos = 0

    def generate(self):
        self.key = os.urandom(64)
        self.iv  = os.urandom(32)


def xor_cipher(data: bytes, state: CipherState) -> bytes:
    iv = bytearray(state.iv)
    pos = state.pos
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ iv[pos]
        pos += 1
        if pos == len(iv):
            iv = bytearray(hashlib.sha256(bytes(iv) + state.key).digest())
            pos = 0
    state.iv  = bytes(iv)
    state.pos = pos
    return bytes(out)

# ── RSA helpers ────────────────────────────────────────────────────────────────

def rsa_to_xml(key) -> str:
    pub = key.publickey()
    n = base64.b64encode(pub.n.to_bytes((pub.n.bit_length()+7)//8,'big')).decode()
    e = base64.b64encode(pub.e.to_bytes((pub.e.bit_length()+7)//8,'big')).decode()
    return f'<RSAKeyValue><Modulus>{n}</Modulus><Exponent>{e}</Exponent></RSAKeyValue>'

def rsa_from_xml(xml: str):
    root = ET.fromstring(xml)
    n = int.from_bytes(base64.b64decode(root.find('Modulus').text), 'big')
    e = int.from_bytes(base64.b64decode(root.find('Exponent').text), 'big')
    return RSA.construct((n, e))

def rsa_encrypt(pub, data: bytes) -> bytes:
    return PKCS1_v1_5.new(pub).encrypt(data)

def rsa_decrypt(priv, data: bytes) -> bytes:
    sentinel = b'\x00'
    result = PKCS1_v1_5.new(priv).decrypt(data, sentinel)
    if result == sentinel:
        raise ValueError("RSA decrypt failed")
    return result

# ── Wire helpers ───────────────────────────────────────────────────────────────

def recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf += chunk
    return buf

def raw_read_cmd(sock):
    return struct.unpack('<I', recv_exact(sock,1) + b'\x00\x00\x00')[0]

def raw_write_cmd(sock, cmd):
    sock.sendall(struct.pack('<I', cmd)[:1])

def raw_read_data(sock):
    length = struct.unpack('<I', recv_exact(sock,3) + b'\x00')[0]
    flags  = struct.unpack('<I', recv_exact(sock,1) + b'\x00\x00\x00')[0]
    data   = recv_exact(sock, length) if length else b''
    if flags & DATA_FLAG_COMPRESSED:
        data = decompress(data)
    return data

def raw_write_data(sock, data):
    if data is None: data = b''
    flags = 0
    if len(data) > 4096:
        data  = gzip.compress(data)
        flags = DATA_FLAG_COMPRESSED
    sock.sendall(struct.pack('<I', len(data))[:3] + struct.pack('<I', flags)[:1] + data)

def decompress(data):
    for fn in [gzip.decompress, zlib.decompress, lambda d: zlib.decompress(d, -15)]:
        try: return fn(data)
        except: pass
    raise ValueError("Cannot decompress data")

# ── Cipher channel ─────────────────────────────────────────────────────────────

class Chan:
    def __init__(self, sock, state):
        self.sock  = sock
        self.state = state

    def read(self, n):
        return xor_cipher(recv_exact(self.sock, n), self.state)

    def write(self, data):
        self.sock.sendall(xor_cipher(data, self.state))

    def read_cmd(self):
        return struct.unpack('<I', self.read(1) + b'\x00\x00\x00')[0]

    def write_cmd(self, cmd):
        self.write(struct.pack('<I', cmd)[:1])

    def read_int(self):
        return struct.unpack('<I', self.read(4))[0]

    def read_data(self):
        length = struct.unpack('<I', self.read(3) + b'\x00')[0]
        flags  = struct.unpack('<I', self.read(1) + b'\x00\x00\x00')[0]
        data   = self.read(length) if length else b''
        if flags & DATA_FLAG_COMPRESSED:
            data = decompress(data)
        return data

    def write_data(self, data):
        if data is None: data = b''
        flags = 0
        if len(data) > 4096:
            data  = gzip.compress(data)
            flags = DATA_FLAG_COMPRESSED
        frame = struct.pack('<I', len(data))[:3] + struct.pack('<I', flags)[:1] + data
        self.write(frame)

    def send_cmd(self, nc_cmd, custom_cmd, data=None):
        self.write_cmd(nc_cmd)
        self.write(struct.pack('<I', custom_cmd))
        self.write_data(data)

# ── Beacon ─────────────────────────────────────────────────────────────────────

def make_beacon(port, nickname, ip):
    d = bytearray()
    d += struct.pack('<I', port)
    nb = nickname.encode('utf-8')
    d += bytes([len(nb)]) + nb
    ib = socket.inet_aton(ip)
    d += bytes([len(ib)]) + ib
    d += hashlib.sha256(bytes(d)).digest()
    return bytes(d)

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return '127.0.0.1'

# ── Handshake ──────────────────────────────────────────────────────────────────

def do_handshake(sock):
    """
    Run ServerInit: we're the display listener, AES Scheduler is the client.
    Returns (cin, cout) cipher channels on success, raises on failure.
    """
    sock.settimeout(15.0)

    # Phase 1
    cmd = raw_read_cmd(sock)
    assert cmd == NCC_BEGIN_CONNECTION_V1, f"Expected 0xA0, got {hex(cmd)}"
    raw_write_cmd(sock, NCC_BEGIN_CONNECTION_V1)
    ok(f"Phase 1: BeginConnection ping-pong  (0xA0 ↔ 0xA0)")

    # Phase 2: RSA key exchange
    our_rsa  = RSA.generate(2048)
    our_st   = CipherState(); our_st.generate()
    their_st = CipherState()

    raw_write_cmd(sock, NCC_PUBLIC_KEY)
    raw_write_data(sock, rsa_to_xml(our_rsa).encode('utf-8'))

    cmd = raw_read_cmd(sock)
    assert cmd == NCC_PUBLIC_KEY, f"Expected 0x11, got {hex(cmd)}"
    their_rsa = rsa_from_xml(raw_read_data(sock).decode('utf-8'))
    ok(f"Phase 2: RSA-2048 public key exchange")

    # Phase 3: symmetric key exchange
    cmd = raw_read_cmd(sock)
    assert cmd == NCC_SYMMETRIC_KEY, f"Expected 0x12, got {hex(cmd)}"
    their_st.iv  = rsa_decrypt(our_rsa, raw_read_data(sock))
    their_st.key = rsa_decrypt(our_rsa, raw_read_data(sock))

    raw_write_cmd(sock, NCC_SYMMETRIC_KEY)
    raw_write_data(sock, rsa_encrypt(their_rsa, our_st.iv))
    raw_write_data(sock, rsa_encrypt(their_rsa, our_st.key))
    ok(f"Phase 3: Symmetric session keys exchanged")

    # Set up cipher channels
    cin  = Chan(sock, our_st)    # read from AES  → decrypt with our state
    cout = Chan(sock, their_st)  # write to AES   → encrypt with their state

    # Phase 4: password
    cout.write_cmd(NCC_REQUEST_PASSWORD)
    cmd = cin.read_cmd()
    assert cmd == NCC_SUBMIT_PASSWORD, f"Expected 0x14, got {hex(cmd)}"
    pw = cin.read_data().decode('utf-8')
    assert pw == DISPLAY_PASSWORD, f"Wrong password: '{pw}'"

    cout.write_cmd(NCC_PASSWORD_VALID)
    cout.write_cmd(NCC_CONNECTION_INIT)
    ok(f"Phase 4: Password verified  ('{pw}')")

    # Phase 5: finalize
    cmd = cin.read_cmd()
    assert cmd == NCC_CONNECTION_INIT, f"Expected 0xFE, got {hex(cmd)}"
    ok(f"Phase 5: Connection initialized")

    sock.settimeout(None)
    return cin, cout

# ── Binary format validator ────────────────────────────────────────────────────

def inspect_scheduler_file(data: bytes):
    """Quick validation and summary of the SchedulerFile binary blob."""
    if len(data) < 4:
        fail("Data too short to be a SchedulerFile")
        return

    version = struct.unpack_from('<i', data, 0)[0]
    if version == SCHEDULER_VERSION:
        ok(f"Magic version confirmed: {version} (0x{version:08X})")
    else:
        log(f"Version: {version} (0x{version:08X}) — expected {SCHEDULER_VERSION}")

    log(f"Total size: {len(data):,} bytes")

    # Try to read a few more fields to prove structure
    try:
        pos = 4
        next_manual_id = struct.unpack_from('<i', data, pos)[0]
        pos += 4
        file_id = data[pos:pos+16].hex()
        pos += 16
        file_type = struct.unpack_from('<i', data, pos)[0]
        pos += 4
        club_count = struct.unpack_from('<i', data, pos)[0]
        pos += 4

        log(f"NextManualID: {next_manual_id}")
        log(f"FileID (GUID): {file_id}")
        log(f"FileType: {'Volleyball' if file_type == 1 else 'Basketball' if file_type == 2 else file_type}")
        log(f"Club count: {club_count}")
        ok("Binary structure looks valid — SchedulerFile.Load() should parse this cleanly")
    except Exception as e:
        log(f"Could not inspect deeper: {e}")

# ── Main test ──────────────────────────────────────────────────────────────────

def run_test(listen_port, nickname):
    my_ip   = local_ip()
    beacon  = make_beacon(listen_port, nickname, my_ip)
    got_data = threading.Event()
    result   = {}

    # Start beacon broadcaster
    def beacon_loop(stop):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not stop.is_set():
            try:
                sock.sendto(beacon, ('<broadcast>', BEACON_PORT))
            except: pass
            stop.wait(5)  # faster than normal for testing
        sock.close()

    stop_beacon = threading.Event()
    bt = threading.Thread(target=beacon_loop, args=(stop_beacon,), daemon=True)
    bt.start()

    section("BEACON")
    log(f"Broadcasting on UDP {BEACON_PORT}")
    log(f"Nickname : '{nickname}'")
    log(f"Our IP   : {my_ip}")
    log(f"Our port : {listen_port}")
    log(f"Packet   : {len(beacon)} bytes  (including 32-byte SHA-256 checksum)")

    section("WAITING FOR AES SCHEDULER")
    log("Listening on TCP port " + str(listen_port))
    log("Open AES Scheduler → the server should appear in the display list")
    log("Waiting up to 60 seconds...")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('', listen_port))
    server.listen(1)
    server.settimeout(60)

    try:
        client_sock, addr = server.accept()
    except socket.timeout:
        fail("No connection from AES Scheduler within 60 seconds")
        log("Make sure AES Scheduler is running and the server is started")
        stop_beacon.set()
        return False

    ok(f"AES Scheduler connected from {addr[0]}:{addr[1]}")
    stop_beacon.set()

    section("HANDSHAKE")
    try:
        cin, cout = do_handshake(client_sock)
    except Exception as e:
        fail(f"Handshake failed: {e}")
        client_sock.close()
        return False

    section("REQUESTING DATA")
    cout.send_cmd(NCC_COMMAND_NO_DATA, SCHED_PLASMA_SELECTED)
    ok(f"Sent PlasmaDisplayModeSelected  (cmd={SCHED_PLASMA_SELECTED})")
    log("Waiting for EventUpdateAttached...")

    section("RECEIVING")
    client_sock.settimeout(30)
    try:
        while True:
            nc_cmd     = cin.read_cmd()
            custom_cmd = cin.read_int()
            data       = cin.read_data()

            cmd_name = {
                SCHED_EVENT_UPDATE:  "EventUpdateAttached",
                SCHED_EVENT_REFRESH: "EventRefreshAttached",
                SCHED_INACTIVATE:    "InactivateDisplay",
            }.get(custom_cmd, f"Unknown({hex(custom_cmd)})")

            nc_name = {
                NCC_COMMAND_NO_DATA: "CommandWithNoData",
                NCC_COMMAND_BINARY:  "CommandWithBinaryData",
                0x23: "CommandWithObjectData",
                0x24: "CommandWithStringData",
            }.get(nc_cmd, f"0x{nc_cmd:02x}")

            log(f"← {nc_name} / {cmd_name}  ({len(data):,} bytes)")

            if custom_cmd == SCHED_EVENT_UPDATE and data:
                ok(f"Got EventUpdateAttached with {len(data):,} bytes")
                result['data'] = data
                break

    except socket.timeout:
        fail("Timed out waiting for EventUpdateAttached (30s)")
        client_sock.close()
        return False
    except Exception as e:
        fail(f"Receive error: {e}")
        client_sock.close()
        return False

    client_sock.close()

    section("VALIDATING SCHEDULER FILE")
    if 'data' in result:
        inspect_scheduler_file(result['data'])

        out_path = 'scheduler_file.bin'
        with open(out_path, 'wb') as f:
            f.write(result['data'])
        log(f"Raw bytes saved to: {out_path}")

    section("RESULT")
    ok("TEST PASSED — AES Scheduler is connected and pushing data")
    log("")
    log("Next steps:")
    log("  1. Compile AESBridge.cs to parse scheduler_file.bin into JSON")
    log("  2. Run aes_connector.py for continuous live updates")
    log("")
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='AES Display Protocol connection test')
    parser.add_argument('--port',     type=int, default=19211, help='TCP listen port (default: 19211)')
    parser.add_argument('--nickname', type=str, default='DashTest', help='Display nickname (default: DashTest)')
    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║     AES Display Protocol — Connection Test           ║")
    print("╚══════════════════════════════════════════════════════╝")

    success = run_test(args.port, args.nickname)
    sys.exit(0 if success else 1)
