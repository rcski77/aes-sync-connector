#!/usr/bin/env python3
"""
AES Sync Monitor  —  port 17471
=================================
Connects to the AES Scheduler sync server as a client.
AES pushes the full SchedulerFile immediately on every score change.
No registration commands needed — just connect, handshake, and listen.

Usage:
    python aes_monitor.py
    python aes_monitor.py --host 10.0.0.101 --password test123
    python aes_monitor.py --bridge bin\Release\net48\AESBridge.exe
"""

import os, sys, socket, struct, hashlib, base64, gzip, zlib
import argparse, subprocess, json, datetime, threading, time
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
NCC_NO_DATA          = 0x21
NCC_BINARY           = 0x22
NCC_OBJECT           = 0x23
NCC_STRING           = 0x24
NCC_BEGIN_V1         = 0xA0
NCC_CONN_INIT        = 0xFE
NCC_END              = 0xFF
DATA_COMPRESSED      = 1

CMD_EVENT_UPDATE         = 16400   # Full SchedulerFile — parse with AESBridge
CMD_REMOTE_ENTRY_UPDATE  = 16640   # Small BinaryFormatter object — score delta signal
CMD_SCORE_KIOSK_UPDATE   = 16641
CMD_FINISHED_PLAYS       = 16896   # Auto-print related — ignore
CMD_PRINTABLE_MATCHES    = 16897
CMD_PRINTABLE_PLAYS      = 16898
CMD_AUTO_PRINT_MATCHES   = 17153
CMD_BAD_COMMAND          = 256

CMD_NAMES = {
    CMD_EVENT_UPDATE:        "EventUpdateAttached",
    CMD_REMOTE_ENTRY_UPDATE: "RemoteEntryUpdateAttached",
    CMD_SCORE_KIOSK_UPDATE:  "ScoreKioskUpdateAttached",
    CMD_FINISHED_PLAYS:      "FinishedPlaysAttached",
    CMD_PRINTABLE_MATCHES:   "PrintableMatchesAttached",
    CMD_PRINTABLE_PLAYS:     "PrintablePlaysAttached",
    CMD_AUTO_PRINT_MATCHES:  "AutoPrintMatchesAttached",
    CMD_BAD_COMMAND:         "ReceivedBadCommand",
}

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

class Chan:
    def __init__(self, sock, state): self.s = sock; self.st = state
    def r(self, n): return xor(recv_n(self.s, n), self.st)
    def w(self, d): self.s.sendall(xor(d, self.st))
    def rcmd(self): return struct.unpack('<I', self.r(1) + b'\x00\x00\x00')[0]
    def wcmd(self, c): self.w(struct.pack('<I', c)[:1])
    def rint(self): return struct.unpack('<I', self.r(4))[0]
    def wdata(self, data=None):
        if data is None: data = b''
        f = 0
        if len(data) > 4096: data = gzip.compress(data); f = DATA_COMPRESSED
        self.w(struct.pack('<I', len(data))[:3] + struct.pack('<I', f)[:1] + data)
    def rdata(self):
        n = struct.unpack('<I', self.r(3) + b'\x00')[0]
        f = struct.unpack('<I', self.r(1) + b'\x00\x00\x00')[0]
        d = self.r(n) if n else b''
        return decomp(d) if f & DATA_COMPRESSED else d

