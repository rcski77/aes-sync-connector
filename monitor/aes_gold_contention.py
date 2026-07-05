#!/usr/bin/env python3
"""
Gold-bracket contention trace — diagnostic script, NOT part of the production pipeline
=========================================================================================
Nothing here is sent to the dashboard — this only reads the local
tournament_data.json (via AESBridge.exe) and prints an analysis. The
production ingest payload (aes_monitor.py's _match_payload/_pool_payload/
push_delta/push_snapshot) is untouched and unaffected by any of this.

For each division, determines which bracket produces the division's overall
#1 finisher ("gold"), then answers: for each pool, round by round, how many
of its finishers are still structurally in contention for gold (i.e. their
next-round destination is itself an ancestor of the Gold bracket), versus how
many have been cut to a lower group.

Model (confirmed against real data + Adam, 2026-07-05 — see
gold_contention_model.md in project memory):

  1. divisions[].finalPlaces: the entry with absoluteRank == 1 tells us,
     via its logic name ("Winner of R4GoldM15") or (if the tournament has
     finished) the actual team, which match produces the division champion.
  2. That match is the root of exactly one bracket (brackets[].roots) —
     that bracket is "gold" for this division, whatever it's actually named.
  3. Walk backward from Gold via breadth-first search: for every entrySeed of
     every play in the current frontier, find the play in the nearest
     earlier round whose teamAssignment produced that value as an exitSeed
     (or further back still, if intervening rounds don't touch the seed at
     all — AES's own pass-through rule, Round.cs:264-274). Every play found
     this way is a "gold ancestor." This is NOT the same as tracing a single
     seed's identity back to one Round-1 pool (that model was tried first
     and confirmed wrong — it conflates a dominant seed simply never losing
     with an actual pool-to-pool contention count).
  4. For each pool at each round, a finisher is "still in contention" if
     their own next-round destination play (searched forward the same way,
     skipping rounds that don't touch their exitSeed) is in the gold-ancestor
     set. This correctly handles undersized pools that get cross-paired with
     a sibling pool's crossover bracket (e.g. two 3-team pools where 2nd/3rd
     place play the sibling pool's 3rd/2nd place for the remaining spot) —
     verified against real "XO" crossover brackets in the data.

Usage:
    python aes_gold_contention.py [path-to-binary] [--bridge PATH] [--division CODE]
"""

import os
import sys
import re
import json
import argparse
from collections import defaultdict
import subprocess

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRIDGE = os.path.normpath(os.path.join(
    _BASE_DIR, '..', 'bridge', 'bin', 'Release', 'net48', 'AESBridge.exe'))
DEFAULT_BIN = os.path.join(_BASE_DIR, 'scheduler_file.bin')


