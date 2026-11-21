/*
 * VsfEditTool.cs  —  test modes for writing .vsf files through AES's own code
 *
 * Answers whether a .vsf saved outside the AES Scheduler opens cleanly in it
 * (docs/WRITE_BACK_EDITOR_SCOPING.md, "Recommended next step"). Dev/test only:
 * the monitor never calls these modes.
 *
 *   AESBridge.exe --roundtrip <in.vsf> <out.vsf>
 *       Load with AES's SchedulerFile.Load, save with AES's SchedulerFile.Save,
 *       no changes. Reports whether the output is byte-identical to the input
 *       and whether a second load/save of the output is stable.
 *
 *   AESBridge.exe --edit <in.vsf> <out.vsf> <op> [<op> ...]
 *       Same, after applying edits through AES's own public model:
 *         team-name:<gaggleId>=<new name>
 *         match-name:<matchId>=<new full name>
 *         score:<matchId>=<ft>-<st>[,<ft>-<st>...]     (via UpdateWithRemoteEntryData)
 *         move:<matchId>=<court name>@<yyyy-MM-ddTHH:mm> (via Court.ScheduleMatch)
 *       Then reloads the output and checks every edit is still there.
 *
 *   AESBridge.exe --ids <in.vsf>
 *       Lists team GaggleIDs and codes, courts, and matches (id, court, start,
 *       length, sets, outcome) to pick edit targets from.
 *
 * "Same data" allows one difference: AES keeps users in a Hashtable, so their
 * order changes on every load and save, AES's own saves included.
 *
 * Caching is never enabled on a file we save: AES itself only calls
 * EnableCaching() on throwaway copies (its report screens), so cached values
 * could go stale after an edit.
 *
 * Only ever run against a COPY of a finished event. The copy keeps the real
 * event's FileID; publishing it would overwrite that event. When opening a test
 * file in AES, block that AES copy's network first: AES contacts its sync
 * service on every file open, even with nobody signed in.
 */

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using AES.Scheduler.Model;

static class VsfEditTool
{
    const int MatchDataType = 33281; // RemoteEntryUpdateType.MatchData

    public static int RoundTrip(string[] args)
    {
        if (args.Length != 2) { Console.Error.WriteLine("Usage: --roundtrip <in.vsf> <out.vsf>"); return 2; }
        if (!CheckPaths(args[0], args[1])) return 2;

        byte[] input = File.ReadAllBytes(args[0]);
        SchedulerFile sf = HostBatch.LoadChecked(input, args[0]);
        if (sf == null) return 1;
        Describe(sf, input);

        byte[] output = sf.Save(args[1]);
        Console.WriteLine($"Saved:     {Path.GetFullPath(args[1])}");
        Console.WriteLine($"Identical: {SameData(input, output)}");
        Console.WriteLine($"Stable:    {StableOnReload(output)}");
        return 0;
    }

    public static int Edit(string[] args)
    {
        if (args.Length < 3) { Console.Error.WriteLine("Usage: --edit <in.vsf> <out.vsf> <op> [<op> ...]"); return 2; }
        if (!CheckPaths(args[0], args[1])) return 2;

        byte[] input = File.ReadAllBytes(args[0]);
        SchedulerFile sf = HostBatch.LoadChecked(input, args[0]);
        if (sf == null) return 1;
        Describe(sf, input);

        var checks = new List<Func<SchedulerFile, string>>();
        foreach (string op in args.Skip(2))
        {
            string error = Apply(sf, op, checks);
            if (error != null) { Console.Error.WriteLine($"Edit failed ({op}): {error}"); return 1; }
            Console.WriteLine($"Applied:   {op}");
        }

        byte[] output = sf.Save(args[1]);
        Console.WriteLine($"Saved:     {Path.GetFullPath(args[1])}");
        Console.WriteLine($"Size:      {input.Length} -> {output.Length} bytes");
        Console.WriteLine($"Stable:    {StableOnReload(output)}");

        SchedulerFile reloaded = SchedulerFile.Load(File.ReadAllBytes(args[1]));
        int failed = 0;
        foreach (var check in checks)
        {
            string problem = check(reloaded);
            if (problem == null) continue;
            Console.WriteLine($"MISSING:   {problem}");
            failed++;
        }
        Console.WriteLine(failed == 0 ? "Reload:    every edit present" : $"Reload:    {failed} edit(s) not present");

        // For each scored match, what the matches it feeds show now, per AES's own team text.
        SchedulerFile before = SchedulerFile.Load(input);
        foreach (int id in Scored(args.Skip(2)))
        {
            Match b = before.GetMatchByID(id), r = reloaded.GetMatchByID(id);
            if (b == null || r == null) continue;
            ShowFeed("winner", b.WinnerDestinationMatch, r.WinnerDestinationMatch);
            ShowFeed("loser", b.LoserDestinationMatch, r.LoserDestinationMatch);
        }
        return failed == 0 ? 0 : 1;
    }

