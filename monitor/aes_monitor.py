#!/usr/bin/env python3
"""
AES Sync Monitor — production version
=======================================
Connects to AES Scheduler on port 17471, receives real-time tournament
updates, parses them via AESBridge.exe, and POSTs JSON to your dashboard.

Config: aes_config.ini (in the same folder as this script), or a
dashboard-downloaded connector-config-<eventId>.ini if aes_config.ini
isn't present.
Usage:  python aes_monitor.py
"""

import os, sys, socket, struct, hashlib, base64, gzip, zlib, glob
import subprocess, json, datetime, threading, time, re, queue
import configparser, urllib.request, urllib.error
import xml.etree.ElementTree as ET

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
except ImportError:
    print("ERROR: pip install pycryptodome"); sys.exit(1)

# When frozen by PyInstaller, __file__ points into the temp extraction folder.
# Use sys.executable instead so config/data files are found next to the .exe.
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Config ─────────────────────────────────────────────────────────────────────

def find_config_path():
    """Look for aes_config.ini first, then fall back to a dashboard-downloaded
    connector-config-<eventId>.ini dropped in the same folder unrenamed."""
    default_path = os.path.join(_BASE_DIR, 'aes_config.ini')
    if os.path.exists(default_path):
        return default_path
    matches = glob.glob(os.path.join(_BASE_DIR, 'connector-config-*.ini'))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"ERROR: Multiple connector-config-*.ini files found in {_BASE_DIR} — keep only one.")
        sys.exit(1)
    return default_path


def load_config():
    cfg = configparser.ConfigParser()
    cfg_path = find_config_path()
    if not os.path.exists(cfg_path):
        print(f"ERROR: Config file not found: {cfg_path}")
        print(f"       Place aes_config.ini (or a downloaded connector-config-*.ini) in this folder.")
        sys.exit(1)
    cfg.read(cfg_path)
    print(f"  Config:    {cfg_path}")
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
OUTCOME_CODES = {v: k for k, v in OUTCOMES.items()}

# Match.WorkTeamType enum order (index == the int AES expects) — see
# WRITE_BACK_EDITOR_SCOPING.md / CLAUDE.md "Score Write-Back".
WORK_TEAM_TYPES = ['None', 'NextHigher', 'NextLower', 'PreviousWinner', 'PreviousLoser',
                    'InternalAbsolute', 'ExternalAbsolute', 'CustomText',
                    'AnotherMatchWinner', 'AnotherMatchLoser']
WORK_TEAM_TYPE_CODE = {name: i for i, name in enumerate(WORK_TEAM_TYPES)}

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
    def send(self, nc, cc, data=None):
        self.wcmd(nc); self.w(struct.pack('<I', cc)); self.wdata(data)

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
    return cin, cout

# ── Bridge ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = _BASE_DIR

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

# ── Write-back (outbox) ─────────────────────────────────────────────────────────
# Opt-in (aes_config.ini [aes] allow_writeback=true, default false). Lets the
# dashboard push score corrections into AES over the same connection. See
# docs/DASHBOARD_OUTBOX_API.md for the endpoint contract and
# docs/WRITE_BACK_EDITOR_SCOPING.md for why this approach (a live
# RemoteEntryUpdate write, not a .vsf file editor) was chosen.
#
# Only the main loop thread (the one already running cin's blocking recv) ever
# calls cout.send() — poll_outbox only performs HTTP GETs against the dashboard
# and hands parsed commands to the main loop via a thread-safe queue.

