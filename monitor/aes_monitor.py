#!/usr/bin/env python3
"""
AES Sync Monitor — production version
=======================================
Connects to AES Scheduler on port 17471, receives real-time tournament
updates, parses them via AESBridge.exe, and POSTs JSON to your dashboard.

Config: aes_config.ini (in the same folder as this script)
Usage:  python aes_monitor.py
"""

import os, sys, socket, struct, hashlib, base64, gzip, zlib
import subprocess, json, datetime, threading, time, re
import configparser, urllib.request, urllib.error
import xml.etree.ElementTree as ET

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
except ImportError:
    print("ERROR: pip install pycryptodome"); sys.exit(1)

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Load .env — check next to script first, then parent directory (project root)
    env_path = os.path.join(script_dir, '.env')
    if not os.path.exists(env_path):
        env_path = os.path.join(os.path.dirname(script_dir), '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())

    cfg = configparser.RawConfigParser()  # RawConfigParser avoids % interpolation on $ values
    cfg_path = os.path.join(script_dir, 'aes_config.ini')
    if not os.path.exists(cfg_path):
        print(f"ERROR: Config file not found: {cfg_path}")
        sys.exit(1)
    cfg.read(cfg_path)

    # Expand ${VAR} / $VAR references in every value
    for section in cfg.sections():
        for key, value in cfg.items(section):
            cfg.set(section, key, os.path.expandvars(value))

    return cfg

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
NCC_BEGIN_V1         = 0xA0
NCC_CONN_INIT        = 0xFE
DATA_COMPRESSED      = 1

CMD_EVENT_UPDATE         = 16400
CMD_REMOTE_ENTRY_UPDATE  = 16640
CMD_FINISHED_PLAYS       = 16896
CMD_PRINTABLE_MATCHES    = 16897
CMD_PRINTABLE_PLAYS      = 16898
CMD_AUTO_PRINT_MATCHES   = 17153

OUTCOMES = {
    '0': 'Undecided', '1': 'FirstTeamWon', '2': 'SecondTeamWon',
    '3': 'Tie', '4': 'FirstTeamForfeit', '5': 'SecondTeamForfeit',
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
        if not c: raise ConnectionError("Disconnected")
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

# ── Raw framing ────────────────────────────────────────────────────────────────

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

# ── Handshake ──────────────────────────────────────────────────────────────────

def client_init(sock, password):
    sock.settimeout(15)
    raw_wcmd(sock, NCC_BEGIN_V1)
    if raw_rcmd(sock) != NCC_BEGIN_V1: raise ValueError("Phase 1 failed")

    our_rsa = RSA.generate(2048)
    s1 = CS(); s1.gen()
    s2 = CS()

    if raw_rcmd(sock) != NCC_PUBLIC_KEY: raise ValueError("Phase 2 failed")
    server_rsa = from_xml(raw_rdata(sock).decode())

    raw_wcmd(sock, NCC_PUBLIC_KEY)
    raw_wdata(sock, to_xml(our_rsa).encode())
    raw_wcmd(sock, NCC_SYMMETRIC_KEY)
    raw_wdata(sock, renc(server_rsa, s1.iv))
    raw_wdata(sock, renc(server_rsa, s1.key))

    if raw_rcmd(sock) != NCC_SYMMETRIC_KEY: raise ValueError("Phase 3 failed")
    s2.iv  = rdec(our_rsa, raw_rdata(sock))
    s2.key = rdec(our_rsa, raw_rdata(sock))

    cin = Chan(sock, s1); cout = Chan(sock, s2)

    if cin.rcmd() != NCC_REQUEST_PASSWORD: raise ValueError("Phase 4 failed")
    cout.wcmd(NCC_SUBMIT_PASSWORD)
    cout.wdata(password.encode('utf-8'))

    resp = cin.rcmd()
    if resp == NCC_PASSWORD_INVALID: raise ValueError("Wrong password")
    if resp != NCC_PASSWORD_VALID:   raise ValueError(f"Unexpected: {hex(resp)}")
    if cin.rcmd() != NCC_CONN_INIT:  raise ValueError("Phase 5 failed")
    cout.wcmd(NCC_CONN_INIT)

    sock.settimeout(None)
    return cin

# ── Bridge ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def call_bridge(raw, bridge_exe):
    """Parse SchedulerFile binary via AESBridge.exe → returns dict or None."""
    if not bridge_exe or not os.path.exists(bridge_exe):
        return None
    try:
        bin_path  = os.path.join(SCRIPT_DIR, 'scheduler_file.bin')
        json_path = os.path.join(SCRIPT_DIR, 'tournament_data.json')
        with open(bin_path, 'wb') as f: f.write(raw)
        r = subprocess.run(
            [bridge_exe, bin_path],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            log(f"Bridge error: {r.stderr.strip()[:120]}")
            return None
        if not os.path.exists(json_path):
            log("Bridge: tournament_data.json not found")
            return None
        with open(json_path, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception as e:
        log(f"Bridge exception: {e}")
        return None

def decode_remote_entry(raw, bridge_exe):
    """Decode RemoteEntryUpdate String[] via AESBridge.exe --remote."""
    if not bridge_exe or not os.path.exists(bridge_exe):
        return None
    try:
        tmp = os.path.join(SCRIPT_DIR, 'remote_entry.bin')
        with open(tmp, 'wb') as f: f.write(raw)
        r = subprocess.run(
            [bridge_exe, tmp, '--remote'],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
    except Exception:
        pass
    return None

# ── Dashboard push ─────────────────────────────────────────────────────────────

SNAPSHOT_INTERVAL = 180  # seconds between full snapshot POSTs

def _post(url, payload_obj, ingest_key, timeout, label):
    """Serialize and POST a payload; log result. Runs in a daemon thread."""
    try:
        payload = json.dumps(payload_obj).encode('utf-8')
        req = urllib.request.Request(
            url,
            data   = payload,
            method = 'POST',
            headers = {
                'Content-Type':   'application/json',
                'Content-Length': str(len(payload)),
                **(({'Authorization': f'Bearer {ingest_key}'}) if ingest_key else {}),
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                log(f"{label} OK (200)")
            else:
                log(f"{label} returned {resp.status}")
    except urllib.error.HTTPError as e:
        log(f"{label} HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        log(f"{label} failed: {e.reason}")
    except Exception as e:
        log(f"{label} exception: {e}")


def _eastern_naive(iso_str):
    """Convert an ISO 8601 UTC string from the bridge to Eastern local time, no offset."""
    try:
        import datetime as _dt
        # bridge emits e.g. "2025-06-28T17:30:00.0000000+00:00"
        dt = _dt.datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        # Convert to Eastern — use zoneinfo (Python 3.9+) or fall back to fixed offset
        try:
            from zoneinfo import ZoneInfo
            eastern = dt.astimezone(ZoneInfo('America/New_York'))
        except ImportError:
            # Fixed -5 fallback (no DST awareness — acceptable degradation)
            eastern = dt + _dt.timedelta(hours=-5)
        return eastern.strftime('%Y-%m-%dT%H:%M:%S')
    except Exception:
        return iso_str  # pass through unchanged if parsing fails


def _match_payload(m):
    """Map a tournament_data.json match dict to the ingest API match shape."""
    return {
        'matchId':   m.get('matchId'),
        'division':  m.get('divisionName', ''),
        'courtId':   m.get('courtId'),
        'courtName': m.get('courtName', ''),
        'startTime': _eastern_naive(m.get('startTime', '')),
        'endTime':   _eastern_naive(m.get('endTime', '')),
        'team1':     m.get('team1', ''),
        'team2':     m.get('team2', ''),
        'workTeam':  m.get('workTeam') or None,
        'hasResult': bool(m.get('decided')),
        'sets':      [{'ft': s['team1'], 'st': s['team2']} for s in m.get('sets', [])],
    }


def _strip_seed(name):
    return re.sub(r'\s+\([A-Z]{1,3}\)$', '', name or '').strip()


def _pool_payload(p):
    """Map a tournament_data.json pool dict to the ingest API pool shape."""
    standings = p.get('standings', [])
    teams = []
    for rank, st in enumerate(standings, start=1):
        pts_against = st.get('ptsAgainst', 0)
        pts_for     = st.get('ptsFor', 0)
        ratio = round(pts_for / pts_against, 4) if pts_against else None
        teams.append({
            'name':        _strip_seed(st.get('team', '')),
            'matchesWon':  st.get('wins', 0),
            'matchesLost': st.get('losses', 0),
            'setsWon':     st.get('setsWon', 0),
            'setsLost':    st.get('setsLost', 0),
            'pointRatio':  ratio,
            'finishRank':  rank,
        })
    courts = p.get('courts') or []
    first_court = courts[0] if courts else {}
    return {
        'playId':          p.get('poolId'),
        'division':        p.get('divisionName', ''),
        'name':            p.get('name', ''),
        'shortName':       p.get('shortName', ''),
        'courtId':         first_court.get('courtId'),
        'courtName':       first_court.get('name', ''),
        'courts':          courts,
        'date':            p.get('date', ''),
        'goldSpotsCount':  None,
        'teams':           teams,
    }


def push_delta(entry_obj, tournament_data, base_url, ingest_key, timeout):
    """POST a single score delta to /api/ingest/delta."""
    if not base_url or not entry_obj:
        return
    vals = entry_obj.get('values') or []
    if len(vals) < 4:
        return

    try:    match_id = int(vals[2])
    except: return

    outcome_code = str(vals[3]) if vals[3] is not None else '0'
    outcome_map  = {'1': 'FirstTeamWon', '2': 'SecondTeamWon', '3': 'Tie',
                    '4': 'FirstTeamForfeit', '5': 'SecondTeamForfeit'}

    set_scores = []
    i = 6
    while i + 1 < len(vals):
        try: set_scores.append({'ft': int(vals[i]), 'st': int(vals[i+1])})
        except: pass
        i += 2

    if not set_scores and outcome_code != '0':
        return  # winner-only delta (no scores entered yet) — wait for the complete one

    # Look up match info from the current snapshot for division/court/times/teams
    match_info = {}
    if tournament_data:
        for m in tournament_data.get('matches', []):
            if m.get('matchId') == match_id:
                match_info = m
                break

    event_id = str(tournament_data['event']['eventId']) if tournament_data else ''

    payload = {
        'aesEventId': event_id,
        'match': {
            'matchId':   match_id,
            'division':  match_info.get('divisionName', ''),
            'courtId':   match_info.get('courtId'),
            'courtName': match_info.get('courtName', ''),
            'startTime': _eastern_naive(match_info.get('startTime', '')),
            'endTime':   _eastern_naive(match_info.get('endTime', '')),
            'team1':     match_info.get('team1', ''),
            'team2':     match_info.get('team2', ''),
            'workTeam':  match_info.get('workTeam') or None,
            'hasResult': outcome_code in ('1', '2', '3', '4', '5'),
            'sets':      set_scores,
            'outcome':   outcome_map.get(outcome_code, 'Undecided'),
        },
    }
    url = base_url.rstrip('/') + '/delta'
    # print(f"\n  ── Delta payload ──\n{json.dumps(payload, indent=2)}\n")
    threading.Thread(target=_post, args=(url, payload, ingest_key, timeout, 'delta'), daemon=True).start()


def push_snapshot(tournament_data, base_url, ingest_key, timeout):
    """POST full tournament state to /api/ingest/snapshot."""
    if not base_url or not tournament_data:
        return
    event_id = str(tournament_data['event']['eventId'])
    payload = {
        'aesEventId':    event_id,
        'snapshotTime':  datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'matches':       [_match_payload(m) for m in tournament_data.get('matches', [])],
        'pools':         [_pool_payload(p) for p in tournament_data.get('pools', [])],
    }
    url = base_url.rstrip('/') + '/snapshot'
    threading.Thread(target=_post, args=(url, payload, ingest_key, timeout, 'snapshot'), daemon=True).start()

# ── Remote entry formatter ─────────────────────────────────────────────────────

def format_remote_entry(obj, tournament_data):
    if not obj: return None
    vals = obj.get('values')
    if not vals or len(vals) < 6:
        return f"  ENTRY  (unexpected format)"

    try: match_id = int(vals[2])
    except: match_id = None

    outcome_code = str(vals[3]) if vals[3] is not None else '0'

    set_scores = []
    i = 6
    while i + 1 < len(vals):
        try: set_scores.append((int(vals[i]), int(vals[i+1])))
        except: pass
        i += 2

    score_text = ', '.join(f"{a}-{b}" for a, b in set_scores)

    match_info = winner = ""
    if match_id and tournament_data:
        for m in tournament_data.get('matches', []):
            if m.get('matchId') == match_id:
                t1, t2   = m.get('team1','?'), m.get('team2','?')
                match_info = f"[{m.get('courtName','?')}] {m.get('shortName','?')}  {t1} vs {t2}"
                if outcome_code == '1':   winner = t1
                elif outcome_code == '2': winner = t2
                elif outcome_code == '4': winner = f"{t2} (forfeit by {t1})"
                elif outcome_code == '5': winner = f"{t1} (forfeit by {t2})"
                break

    if not match_info: match_info = f"matchId={match_id}"
    parts = [match_info]
    if score_text: parts.append(score_text)
    if winner:     parts.append(f"{winner} wins")
    return "  ENTRY  " + "  →  ".join(parts)

# ── Differ ─────────────────────────────────────────────────────────────────────

def diff(prev, curr):
    if not prev or not curr: return []
    changes = []
    pm = {m['matchId']: m for m in prev.get('matches', [])}
    cm = {m['matchId']: m for m in curr.get('matches', [])}
    for mid, m in cm.items():
        p = pm.get(mid)
        if not p: continue
        cs, ps = m.get('scoreText',''), p.get('scoreText','')
        if cs != ps:
            t1, t2 = m.get('team1','?'), m.get('team2','?')
            court  = m.get('courtName','?')
            name   = m.get('shortName','?')
            if m.get('decided'):
                w = t1 if m.get('firstTeamWon') else t2
                changes.append(f"  FINAL  [{court}] {name}  {t1} vs {t2}  →  {cs}  ({w} wins)")
            elif cs:
                changes.append(f"  SCORE  [{court}] {name}  {t1} vs {t2}  →  {cs}")
    return changes

# ── Logging ────────────────────────────────────────────────────────────────────

def ts(): return datetime.datetime.now().strftime('%H:%M:%S')
def log(msg): print(f"  [{ts()}] {msg}")

def show_update(data, raw_size, n, changes):
    print(f"\n{'═'*62}")
    print(f"  UPDATE #{n}  {ts()}  —  {raw_size:,} bytes")
    print(f"{'─'*62}")
    if data:
        matches = data.get('matches', [])
        decided = sum(1 for m in matches if m.get('decided'))
        print(f"  Event:   {data['event'].get('name','')}")
        print(f"  Matches: {len(matches)} total  |  {decided} decided  |  {len(matches)-decided} pending")
        if changes:
            print(f"\n  ── Changes ──")
            for c in changes: print(c)
        else:
            print(f"\n  (no score changes vs previous update)")
    print(f"{'─'*62}")

# ── Main loop ──────────────────────────────────────────────────────────────────

def monitor(cfg):
    host        = cfg.get('aes', 'host',     fallback='127.0.0.1')
    port        = cfg.getint('aes', 'port',  fallback=17471)
    password    = cfg.get('aes', 'password', fallback='')
    base_url    = cfg.get('dashboard', 'endpoint',    fallback='').strip()
    ingest_key  = cfg.get('dashboard', 'ingest_key',  fallback='').strip()
    timeout     = cfg.getint('dashboard', 'timeout',  fallback=10)

    # Resolve bridge path relative to script directory
    bridge_rel = cfg.get('bridge', 'exe', fallback=r'..\bridge\bin\Release\net48\AESBridge.exe')
    bridge_exe = os.path.join(SCRIPT_DIR, bridge_rel)
    if not os.path.exists(bridge_exe):
        log(f"WARNING: AESBridge.exe not found at {bridge_exe}")
        log(f"         Raw binary will be saved but scores won't be parsed or pushed")
        bridge_exe = None

    print(f"\n{'═'*62}")
    print(f"  AES Sync Monitor")
    print(f"{'═'*62}")
    print(f"  AES:       {host}:{port}")
    print(f"  Bridge:    {bridge_exe or 'NOT FOUND'}")
    print(f"  Ingest:    {base_url or '(not configured — set in aes_config.ini)'}")
    print(f"  Press Ctrl+C to stop")
    print(f"{'─'*62}\n")

    prev_data  = None
    update_num = 0

    while True:
        last_snapshot = None  # reset on each new connection → always push snapshot on connect
        log(f"Connecting to {host}:{port}...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.connect((host, port))
            log("Connected — completing handshake...")
            cin = client_init(sock, password)
            sock.settimeout(1.0)   # lets Ctrl+C interrupt blocking recv on Windows
            log("Handshake complete ✓  —  waiting for data...\n")

            while True:
                try:
                    nc   = cin.rcmd()
                    cc   = cin.rint()
                    data = cin.rdata()
                except socket.timeout:
                    continue

                if cc == CMD_EVENT_UPDATE and data:
                    update_num += 1
                    curr    = call_bridge(data, bridge_exe)
                    changes = diff(prev_data, curr)
                    show_update(curr, len(data), update_num, changes)

                    # Push snapshot on first connect or after interval has elapsed
                    if curr and base_url:
                        now = datetime.datetime.now(datetime.timezone.utc)
                        if last_snapshot is None or (now - last_snapshot).total_seconds() >= SNAPSHOT_INTERVAL:
                            push_snapshot(curr, base_url, ingest_key, timeout)
                            last_snapshot = now

                    prev_data = curr

                elif cc == CMD_REMOTE_ENTRY_UPDATE and data:
                    obj    = decode_remote_entry(data, bridge_exe)
                    change = format_remote_entry(obj, prev_data)
                    if change:
                        print(f"  [{ts()}]{change}")
                    else:
                        log(f"RemoteEntryUpdate ({len(data)} bytes)")

                    # Push delta immediately
                    if obj and base_url:
                        push_delta(obj, prev_data, base_url, ingest_key, timeout)

                elif cc in (CMD_FINISHED_PLAYS, CMD_PRINTABLE_MATCHES,
                            CMD_PRINTABLE_PLAYS, CMD_AUTO_PRINT_MATCHES):
                    pass  # auto-print heartbeat — ignore

                else:
                    log(f"← cmd={cc} ({len(data)} bytes)")

        except KeyboardInterrupt:
            log("Shutting down.")
            try: sock.close()
            except: pass
            break
        except ConnectionError as e:
            log(f"Disconnected: {e}")
        except Exception as e:
            log(f"Error: {e}")
        finally:
            try: sock.close()
            except: pass

        log("Reconnecting in 5 seconds...")
        try: time.sleep(5)
        except KeyboardInterrupt:
            log("Shutting down."); break

if __name__ == '__main__':
    cfg = load_config()
    monitor(cfg)