    // Ids to pick edit targets from: team GaggleIDs and codes (no names), courts, matches.
    public static int Ids(string[] args)
    {
        if (args.Length != 1 || !File.Exists(args[0])) { Console.Error.WriteLine("Usage: --ids <in.vsf>"); return 2; }
        SchedulerFile sf = HostBatch.LoadChecked(File.ReadAllBytes(args[0]), args[0]);
        if (sf == null) return 1;
        Console.WriteLine("TEAMS  gaggleId  code");
        foreach (Team t in sf.Teams) Console.WriteLine($"  {t.GaggleID}  {t.Code}");
        Console.WriteLine("COURTS");
        foreach (Court c in sf.Courts) Console.WriteLine($"  {c.Name}");
        Console.WriteLine("MATCHES  matchId  court  start  minutes  sets  outcome  play");
        foreach (Match m in sf.Event.Matches.OrderBy(x => x.ScheduledCourt?.Name).ThenBy(x => x.ScheduledStartDateTime))
            Console.WriteLine($"  {m.MatchID}  {m.ScheduledCourt?.Name ?? "-"}  " +
                (m.ScheduledCourt == null ? "-" : m.ScheduledStartDateTime.ToString("yyyy-MM-ddTHH:mm", CultureInfo.InvariantCulture)) +
                $"  {m.MatchLength}  {m.Sets.Length}  {m.TypeOfOutcome}  {m.OwningPlay.CompleteShortName}");
        return 0;
    }

    // ── edits ──────────────────────────────────────────────────────────────