def poll_outbox(base_url, ingest_key, timeout, poll_interval, out_q, stop_event):
    """Background thread: GET pending score-correction commands from the
    dashboard and push them onto out_q for the main loop to apply. Never
    touches the AES socket."""
    url = base_url.rstrip('/') + '/outbox'
    while not stop_event.is_set():
        try:
            req = urllib.request.Request(url, headers={
                'Authorization': f'Bearer {ingest_key}',
                'User-Agent':    'aes-sync-connector/1.0',
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                for cmd in body.get('commands', []):
                    out_q.put(cmd)
        except Exception as e:
            log(f"outbox poll failed: {e}")
        stop_event.wait(poll_interval)


def _merge_for_encode(cmd, tournament_data, file_id):
    """Build the --encode-remote CLI args for one outbox command, merging with
    the match's last-known full state. AES's UpdateWithRemoteEntryData
    overwrites WorkTeamNumber/TypeOfWorkTeam unconditionally and clears any
    Sets slot beyond what's supplied — so workTeamNumber/typeOfWorkTeam always
    come from our own cached match info (never the incoming command; work-team
    reassignment is out of scope), and `sets` falls back to the cached full
    set list when the command omits it (an outcome-only correction)."""
    match_id = cmd['matchId']
    match_info = {}
    for m in (tournament_data or {}).get('matches', []):
        if m.get('matchId') == match_id:
            match_info = m
            break

    outcome_name = cmd.get('outcome') or match_info.get('outcome')
    outcome_code = OUTCOME_CODES.get(outcome_name, '0')

    sets = cmd.get('sets')
    if sets is None:
        sets = match_info.get('sets', [])

    work_team_number  = match_info.get('workTeamNumber', 0)
    type_of_work_team = WORK_TEAM_TYPE_CODE.get(match_info.get('typeOfWorkTeam', 'None'), 0)

    args = [file_id, str(match_id), str(outcome_code), str(work_team_number), str(type_of_work_team)]
    for s in sets:
        args += [str(s['team1']), str(s['team2'])]
    return args


def send_score_correction(cmd, cout, tournament_data, file_id, bridge_exe):
    """Encode and send one outbox command into AES over the live connection.
    Returns (status, detail): 'applied' (sent, no local error), 'rejected'
    (bridge validation failed — malformed input, don't retry as-is), or
    'failed' (transient — AES not connected yet, subprocess timeout, fileId
    not yet known — safe to retry)."""
    if not bridge_exe:
        return 'failed', 'AESBridge.exe not available'
    if not file_id:
        return 'failed', 'fileId not yet captured — waiting for first EventUpdate'
    try:
        args = _merge_for_encode(cmd, tournament_data, file_id)
    except Exception as e:
        return 'rejected', f'bad outbox command: {e}'

    out_path = os.path.join(SCRIPT_DIR, 'remote_entry_out.bin')
    try:
        r = subprocess.run(
            [bridge_exe, '--encode-remote', out_path] + args,
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return 'rejected', r.stderr.strip()[:200]
        with open(out_path, 'rb') as f:
            payload = f.read()
        cout.send(NCC_OBJECT, CMD_REMOTE_ENTRY_UPDATE, payload)
        return 'applied', None
    except Exception as e:
        return 'failed', str(e)


def _ack_outbox(base_url, ingest_key, timeout, cmd_id, status, detail, cf_headers=None):
    """POST the apply result back to the dashboard. Runs in a daemon thread,
    like the delta/snapshot pushes."""
    url = base_url.rstrip('/') + f'/outbox/{cmd_id}/ack'
    _post(url, {'status': status, 'detail': detail}, ingest_key, timeout, f'outbox-ack({cmd_id})', cf_headers)

# ── Dashboard push ─────────────────────────────────────────────────────────────

SNAPSHOT_INTERVAL = 180  # seconds between full snapshot POSTs

def _post(url, payload_obj, ingest_key, timeout, label, cf_headers=None):
    """Serialize and POST a payload; log result. Runs in a daemon thread."""
    try:
        payload = json.dumps(payload_obj).encode('utf-8')
        headers = {
            'Content-Type':   'application/json',
            'Content-Length': str(len(payload)),
            'User-Agent':     'aes-sync-connector/1.0',
        }
        if ingest_key:
            headers['Authorization'] = f'Bearer {ingest_key}'
        if cf_headers:
            headers.update(cf_headers)
        req = urllib.request.Request(
            url,
            data    = payload,
            method  = 'POST',
            headers = headers,
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
        'playId':    m.get('playId'),
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


def _server_safe_key(event_id, manual_addition, event_name):
    """Derive AES's web API event ID string (e.g. 'PTAwMDAwNDUwMzE90') from the
    numeric eventId. Verified against gavin-aes-scripts sample data:
    eventId 45031, manual_addition=False -> 'PTAwMDAwNDUwMzE90'."""
    if manual_addition:
        token = ''.join(ch for ch in (event_name or '') if ch.isalnum())
    else:
        token = f'={event_id:010d}='
    data = token.encode('utf-8')
    b64 = base64.b64encode(data).decode('ascii')
    length = len(b64)
    while length > 0 and b64[length - 1] == '=':
        length -= 1
    chars = []
    for i in range(length):
        c = b64[i]
        chars.append('-' if c == '+' else '_' if c == '/' else c)
    chars.append(chr(48 + len(b64) - length))
    return ''.join(chars)


def _event_id_key(tournament_data):
    """Compute aesEventIdKey from a tournament_data.json dict, or None if unavailable."""
    event_info = tournament_data.get('event', {}) if tournament_data else {}
    event_id = event_info.get('eventId')
    if event_id is None:
        return None
    return _server_safe_key(event_id, event_info.get('manualAddition', False), event_info.get('name', ''))


def _build_play_items(tournament_data, division_code):
    """Flatten one division's pools + brackets into a list tagged by type, each
    with a stable unique id (poolId/bracketId) — mirrors build_items() in
    aes_gold_contention.py."""
    items = []
    for p in tournament_data.get('pools', []):
        if p.get('divisionCode') == division_code:
            items.append({'type': 'pool', 'id': p.get('poolId'),
                          'roundIndex': p.get('roundIndex'),
                          'teamAssignments': p.get('teamAssignments', [])})
    for b in tournament_data.get('brackets', []):
        if b.get('divisionCode') == division_code:
            items.append({'type': 'bracket', 'id': b.get('bracketId'),
                          'roundIndex': b.get('roundIndex'),
                          'teamAssignments': b.get('teamAssignments', [])})
    return items


def _find_dest_play(items, max_round, seed, from_round):
    """Forward search: the play in the nearest later round whose teamAssignment
    has this seed as its entrySeed. Skips rounds that don't touch the seed."""
    r = from_round + 1
    while r <= max_round:
        for it in items:
            if it['roundIndex'] == r:
                for ta in it['teamAssignments']:
                    if ta.get('entrySeed') == seed:
                        return it
        r += 1
    return None


def _find_source_play(items, seed, from_round):
    """Backward search: the play in the nearest earlier round that could
    produce this seed as one of its own exits. Searches entrySeed, not
    exitSeed: a play's own exitSeed multiset is always the same set of values
    as its own entrySeed multiset (barring reseeds, out of scope here) — AES
    redistributes the same seed numbers it received among its own finishers
    once standings are known, never introduces new ones. entrySeed is
    structural (assigned when the bracket/schedule is built) and available
    before any match in that play is decided, unlike exitSeed (which requires
    finish rank, i.e. the play to have actually been played). Verified
    against real data: pool -60263's entrySeeds [14,49,76,111] and exitSeeds
    [14,76,49,111] are the same set, just permuted across physical teams.
    Skips rounds that don't touch the seed (AES's pass-through rule)."""
    r = from_round - 1
    while r >= 0:
        for it in items:
            if it['roundIndex'] == r:
                for ta in it['teamAssignments']:
                    if ta.get('entrySeed') == seed:
                        return it
        r -= 1
    return None


def _find_gold_bracket_id(tournament_data, division_code):
    """Returns the bracketId whose winner is this division's overall #1
    finisher, or None if it can't be determined (mirrors find_gold_bracket()
    in aes_gold_contention.py)."""
    division = next((dv for dv in tournament_data.get('divisions', [])
                      if dv.get('divisionCode') == division_code), None)
    if division is None:
        return None
    fp1 = next((fp for fp in division.get('finalPlaces', [])
                 if fp.get('absoluteRank') == 1), None)
    if fp1 is None or not fp1.get('team'):
        return None

    text = fp1['team']
    m = re.match(r'(Winner|Loser) of (\S+)', text)
    if m:
        match_shortname = m.group(2)
        for b in tournament_data.get('brackets', []):
            if b.get('divisionCode') == division_code:
                for root in b.get('roots', []):
                    if (root.get('match') or {}).get('shortName') == match_shortname:
                        return b.get('bracketId')
        return None

    # Tournament already finished — team is resolved to a real name. Find the
    # bracket whose root match's decided winner is that team.
    for b in tournament_data.get('brackets', []):
        if b.get('divisionCode') == division_code:
            for root in b.get('roots', []):
                match = root.get('match') or {}
                if match.get('decided'):
                    winner = match.get('team1') if match.get('firstTeamWon') else match.get('team2')
                    if winner == text:
                        return b.get('bracketId')
    return None


def _build_gold_ancestors(items, gold_item):
    """Backward BFS from Gold: a play is a gold ancestor if at least one of
    its teamAssignments' entrySeed traces back to it from a later ancestor."""
    ids_seen = {gold_item['id']}
    frontier = [gold_item]
    while frontier:
        nxt = []
        for play in frontier:
            for ta in play['teamAssignments']:
                seed = ta.get('entrySeed')
                if seed is None:
                    continue
                src = _find_source_play(items, seed, play['roundIndex'])
                if src and src['id'] not in ids_seen:
                    ids_seen.add(src['id'])
                    nxt.append(src)
        frontier = nxt
    return ids_seen


def _compute_gold_spots(tournament_data):
    """Returns {poolId: goldSpotsCount} — for each pool, how many of its
    finishers are structurally still in contention for the division's gold
    bracket (their next-round destination play is an ancestor of Gold).
    Ported from the validated aes_gold_contention.py diagnostic — see
    gold_contention_model.md in project memory. NOT a per-division constant:
    a 4-team pool can have 0-4 teams still in contention depending on round
    and how AES's own bracket structure was built.

    Uses each finisher slot's entrySeed (the seed the pool itself received),
    not exitSeed (the seed a specific physical team is assigned once
    standings are final) — a pool's own exitSeed multiset is always the same
    set of values as its own entrySeed multiset (barring reseeds), so
    entrySeed gives the identical count while also being structural: known
    as soon as the bracket/schedule is built, not only after the pool is
    played. A first version used exitSeed and returned 0 (not null) for
    every pool in a freshly-started event, because none of that event's
    exitSeeds were assigned yet — Adam caught this in production."""
    result = {}
    for div in tournament_data.get('divisions', []):
        code = div.get('divisionCode')
        items = _build_play_items(tournament_data, code)
        if not items:
            continue
        round_indexes = [it['roundIndex'] for it in items if it['roundIndex'] is not None]
        if not round_indexes:
            continue
        max_round = max(round_indexes)

        gold_id = _find_gold_bracket_id(tournament_data, code)
        if gold_id is None:
            continue
        gold_item = next((it for it in items if it['type'] == 'bracket' and it['id'] == gold_id), None)
        if gold_item is None:
            continue

        gold_ancestors = _build_gold_ancestors(items, gold_item)

        for it in items:
            if it['type'] != 'pool':
                continue
            in_contention = 0
            resolved = False  # True once at least one finisher slot has a known seed
            for ta in it['teamAssignments']:
                seed = ta.get('entrySeed')
                if seed is None:
                    continue
                resolved = True
                dest = _find_dest_play(items, max_round, seed, it['roundIndex'])
                if dest and dest['id'] in gold_ancestors:
                    in_contention += 1
            # A pool with no entrySeed at all yet (e.g. a later-round bracket slot
            # AES hasn't structurally seeded yet) stays unset -> null downstream,
            # not 0 — 0 means "confirmed nobody from this pool advances."
            if resolved:
                result[it['id']] = in_contention
    return result


def _pool_payload(p, gold_spots_map=None):
    """Map a tournament_data.json pool dict to the ingest API pool shape."""
    standings = p.get('standings', [])
    # teamAssignments carries AES's real exitSeed per team (null until AES has
    # officially finalized the pool's standings in Scheduler) — keyed by the
    # same raw TeamText used in standings[].team, so a direct name match joins
    # them. Not derived/guessed here; passed through only once AES reports it.
    exit_seed_by_team = {
        ta.get('team'): ta.get('exitSeed')
        for ta in p.get('teamAssignments', [])
        if ta.get('team')
    }
    teams = []
    for st in standings:
        pts_against = st.get('ptsAgainst', 0)
        pts_for     = st.get('ptsFor', 0)
        ratio = round(pts_for / pts_against, 4) if pts_against else None
        raw_name = st.get('team', '')
        teams.append({
            'name':        _strip_seed(raw_name),
            'matchesWon':  st.get('wins', 0),
            'matchesLost': st.get('losses', 0),
            'setsWon':     st.get('setsWon', 0),
            'setsLost':    st.get('setsLost', 0),
            'pointRatio':  ratio,
            'finishRank':  st.get('finishRank'),
            'exitSeed':    exit_seed_by_team.get(raw_name),
        })
    courts = p.get('courts') or []
    first_court = courts[0] if courts else {}
    return {
        'playId':          p.get('poolId'),
        'division':        p.get('divisionName', ''),
        'divisionId':      p.get('divisionId'),
        'name':            p.get('name', ''),
        'shortName':       p.get('fullShortName') or p.get('shortName', ''),
        'courtId':         first_court.get('courtId'),
        'courtName':       first_court.get('name', ''),
        'courts':          courts,
        'date':            p.get('date', ''),
        'goldSpotsCount':  (gold_spots_map or {}).get(p.get('poolId')),
        'teams':           teams,
    }


def push_delta(entry_obj, tournament_data, base_url, ingest_key, timeout, cf_headers=None):
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
        if not match_info:
            log(f"WARNING: delta for matchId={match_id} not found in snapshot — bridge may have failed on last EventUpdate")

    event_info = tournament_data.get('event', {}) if tournament_data else {}
    event_id = str(event_info['eventId']) if event_info.get('eventId') is not None else ''

    payload = {
        'aesEventId':    event_id,
        'aesEventIdKey': _event_id_key(tournament_data),
        'match': {
            'matchId':   match_id,
            'playId':    match_info.get('playId'),
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
    threading.Thread(target=_post, args=(url, payload, ingest_key, timeout, 'delta', cf_headers), daemon=True).start()


def _bracket_node_payload(node):
    """Map a tournament_data.json bracket roots[]/topSource/bottomSource node to the
    ingest API bracket match-node shape. Recurses through the full tree (not just
    final + semis) since the bracket-tree flyout UI needs every round's wiring."""
    if not node:
        return None
    m = node.get('match') or {}
    outcome = m.get('outcome', 'Undecided')
    return {
        'matchId':            m.get('matchId'),
        'matchName':          m.get('fullName', ''),
        'firstTeam':          m.get('team1', ''),
        'secondTeam':         m.get('team2', ''),
        'firstTeamWon':       bool(m.get('firstTeamWon')),
        'secondTeamWon':      outcome == 'SecondTeamWon',
        'court':              m.get('courtName', ''),
        'scheduledStartTime': _eastern_naive(m.get('startTime', '')),
        'topSource':          _bracket_node_payload(node.get('topSource')),
        'bottomSource':       _bracket_node_payload(node.get('bottomSource')),
    }


def _bracket_payload(b):
    """Map a tournament_data.json bracket dict to the ingest API brackets[] shape.
    Returns None (caller skips) for pool-owned tiebreaker brackets (isPlayoff —
    see IsFromPlayoffBracket in bridge/CLAUDE.md, these aren't real divisional
    brackets) and for brackets AES hasn't seeded yet (no root match)."""
    if b.get('isPlayoff'):
        return None
    roots = b.get('roots') or []
    if not roots or not roots[0].get('match'):
        return None
    root = _bracket_node_payload(roots[0])
    return {
        'division':         b.get('divisionName', ''),
        'date':             (root.get('scheduledStartTime') or '').split('T')[0],
        'bracketFullName':  b.get('fullName') or b.get('name', ''),
        'bracketShortName': b.get('shortName') or b.get('name', ''),
        'root':             root,
    }


def push_snapshot(tournament_data, base_url, ingest_key, timeout, cf_headers=None):
    """POST full tournament state to /api/ingest/snapshot."""
    if not base_url or not tournament_data:
        return
    event_id = str(tournament_data['event']['eventId'])
    brackets = [bp for b in tournament_data.get('brackets', [])
                if (bp := _bracket_payload(b)) is not None]
    gold_spots_map = _compute_gold_spots(tournament_data)
    payload = {
        'aesEventId':    event_id,
        'aesEventIdKey': _event_id_key(tournament_data),
        'snapshotTime':  datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'matches':       [_match_payload(m) for m in tournament_data.get('matches', [])],
        'pools':         [_pool_payload(p, gold_spots_map) for p in tournament_data.get('pools', [])],
        'brackets':      brackets,
    }
    url = base_url.rstrip('/') + '/snapshot'
    threading.Thread(target=_post, args=(url, payload, ingest_key, timeout, 'snapshot', cf_headers), daemon=True).start()

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
    base_url    = cfg.get('dashboard', 'endpoint',        fallback='').strip()
    ingest_key  = cfg.get('dashboard', 'ingest_key',      fallback='').strip()
    timeout     = cfg.getint('dashboard', 'timeout',      fallback=10)
    cf_id       = cfg.get('dashboard', 'cf_client_id',    fallback='').strip()
    cf_secret   = cfg.get('dashboard', 'cf_client_secret',fallback='').strip()
    cf_headers  = {'CF-Access-Client-Id': cf_id, 'CF-Access-Client-Secret': cf_secret} if cf_id else None
    allow_writeback       = cfg.getboolean('aes', 'allow_writeback', fallback=False)
    outbox_poll_interval  = cfg.getint('dashboard', 'outbox_poll_interval', fallback=5)

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
    print(f"  Write-back: {'ENABLED — dashboard corrections will be sent into AES' if allow_writeback else 'disabled (read-only)'}")
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
            cin, cout = client_init(sock, password)
            sock.settimeout(1.0)   # lets Ctrl+C interrupt blocking recv on Windows
            log("Handshake complete ✓  —  waiting for data...\n")

            current_file_id = None
            outbox_q  = queue.Queue()
            stop_poll = threading.Event()
            if allow_writeback and base_url:
                threading.Thread(
                    target=poll_outbox,
                    args=(base_url, ingest_key, timeout, outbox_poll_interval, outbox_q, stop_poll),
                    daemon=True,
                ).start()

            while True:
                try:
                    nc   = cin.rcmd()
                    cc   = cin.rint()
                    data = cin.rdata()
                except socket.timeout:
                    while allow_writeback and not outbox_q.empty():
                        cmd = outbox_q.get_nowait()
                        status, detail = send_score_correction(cmd, cout, prev_data, current_file_id, bridge_exe)
                        threading.Thread(
                            target=_ack_outbox,
                            args=(base_url, ingest_key, timeout, cmd.get('id'), status, detail, cf_headers),
                            daemon=True,
                        ).start()
                    continue

                if cc == CMD_EVENT_UPDATE and data:
                    update_num += 1
                    curr    = call_bridge(data, bridge_exe)
                    changes = diff(prev_data, curr)
                    show_update(curr, len(data), update_num, changes)

                    if curr and curr.get('event', {}).get('fileId'):
                        current_file_id = curr['event']['fileId']

                    # Push snapshot on first connect or after interval has elapsed
                    if curr and base_url:
                        now = datetime.datetime.now(datetime.timezone.utc)
                        if last_snapshot is None or (now - last_snapshot).total_seconds() >= SNAPSHOT_INTERVAL:
                            push_snapshot(curr, base_url, ingest_key, timeout, cf_headers)
                            last_snapshot = now

                    if curr is not None:
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
                        push_delta(obj, prev_data, base_url, ingest_key, timeout, cf_headers)

                elif cc in (CMD_FINISHED_PLAYS, CMD_PRINTABLE_MATCHES,
                            CMD_PRINTABLE_PLAYS, CMD_AUTO_PRINT_MATCHES):
                    pass  # auto-print heartbeat — ignore

                else:
                    log(f"← cmd={cc} ({len(data)} bytes)")

        except KeyboardInterrupt:
            log("Shutting down.")
            try: stop_poll.set()
            except NameError: pass
            try: sock.close()
            except: pass
            break
        except ConnectionError as e:
            log(f"Disconnected: {e}")
        except Exception as e:
            log(f"Error: {e}")
        finally:
            try: stop_poll.set()
            except NameError: pass
            try: sock.close()
            except: pass

        log("Reconnecting in 5 seconds...")
        try: time.sleep(5)
        except KeyboardInterrupt:
            log("Shutting down."); break

if __name__ == '__main__':
    cfg = load_config()
    monitor(cfg)