def run_bridge(bin_path, bridge_exe):
    if not os.path.exists(bridge_exe):
        print(f"ERROR: AESBridge.exe not found at {bridge_exe}")
        sys.exit(1)
    if not os.path.exists(bin_path):
        print(f"ERROR: Input binary not found at {bin_path}")
        sys.exit(1)

    r = subprocess.run([bridge_exe, bin_path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"ERROR: bridge failed:\n{r.stderr}")
        sys.exit(1)

    json_path = os.path.join(os.path.dirname(os.path.abspath(bin_path)), 'tournament_data.json')
    with open(json_path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def build_items(data, division_code):
    """Flatten this division's pools + brackets into one list, tagged by type,
    each with a stable unique id (poolId/bracketId)."""
    items = []
    for p in data['pools']:
        if p['divisionCode'] == division_code:
            items.append({'type': 'pool', 'id': p['poolId'], 'name': p['name'],
                           'roundIndex': p['roundIndex'], 'groupShortName': p['groupShortName'],
                           'teamAssignments': p['teamAssignments']})
    for b in data['brackets']:
        if b['divisionCode'] == division_code:
            items.append({'type': 'bracket', 'id': b['bracketId'], 'name': b['name'],
                           'roundIndex': b['roundIndex'], 'groupShortName': b['groupShortName'],
                           'teamAssignments': b['teamAssignments']})
    return items


def find_dest_play(items, max_round, seed, from_round):
    """Forward search: the play in the nearest later round whose teamAssignment
    has this seed as its entrySeed. Skips rounds that don't touch the seed."""
    r = from_round + 1
    while r <= max_round:
        for it in items:
            if it['roundIndex'] == r:
                for ta in it['teamAssignments']:
                    if ta['entrySeed'] == seed:
                        return it
        r += 1
    return None


def find_source_play(items, seed, from_round):
    """Backward search: the play in the nearest earlier round whose
    teamAssignment has this seed as its exitSeed. Skips rounds that don't
    touch the seed (AES's pass-through rule)."""
    r = from_round - 1
    while r >= 0:
        for it in items:
            if it['roundIndex'] == r:
                for ta in it['teamAssignments']:
                    if ta['exitSeed'] == seed:
                        return it
        r -= 1
    return None


def find_gold_bracket(data, division_code):
    """Returns (bracket_dict, description) for the bracket whose winner is
    this division's overall #1 finisher, or (None, reason) if it can't be
    determined."""
    division = next((dv for dv in data['divisions'] if dv['divisionCode'] == division_code), None)
    if division is None:
        return None, 'division not found'

    fp1 = next((fp for fp in division['finalPlaces'] if fp['absoluteRank'] == 1), None)
    if fp1 is None or not fp1['team']:
        return None, 'no absoluteRank==1 finalPlace entry (or unresolved)'

    text = fp1['team']
    m = re.match(r'(Winner|Loser) of (\S+)', text)
    if m:
        match_shortname = m.group(2)
        for b in data['brackets']:
            if b['divisionCode'] == division_code:
                for root in b['roots']:
                    if root['match']['shortName'] == match_shortname:
                        return b, text
        return None, f'no bracket root matches "{match_shortname}"'

    # Tournament already finished — team is resolved to a real name. Find the
    # bracket whose root match's decided winner is that team.
    for b in data['brackets']:
        if b['divisionCode'] == division_code:
            for root in b['roots']:
                match = root['match']
                if match['decided']:
                    winner = match['team1'] if match['firstTeamWon'] else match['team2']
                    if winner == text:
                        return b, text
    return None, f'no decided root match won by "{text}"'


def build_gold_ancestors(items, gold):
    """Backward BFS from Gold: a play is a gold ancestor if at least one of
    its teamAssignments' entrySeed traces back to it from a later ancestor."""
    ids_seen = {gold['id']}
    frontier = [gold]
    while frontier:
        nxt = []
        for play in frontier:
            for ta in play['teamAssignments']:
                src = find_source_play(items, ta['entrySeed'], play['roundIndex'])
                if src and src['id'] not in ids_seen:
                    ids_seen.add(src['id'])
                    nxt.append(src)
        frontier = nxt
    return ids_seen


def analyze_division(data, division_code):
    items = build_items(data, division_code)
    if not items:
        print("  No pools/brackets found for this division.")
        return
    max_round = max(it['roundIndex'] for it in items)

    gold_raw, desc = find_gold_bracket(data, division_code)
    if gold_raw is None:
        print(f"  Could not determine gold bracket: {desc}")
        return
    gold = next(it for it in items if it['type'] == 'bracket' and it['id'] == gold_raw['bracketId'])

    print(f"  #1 place resolves via: {desc}")
    print(f"  Gold bracket: {gold['name']}  ({len(gold['teamAssignments'])} teams)")

    gold_ancestors = build_gold_ancestors(items, gold)

    pool_rounds = sorted(set(it['roundIndex'] for it in items if it['type'] == 'pool'))
    for r in pool_rounds:
        pools = [it for it in items if it['roundIndex'] == r and it['type'] == 'pool']
        print(f"\n  -- Round index {r} --")
        by_group = defaultdict(list)
        for p in sorted(pools, key=lambda x: x['name']):
            in_contention = 0
            for ta in p['teamAssignments']:
                if ta['exitSeed'] is None:
                    continue
                dest = find_dest_play(items, max_round, ta['exitSeed'], p['roundIndex'])
                if dest and dest['id'] in gold_ancestors:
                    in_contention += 1
            by_group[p['groupShortName'] or '(no group)'].append(
                (p['name'], len(p['teamAssignments']), in_contention))

        for group, pools_in_group in sorted(by_group.items()):
            print(f"    Group {group}:")
            for name, size, count in pools_in_group:
                marker = f"{count}/{size} still in gold contention" if count < size else f"all {size} still in gold contention"
                print(f"      {name}: {marker}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('binary', nargs='?', default=DEFAULT_BIN,
                     help='Path to SchedulerFile binary (default: scheduler_file.bin)')
    ap.add_argument('--bridge', default=DEFAULT_BRIDGE, help='Path to AESBridge.exe')
    ap.add_argument('--division', default=None,
                     help='Limit to a single division code (default: all divisions)')
    args = ap.parse_args()

    data = run_bridge(args.binary, args.bridge)

    codes = [args.division] if args.division else [dv['divisionCode'] for dv in data['divisions']]
    for code in codes:
        print(f"=== {code} ===")
        analyze_division(data, code)
        print()


if __name__ == '__main__':
    main()