    internal static string Apply(SchedulerFile sf, string op, List<Func<SchedulerFile, string>> checks)
    {
        int colon = op.IndexOf(':');
        int eq = op.IndexOf('=');
        if (colon < 1 || eq < colon + 2) return "expected <kind>:<id>=<value>";
        string kind = op.Substring(0, colon);
        string idText = op.Substring(colon + 1, eq - colon - 1);
        string value = op.Substring(eq + 1);
        if (!int.TryParse(idText, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out int id))
            return $"'{idText}' is not a number";

        switch (kind)
        {
            case "team-name":
            {
                Team team = sf.Teams.FirstOrDefault(t => t.GaggleID == id);
                if (team == null) return $"no team with GaggleID {id}";
                if (value.Trim() == "") return "empty name";
                team.Name = value;
                checks.Add(f => f.Teams.FirstOrDefault(t => t.GaggleID == id)?.Name == value
                    ? null : $"team {id} name");
                return null;
            }

            case "match-name":
            {
                Match m = sf.GetMatchByID(id);
                if (m == null) return $"no match {id}";
                if (value.Trim() == "") return "empty name";
                m.FullName = value;
                checks.Add(f => f.GetMatchByID(id)?.FullName == value ? null : $"match {id} name");
                return null;
            }

            case "score":
            {
                Match m = sf.GetMatchByID(id);
                if (m == null) return $"no match {id}";
                var pairs = new List<int[]>();
                foreach (string set in value.Split(','))
                {
                    string[] p = set.Split('-');
                    if (p.Length != 2
                        || !int.TryParse(p[0], NumberStyles.None, CultureInfo.InvariantCulture, out int ft)
                        || !int.TryParse(p[1], NumberStyles.None, CultureInfo.InvariantCulture, out int st)
                        || ft > 199 || st > 199)
                        return $"bad set score '{set}' (expected e.g. 25-21)";
                    pairs.Add(new[] { ft, st });
                }
                if (pairs.Count > m.Sets.Length) return $"match {id} has only {m.Sets.Length} set(s)";
                int won1 = pairs.Count(p => p[0] > p[1]);
                int won2 = pairs.Count(p => p[1] > p[0]);
                if (won1 == won2) return "no winner from those sets";
                var outcome = won1 > won2 ? Match.OutcomeType.FirstTeamWon : Match.OutcomeType.SecondTeamWon;

                // The exact payload AES's own score entry sends (MatchData), applied by AES's own
                // handler, so outcome, sets and LastScoreDateTimeEntered change together.
                var payload = new List<string>
                {
                    sf.FileID.ToString(), MatchDataType.ToString(CultureInfo.InvariantCulture),
                    id.ToString(CultureInfo.InvariantCulture), ((int)outcome).ToString(CultureInfo.InvariantCulture),
                    m.WorkTeamNumber.ToString(CultureInfo.InvariantCulture),
                    ((int)m.TypeOfWorkTeam).ToString(CultureInfo.InvariantCulture),
                };
                foreach (int[] p in pairs)
                {
                    payload.Add(p[0].ToString(CultureInfo.InvariantCulture));
                    payload.Add(p[1].ToString(CultureInfo.InvariantCulture));
                }
                if (sf.UpdateWithRemoteEntryData(payload.ToArray()) == null) return "AES did not apply the score";

                checks.Add(f =>
                {
                    Match r = f.GetMatchByID(id);
                    if (r == null) return $"match {id} gone";
                    if (r.TypeOfOutcome != outcome) return $"match {id} outcome is {r.TypeOfOutcome}";
                    Match.Set[] sets = r.Sets;
                    for (int i = 0; i < sets.Length; i++)
                    {
                        int? ft = i < pairs.Count ? pairs[i][0] : (int?)null;
                        int? st = i < pairs.Count ? pairs[i][1] : (int?)null;
                        if (sets[i].FirstTeamScore != ft || sets[i].SecondTeamScore != st)
                            return $"match {id} set {i + 1} is {sets[i].FirstTeamScore}-{sets[i].SecondTeamScore}";
                    }
                    return null;
                });
                return null;
            }

            case "move":
            {
                Match m = sf.GetMatchByID(id);
                if (m == null) return $"no match {id}";
                int at = value.LastIndexOf('@');
                if (at < 1) return "expected <court name>@<yyyy-MM-ddTHH:mm>";
                string courtName = value.Substring(0, at);
                Court court = sf.Courts.FirstOrDefault(c =>
                    string.Equals(c.Name, courtName, StringComparison.OrdinalIgnoreCase));
                if (court == null) return $"no court named '{courtName}'";
                if (!DateTime.TryParseExact(value.Substring(at + 1), "yyyy-MM-dd'T'HH:mm",
                        CultureInfo.InvariantCulture, DateTimeStyles.None, out DateTime start))
                    return "time must be yyyy-MM-ddTHH:mm";
                // Keep the clock AES already stores times on.
                start = DateTime.SpecifyKind(start, m.ScheduledStartDateTime.Kind);

                // Refuse to bump other matches: AES's drag would unschedule them silently.
                Match[] clash = court.GetMatchesForDateTimeRange(start, new TimeSpan(0, m.MatchLength, 0))
                    .Where(x => x.MatchID != m.MatchID).ToArray();
                if (clash.Length > 0)
                    return $"slot taken by match(es) {string.Join(", ", clash.Select(x => x.MatchID))}";

                court.ScheduleMatch(m, start, true); // the call AES's own grid drag makes
                checks.Add(f =>
                {
                    Match r = f.GetMatchByID(id);
                    if (r == null) return $"match {id} gone";
                    string onCourt = r.ScheduledCourt?.Name;
                    if (!string.Equals(onCourt, court.Name, StringComparison.Ordinal)) return $"match {id} is on '{onCourt}'";
                    if (r.ScheduledStartDateTime != start) return $"match {id} starts {r.ScheduledStartDateTime:yyyy-MM-dd HH:mm}";
                    return null;
                });
                return null;
            }

            default:
                return $"unknown edit '{kind}'";
        }
    }

    // ── helpers ────────────────────────────────────────────────────────────

    static IEnumerable<int> Scored(IEnumerable<string> ops)
    {
        foreach (string op in ops)
        {
            if (!op.StartsWith("score:", StringComparison.Ordinal)) continue;
            int eq = op.IndexOf('=');
            if (eq > 6 && int.TryParse(op.Substring(6, eq - 6), NumberStyles.AllowLeadingSign,
                    CultureInfo.InvariantCulture, out int id))
                yield return id;
        }
    }

    static void ShowFeed(string side, Match before, Match after)
    {
        if (after == null) return;
        string was = before == null ? "-" : $"{before.FirstTeamText} v {before.SecondTeamText}";
        string now = $"{after.FirstTeamText} v {after.SecondTeamText}";
        Console.WriteLine($"Feeds:     {side} -> match {after.MatchID}: {now}" + (was == now ? " (unchanged)" : $" (was {was})"));
    }

