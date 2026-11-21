"""Cross-check two .vsf files with the independent parser (gavin-aes-scripts/aes_vsf.py, loaded by
path, never copied: it has no license).

    python -I tools/vsf_xcheck.py <before.vsf> <after.vsf> [<payload.bin>]

Used to check bridge --edit / --host-batch output (docs/WRITE_BACK_EDITOR_SCOPING.md).
Prints every changed match/play-slot value, then every other changed path in the whole file
(users excluded: they hold people's details; only their count is compared). Strings are never
printed outside matches' scheduling fields. Optional 3rd arg: a PostEvent payload frame to check."""
import sys, gzip, struct, importlib.util
spec = importlib.util.spec_from_file_location("aes_vsf", r"C:\Git Repos\gavin-aes-scripts\aes_vsf.py")
P = importlib.util.module_from_spec(spec); spec.loader.exec_module(P)

def plays(ev):
    for d in ev["divisions"]:
        for r in d["rounds"]:
            for g in r["groups"]:
                for p in g["plays"]:
                    yield p
                    pb = p.get("playoffBracket")
                    if isinstance(pb, dict) and "playId" in pb: yield pb

def flat(v):
    courts = {c["courtId"]: c["name"] for c in v["courts"]} if v["courts"] and "courtId" in v["courts"][0] else {}
    out = {}
    for p in plays(v["event"]):
        for i, t in enumerate(p.get("teams", [])):
            for k in ("entrySeed", "finishRank", "reseedSeed", "rankWillBeReseeded"):
                out[f"play {p['playId']} slot {i} {k}"] = t.get(k)
        for m in p.get("matches", []):
            k = f"match {m['matchId']}"
            out[k + " court"] = courts.get(m["scheduledCourtId"], m["scheduledCourtId"])
            out[k + " start"] = m["scheduledStart"]
            out[k + " outcome"] = m["outcome"]
            out[k + " sets"] = ",".join(f"{s['firstTeamScore']}-{s['secondTeamScore']}" for s in m["sets"])
            out[k + " work"] = f"{m['workTeamType']}/{m['workTeamNumber']}"
            out[k + " lastScoreEntered set"] = bool(m["lastScoreDateTimeEntered"])
    return out

def walk(a, b, path, diffs):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            walk(a.get(k, "<absent>"), b.get(k, "<absent>"), f"{path}.{k}", diffs)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b): diffs.append(f"{path}: length {len(a)} -> {len(b)}"); return
        for i, (x, y) in enumerate(zip(a, b)): walk(x, y, f"{path}[{i}]", diffs)
    elif a != b:
        shown = "" if isinstance(a, str) or isinstance(b, str) else f": {a} -> {b}"
        diffs.append(path + shown)

a, b = (P.load_vsf_file(x) for x in sys.argv[1:3])
print(f"versions {a['version']} -> {b['version']}; fileId same: {a['fileId'] == b['fileId']}; users {len(a['users'])} -> {len(b['users'])}")
fa, fb = flat(a), flat(b)
ch = [k for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]
print(f"{len(ch)} match/slot value(s) changed:")
for k in ch: print(f"  {k}: {fa.get(k)} -> {fb.get(k)}")
diffs = []
sa = {k: v for k, v in a.items() if k not in ("users", "version")}
sb = {k: v for k, v in b.items() if k not in ("users", "version")}
walk(sa, sb, "", diffs)
print(f"{len(diffs)} raw path(s) differ anywhere in the file:")
for d in diffs: print("  " + d)
if len(sys.argv) > 3:
    fr = open(sys.argv[3], "rb").read()
    n = fr[5] | fr[6] << 8 | fr[7] << 16
    body = fr[9:]; body = gzip.decompress(body) if fr[8] & 1 else body
    out = open(sys.argv[2], "rb").read()
    print(f"payload: ncc=0x{fr[0]:02x} cmd={struct.unpack('<i', fr[1:5])[0]} len ok={n == len(fr) - 9} gzip={fr[8] & 1} "
          f"body {len(body)} bytes, contains the saved file byte-for-byte: {out in body}")