# ── Raw framing (pre-cipher) ───────────────────────────────────────────────────
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
    """WE connect to AES Scheduler on port 17471."""
    sock.settimeout(15)

    raw_wcmd(sock, NCC_BEGIN_V1)
    if raw_rcmd(sock) != NCC_BEGIN_V1:
        raise ValueError("Phase 1 failed")

    our_rsa = RSA.generate(2048)
    s1 = CS(); s1.gen()  # our state (server reads with this)
    s2 = CS()             # server's state (we read with this)

    if raw_rcmd(sock) != NCC_PUBLIC_KEY:
        raise ValueError("Phase 2 failed")
    server_rsa = from_xml(raw_rdata(sock).decode())

    raw_wcmd(sock, NCC_PUBLIC_KEY)
    raw_wdata(sock, to_xml(our_rsa).encode())

    raw_wcmd(sock, NCC_SYMMETRIC_KEY)
    raw_wdata(sock, renc(server_rsa, s1.iv))
    raw_wdata(sock, renc(server_rsa, s1.key))

    if raw_rcmd(sock) != NCC_SYMMETRIC_KEY:
        raise ValueError("Phase 3 failed")
    s2.iv  = rdec(our_rsa, raw_rdata(sock))
    s2.key = rdec(our_rsa, raw_rdata(sock))

    cin  = Chan(sock, s1)
    cout = Chan(sock, s2)

    if cin.rcmd() != NCC_REQUEST_PASSWORD:
        raise ValueError("Phase 4 failed")
    cout.wcmd(NCC_SUBMIT_PASSWORD)
    cout.wdata(password.encode('utf-8'))

    resp = cin.rcmd()
    if resp == NCC_PASSWORD_INVALID:
        raise ValueError("Wrong password — check AES Server Setup")
    if resp != NCC_PASSWORD_VALID:
        raise ValueError(f"Unexpected response: {hex(resp)}")

    if cin.rcmd() != NCC_CONN_INIT:
        raise ValueError("Phase 5 failed")
    cout.wcmd(NCC_CONN_INIT)

    sock.settimeout(None)
    return cin