    static bool CheckPaths(string inPath, string outPath)
    {
        if (!File.Exists(inPath)) { Console.Error.WriteLine($"Not found: {inPath}"); return false; }
        if (string.Equals(Path.GetFullPath(inPath), Path.GetFullPath(outPath), StringComparison.OrdinalIgnoreCase))
        {
            Console.Error.WriteLine("Refusing to overwrite the input file; give a different output path.");
            return false;
        }
        return true;
    }

    static void Describe(SchedulerFile sf, byte[] input)
    {
        int version = BitConverter.ToInt32(input, 0);
        Console.WriteLine($"Input:     {input.Length} bytes, format version {version}");
        Console.WriteLine($"Event:     {sf.Event.Name}");
        Console.WriteLine($"Contents:  {sf.Teams.Length} teams, {sf.Courts.Length} courts, {sf.Event.Matches.Length} matches");
    }

    // "yes", or where the two byte arrays first differ.
    static string Compare(byte[] a, byte[] b)
    {
        int n = Math.Min(a.Length, b.Length);
        for (int i = 0; i < n; i++)
            if (a[i] != b[i])
                return $"no: first difference at byte {i} of {a.Length} (sizes {a.Length} vs {b.Length})";
        return a.Length == b.Length ? "yes" : $"no: same up to byte {n}, sizes {a.Length} vs {b.Length}";
    }

    // Does loading our output and saving it again give the same data? AES keeps users in a
    // Hashtable, so their order changes on every load and save (AES's own saves too); the
    // check allows that and nothing else.
    static string StableOnReload(byte[] saved) => SameData(saved, SchedulerFile.Load(saved).Save());

    // "yes" when byte-identical, "yes, except user order" when the only difference is the
    // order of the user records, otherwise where it differs.
    public static string SameData(byte[] a, byte[] b)
    {
        string exact = Compare(a, b);
        if (exact == "yes") return "yes";
        if (BitConverter.ToInt32(a, 0) != CurrentVersion || BitConverter.ToInt32(b, 0) != CurrentVersion)
            return exact + " (older format: user-order check not run)";

        UserBlock ua = UsersOf(a), ub = UsersOf(b);
        if (ua.Start != ub.Start || ua.Length != ub.Length || a.Length != b.Length) return exact;
        for (int i = 0; i < a.Length; i++)
        {
            if (i == ua.Start) { i += ua.Length - 1; continue; }
            if (a[i] != b[i]) return $"no: first difference at byte {i}, outside the user records";
        }
        return ua.Records.SequenceEqual(ub.Records) ? "yes, except user order" : "no: user records differ";
    }

    const int CurrentVersion = 660209715;

    class UserBlock { public int Start; public int Length; public List<string> Records; }

    // Where the user records sit in a current-format file (SchedulerFile.Save writes
    // header, clubs, teams, then users), and each record's bytes, sorted.
    static UserBlock UsersOf(byte[] data)
    {
        SchedulerFile sf = SchedulerFile.Load(data);
        int start = 4 + 4 + 16 + 4;                        // version, next manual id, FileID, file type
        start += 4 + sf.Clubs().Sum(c => Bytes(c).Length);  // club count + clubs
        start += 4 + sf.Teams.Sum(t => Bytes(t).Length);    // team count + teams
        start += 4;                                         // user count
        var records = sf.Users().Select(u => Convert.ToBase64String(Bytes(u))).ToList();
        records.Sort(StringComparer.Ordinal);
        return new UserBlock
        {
            Start = start,
            Length = records.Sum(r => Convert.FromBase64String(r).Length),
            Records = records,
        };
    }

    // Each model class's own internal Save(BinaryWriter), so record bytes match the file.
    static byte[] Bytes(object record)
    {
        var save = record.GetType().GetMethod("Save",
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public,
            null, new[] { typeof(BinaryWriter) }, null);
        if (save == null) throw new InvalidOperationException($"{record.GetType().Name} has no Save(BinaryWriter)");
        using var ms = new MemoryStream();
        using (var bw = new BinaryWriter(ms))
        {
            save.Invoke(record, new object[] { bw });
            bw.Flush();
        }
        return ms.ToArray();
    }

    static IEnumerable<object> Clubs(this SchedulerFile sf) => Field(sf, "_Clubs").Cast<object>();
    static IEnumerable<object> Users(this SchedulerFile sf) =>
        ((System.Collections.Hashtable)FieldValue(sf, "_Users")).Values.Cast<object>();

    static System.Collections.IEnumerable Field(object o, string name) => (System.Collections.IEnumerable)FieldValue(o, name);

    static object FieldValue(object o, string name) =>
        o.GetType().GetField(name, System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)?.GetValue(o)
        ?? throw new InvalidOperationException($"no field {name}");
}
