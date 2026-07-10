#!/usr/bin/env python3
"""
Inspect new AESBridge fields — diagnostic script, NOT part of the production pipeline
=======================================================================================
Runs AESBridge.exe against an existing SchedulerFile binary (default:
scheduler_file.bin in this folder) and prints the three data structures added
to tournament_data.json while researching the pool/bracket tree, final-place,
and seed logic (see docs/WRITE_BACK_EDITOR_SCOPING.md):

  - divisions[].finalPlaces        Division.FinalPlace — gold/silver/bronze
                                    groups and their seed resolution
  - brackets[].roots[].topSource/
    bottomSource                   the bracket tree (already existed in the
                                    schema; shown here for context)
  - pools[]/brackets[].teamAssignments
                                    Play.TeamAssignment seed chain
                                    (EntrySeed -> FinishRank -> ExitSeed)

This exists to look at the data before deciding whether/how to wire it into
the real ingest schema — nothing here is sent anywhere.

Usage:
    python aes_inspect_new_fields.py [path-to-binary] [--bridge PATH]
"""

import os
import sys
import json
import argparse
import subprocess

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRIDGE = os.path.normpath(os.path.join(
    _BASE_DIR, '..', 'bridge', 'bin', 'Release', 'net48', 'AESBridge.exe'))
DEFAULT_BIN = os.path.join(_BASE_DIR, 'scheduler_file.bin')

MATCH_PREVIEW_KEYS = ('shortName', 'team1', 'team2', 'outcome')


def run_bridge(bin_path, bridge_exe):
    if not os.path.exists(bridge_exe):
        print(f"ERROR: AESBridge.exe not found at {bridge_exe}")
        sys.exit(1)
    if not os.path.exists(bin_path):
        print(f"ERROR: Input binary not found at {bin_path}")
        sys.exit(1)

    r = subprocess.run([bridge_exe, bin_path], capture_output=True, text=True, timeout=60)
    print(r.stdout.strip())
    if r.returncode != 0:
        print(f"ERROR: bridge failed:\n{r.stderr}")
        sys.exit(1)

    json_path = os.path.join(os.path.dirname(os.path.abspath(bin_path)), 'tournament_data.json')
    with open(json_path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def print_divisions(data):
    print("\n" + "=" * 70)
    print("DIVISIONS / FINAL PLACES  (Division.FinalPlaces)")
    print("=" * 70)
    divisions = data.get('divisions', [])
    print(f"{len(divisions)} division(s) found.\n")
    for div in divisions:
        fps = div['finalPlaces']
        named = [fp for fp in fps if fp['groupName']]
        print(f"- {div['divisionCode']} ({div['divisionName']}): "
              f"{len(fps)} finalPlace entries, {len(named)} with a groupName set")
        for fp in fps[:5]:
            print(f"    {fp}")
        if len(fps) > 5:
            print(f"    ... ({len(fps) - 5} more)")
        print()


def _match_preview(match):
    return {k: match.get(k) for k in MATCH_PREVIEW_KEYS}


def _truncate(node, levels):
    if node is None or levels < 0:
        return None
    return {
        'match': _match_preview(node['match']),
        'topSource': _truncate(node.get('topSource'), levels - 1) if levels > 0 else '...',
        'bottomSource': _truncate(node.get('bottomSource'), levels - 1) if levels > 0 else '...',
    }


def _tree_depth(node, depth=0):
    if node is None:
        return depth
    return max(_tree_depth(node.get('topSource'), depth + 1),
               _tree_depth(node.get('bottomSource'), depth + 1))


def print_bracket_tree(data):
    print("=" * 70)
    print("BRACKET TREE  (Bracket.PlotMatchPositions -> topSource/bottomSource)")
    print("=" * 70)
    brackets = data.get('brackets', [])
    print(f"{len(brackets)} bracket(s) found.\n")
    if not brackets:
        return

    sample = max(brackets, key=lambda b: b['matchCount'])
    print(f"Deepest example: {sample['name']} ({sample['divisionCode']}), "
          f"{sample['matchCount']} matches, {len(sample['roots'])} root(s)")
    for root in sample['roots']:
        print(f"  root match {root['match']['shortName']}: tree depth {_tree_depth(root)}")

    if sample['roots']:
        print("\n  First root node, truncated to 2 levels:")
        root = sample['roots'][0]
        preview = {
            'match': _match_preview(root['match']),
            'topSource': _truncate(root.get('topSource'), 1),
            'bottomSource': _truncate(root.get('bottomSource'), 1),
        }
        print(json.dumps(preview, indent=2))
    print()


def print_team_assignments(data):
    print("=" * 70)
    print("TEAM ASSIGNMENTS  (Play.TeamAssignment seed chain)")
    print("=" * 70)
    pools = data.get('pools', [])
    brackets = data.get('brackets', [])
    print(f"{len(pools)} pool(s), {len(brackets)} bracket(s).\n")

    if pools:
        p = pools[0]
        print(f"Example pool: {p['name']} ({p['divisionCode']})")
        for ta in p['teamAssignments']:
            print(f"    {ta}")
        print()

    if brackets:
        b = brackets[0]
        print(f"Example bracket: {b['name']} ({b['divisionCode']})")
        for ta in b['teamAssignments'][:8]:
            print(f"    {ta}")
        if len(b['teamAssignments']) > 8:
            print(f"    ... ({len(b['teamAssignments']) - 8} more)")
        print()


def build_preview(data):
    """Small standalone JSON with just the new sections plus a few samples,
    for browsing in an editor without wading through the full multi-MB output."""
    return {
        'divisions': data.get('divisions', []),
        'pools_sample': data.get('pools', [])[:3],
        'brackets_sample': data.get('brackets', [])[:3],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('binary', nargs='?', default=DEFAULT_BIN,
                     help='Path to SchedulerFile binary (default: scheduler_file.bin)')
    ap.add_argument('--bridge', default=DEFAULT_BRIDGE, help='Path to AESBridge.exe')
    args = ap.parse_args()

    print(f"Binary: {args.binary}")
    print(f"Bridge: {args.bridge}\n")

    data = run_bridge(args.binary, args.bridge)

    print_divisions(data)
    print_bracket_tree(data)
    print_team_assignments(data)

    preview_path = os.path.join(_BASE_DIR, 'new_fields_preview.json')
    with open(preview_path, 'w', encoding='utf-8') as f:
        json.dump(build_preview(data), f, indent=2)
    print(f"Full preview (all divisions + 3 sample pools/brackets) written to:\n  {preview_path}")


if __name__ == '__main__':
    main()