# ── Bridge ─────────────────────────────────────────────────────────────────────
def call_bridge(raw, bridge_exe):
    if not bridge_exe or not os.path.exists(bridge_exe):
        return None
    try:
        bin_path  = os.path.abspath('scheduler_file.bin')
        json_path = os.path.abspath('tournament_data.json')
        with open(bin_path, 'wb') as f: f.write(raw)
        r = subprocess.run([os.path.abspath(bridge_exe), bin_path],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        if not os.path.exists(json_path):
            return None
        with open(json_path, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception as e:
        print(f"  [Bridge] {e}")
        return None

# ── Remote entry decoder ──────────────────────────────────────────────────────
def decode_remote_entry(raw, bridge_exe):
    """Decode a RemoteEntryUpdateAttached payload via AESBridge --remote mode."""
    if not bridge_exe or not os.path.exists(bridge_exe):
        return None
    try:
        tmp = os.path.abspath('remote_entry.bin')
        with open(tmp, 'wb') as f: f.write(raw)
        r = subprocess.run(
            [os.path.abspath(bridge_exe), tmp, '--remote'],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
    except Exception as e:
        pass
    return None

# OutcomeType values from Match.OutcomeType enum
OUTCOMES = {
    '0': 'Undecided',
    '1': 'FirstTeamWon',
    '2': 'SecondTeamWon',
    '3': 'Tie',
    '4': 'FirstTeamForfeit',
    '5': 'SecondTeamForfeit',
}

def format_remote_entry(obj, tournament_data):
    """
    Decode a RemoteEntry String[] payload.

    Format confirmed from live data:
      [0] File GUID
      [1] Event ID
      [2] Match ID (negative int)
      [3] OutcomeType (1=FirstTeamWon, 2=SecondTeamWon, ...)
      [4] Match type (1=BestOf)
      [5] Max set count
      [6+] Set scores: t1, t2, t1, t2, ... (one pair per set played)
    """
    if not obj: return None
    vals = obj.get('values')
    if not vals or len(vals) < 6:
        return f"  ENTRY  (unexpected format: {obj})"

    # Parse fields
    try: match_id = int(vals[2])
    except: match_id = None

    outcome_code = str(vals[3]) if vals[3] is not None else '0'
    outcome      = OUTCOMES.get(outcome_code, f"outcome={outcome_code}")

    # Set scores — pairs starting at index 6
    set_scores = []
    i = 6
    while i + 1 < len(vals):
        try:
            s1 = int(vals[i]); s2 = int(vals[i+1])
            set_scores.append((s1, s2))
        except: pass
        i += 2

    score_text = ', '.join(f"{a}-{b}" for a, b in set_scores)

    # Look up match in last known tournament data
    match_info = ""
    winner     = ""
    if match_id and tournament_data:
        for m in tournament_data.get('matches', []):
            if m.get('matchId') == match_id:
                t1 = m.get('team1', '?')
                t2 = m.get('team2', '?')
                court = m.get('courtName', '?')
                name  = m.get('shortName', '?')
                match_info = f"[{court}] {name}  {t1} vs {t2}"
                if outcome_code == '1': winner = t1
                elif outcome_code == '2': winner = t2
                elif outcome_code == '4': winner = f"{t2} (forfeit by {t1})"
                elif outcome_code == '5': winner = f"{t1} (forfeit by {t2})"
                break

    if not match_info:
        match_info = f"matchId={match_id}"

    parts = [match_info]
    if score_text: parts.append(score_text)
    if winner:     parts.append(f"{winner} wins")
    elif outcome_code not in ('1','2','3','4','5','0'):
        parts.append(outcome)

    return "  ENTRY  " + "  →  ".join(parts)

# ── Differ ─────────────────────────────────────────────────────────────────────
def diff(prev, curr):
    if not prev or not curr: return []
    changes = []
    pm = {m['matchId']: m for m in prev.get('matches', [])}
    cm = {m['matchId']: m for m in curr.get('matches', [])}
    for mid, m in cm.items():
        p = pm.get(mid)
        court = m.get('courtName', '?')
        name  = m.get('shortName', '?')
        t1, t2 = m.get('team1','?'), m.get('team2','?')
        if p is None:
            changes.append(f"  NEW    [{court}] {name}  {t1} vs {t2}")
            continue
        cs, ps = m.get('scoreText',''), p.get('scoreText','')
        if cs != ps:
            if m.get('decided'):
                w = t1 if m.get('firstTeamWon') else t2
                changes.append(f"  FINAL  [{court}] {name}  {t1} vs {t2}  →  {cs}  ({w} wins)")
            elif cs:
                changes.append(f"  SCORE  [{court}] {name}  {t1} vs {t2}  →  {cs}")
        elif m.get('sets') != p.get('sets'):
            for i, (a, b) in enumerate(zip(m.get('sets',[]), p.get('sets',[]) or [])):
                if a != b:
                    changes.append(f"  SET {i+1}  [{court}] {name}  {t1} vs {t2}  "
                                   f"→  {a.get('team1')}-{a.get('team2')}")
    return changes

# ── Display ────────────────────────────────────────────────────────────────────
def ts(): return datetime.datetime.now().strftime('%H:%M:%S')

def show_update(data, raw_size, n, changes):
    print(f"\n{'═'*62}")
    print(f"  UPDATE #{n}  {ts()}  —  {raw_size:,} bytes")
    print(f"{'─'*62}")
    if data:
        matches = data.get('matches', [])
        decided = sum(1 for m in matches if m.get('decided'))
        pending = len(matches) - decided
        print(f"  Event:   {data['event'].get('name','')}")
        print(f"  Matches: {len(matches)} total  |  "
              f"{decided} decided  |  {pending} pending")
        if changes:
            print(f"\n  ── Changes ──────────────────────────────────────────")
            for c in changes: print(c)
        else:
            print(f"\n  (no score changes vs previous update)")
        if pending:
            next_m = [m for m in matches if not m.get('decided')][:4]
            print(f"\n  ── Pending (next {len(next_m)}) ─────────────────────────────")
            for m in next_m:
                st = m.get('startTime','')[:16].replace('T',' ')
                print(f"    [{m.get('courtName','?')}] {st}  "
                      f"{m.get('team1','?')} vs {m.get('team2','?')}")
    else:
        print(f"  (bridge not available — raw binary saved to scheduler_file.bin)")
    print(f"{'─'*62}")

# ── Main ───────────────────────────────────────────────────────────────────────
def monitor(host, port, password, bridge_exe):
    print(f"\n{'═'*62}")
    print(f"  AES Sync Monitor  —  real-time score updates")
    print(f"{'═'*62}")
    print(f"  Host:     {host}:{port}")
    print(f"  Password: {password}")
    if bridge_exe and os.path.exists(bridge_exe):
        print(f"  Bridge:   {bridge_exe}")
    else:
        print(f"  Bridge:   not found (scores won't be parsed — check --bridge path)")
    print(f"  Press Ctrl+C to stop")
    print(f"{'─'*62}\n")

    prev_data  = None
    update_num = 0

    while True:
        print(f"  [{ts()}] Connecting to {host}:{port}...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.connect((host, port))
            print(f"  [{ts()}] Connected — completing handshake...")
            cin = client_init(sock, password)
            print(f"  [{ts()}] Handshake complete ✓  —  waiting for data...\n")

            while True:
                nc   = cin.rcmd()
                cc   = cin.rint()
                data = cin.rdata()
                name = CMD_NAMES.get(cc, f"cmd={cc}")

                # Full tournament update — parse and diff
                if cc == CMD_EVENT_UPDATE and data:
                    update_num += 1
                    curr = call_bridge(data, bridge_exe)
                    if curr is None:
                        print(f"  [{ts()}] Bridge failed — check AESBridge.exe path")
                    changes = diff(prev_data, curr)
                    if not changes and prev_data and curr:
                        # Help diagnose: count decided matches in each snapshot
                        pd = sum(1 for m in prev_data.get('matches',[]) if m.get('decided'))
                        cd = sum(1 for m in curr.get('matches',[]) if m.get('decided'))
                        if cd != pd:
                            changes = [f"  (decided count changed: {pd} → {cd}, diff may have missed detail)"]
                    show_update(curr, len(data), update_num, changes)
                    prev_data = curr

                # Score delta signal — decode and show immediately
                elif cc == CMD_REMOTE_ENTRY_UPDATE:
                    obj = decode_remote_entry(data, bridge_exe)
                    change = format_remote_entry(obj, prev_data)
                    if change:
                        print(f"  [{ts()}] {change}")
                    else:
                        print(f"  [{ts()}] ← RemoteEntryUpdate ({len(data)} bytes)")

                # Auto-print / heartbeat signals — just log briefly
                elif cc in (CMD_FINISHED_PLAYS, CMD_PRINTABLE_MATCHES,
                            CMD_PRINTABLE_PLAYS, CMD_AUTO_PRINT_MATCHES):
                    pass  # silently ignore auto-print noise

                else:
                    print(f"  [{ts()}] ← {name}  ({len(data)} bytes)")

        except KeyboardInterrupt:
            print(f"\n  Monitor stopped.")
            try: sock.close()
            except: pass
            break
        except ConnectionError as e:
            print(f"\n  [{ts()}] Disconnected: {e}")
        except Exception as e:
            print(f"\n  [{ts()}] Error: {e}")
        finally:
            try: sock.close()
            except: pass

        print(f"  [{ts()}] Reconnecting in 5 seconds...")
        try: time.sleep(5)
        except KeyboardInterrupt:
            print(f"\n  Monitor stopped."); break

if __name__ == '__main__':
    p = argparse.ArgumentParser(description='AES real-time sync monitor')
    p.add_argument('--host',     default='127.0.0.1')
    p.add_argument('--port',     type=int, default=17471)
    p.add_argument('--password', default='test123')
    p.add_argument('--bridge',   default=r'bin\Release\net48\AESBridge.exe')
    args = p.parse_args()
    monitor(args.host, args.port, args.password, args.bridge)