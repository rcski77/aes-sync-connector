/*
 * HostBatch.cs  —  spike: a headless "host loop" over a .vsf, using AES's own model
 *
 * Tests whether something other than the AES Scheduler (Relay) could hold an event's
 * .vsf on event day: apply a batch of court moves, scores, finishes and reseeds the way
 * the Scheduler's own screens do, save with AES's own SchedulerFile.Save, and build the
 * exact bytes AES's publish would send. See docs/WRITE_BACK_EDITOR_SCOPING.md,
 * "Headless host loop". Dev/test only: the monitor never calls these modes.
 *
 * NEVER opens a network connection. --host-batch only builds the PostEvent bytes and
 * writes them to a file.
 *
 *   AESBridge.exe --host-batch <in.vsf> <out.vsf> <changes.json> [<payload.bin>]
 *
 *   changes.json is either an array of changes or
 *   { "forceResultsImport": false, "changes": [ ... ] }. Changes apply in order:
 *
 *     { "op": "move",  "match": -50012, "court": "Court 3", "start": "2025-03-01T09:00" }
 *         Court.ScheduleMatch, refusing a slot another match holds (same as --edit move).
 *     { "op": "score", "match": -50012, "sets": [[25, 20], [25, 18]] }
 *         A MatchData payload through SchedulerFile.UpdateWithRemoteEntryData (as --edit score).
 *     { "op": "finish", "play": -50010, "ranks": [2, 1, 4, 3] }
 *         One rank (or null) per team slot in Play.Teams order: a FinishData payload through
 *         UpdateWithRemoteEntryData, the shape --encode-finish builds. If AES's in-order apply
 *         would hit a rank another slot still holds (its setter throws part-way), a clearing
 *         payload goes first, as the monitor's _finish_steps does.
 *     { "op": "reseed", "play": -50010, "team": 0, "seed": 2 }
 *     { "op": "reseed", "round": -50006, "team": "<team code>", "seed": 2 }
 *         Play.TeamAssignment.ReseedSeed, exactly as ReseedTeamsControl's drag does: the team
 *         must be one of the round's ReseedingTeams, the seed one of its ActualReseedingSeeds;
 *         whoever holds that seed already is cleared first. "team" is a slot index in the
 *         play, or a team code. "seed": null clears.
 *     { "op": "reseed-flag", "play": -50010, "rank": 1, "value": true }
 *         Design change, not event-day work: marks finish rank N of a play as reseeded
 *         (Play.Teams[N-1].RankWillBeReseeded, what EditPoolControl/EditBracketControl's
 *         "Reseeds" list sets). AES's setter clears every ReseedSeed in the round.
 *
 *   Then: save, reload, check the reloaded file holds exactly the in-memory state (every
 *   match and play slot, data and AES's display text), list every value that differs from
 *   the input, and print what each changed match side and play slot now shows.
 *
 *   <payload.bin>, if given, gets the plaintext frame AES's publish would send for
 *   PostEvent (NetworkControl.UpdateThreadLoop -> NetConnection.SendCommand): NCCommand
 *   0x23, command 9216 LE, 3-byte length, flags (1 = gzip, used when > 4096 bytes), then
 *   BinaryFormatter(object[] { Save() bytes, forceResultsImport }). Before the stream cipher.
 *   The AuthenticationCommit frame (email + password) AES sends first is NOT built.
 *
 *   AESBridge.exe --plays <in.vsf>
 *       Lists rounds, plays and team slots (entry seed, finish rank, reseed flag, reseed
 *       seed, team code) to pick batch targets from, and each round's reseeding teams.
 *
 * Only ever run against a COPY of a finished event (the copy keeps the real FileID).
 */

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Runtime.Serialization.Formatters.Binary;
using System.Web.Script.Serialization;
using AES.Scheduler.Model;

static class HostBatch
{
    const int FinishDataType = 33282;   // RemoteEntryUpdateType.FinishData
    const int PostEventCommand = 9216;  // SchedulerNetCommand.PostEvent
    const byte CommandWithObjectData = 0x23;

    // ── format check ───────────────────────────────────────────────────────

    // The newest format the referenced EventScheduler assembly reads (SchedulerFile.CURRENT_VERSION).
    public static int SupportedVersion
    {
        get
        {
            var f = typeof(SchedulerFile).GetField("CURRENT_VERSION",
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic);
            return f == null ? 660209715 : (int)f.GetRawConstantValue();
        }
    }

    // Reports the input's format version and loads it, or returns null with a clear message
    // when AES's loader would refuse it. Every .vsf mode loads through here.
    public static SchedulerFile LoadChecked(byte[] data, string path)
    {
        if (data.Length < 8) { Console.Error.WriteLine($"Not a scheduler file (only {data.Length} bytes): {path}"); return null; }
        int version = BitConverter.ToInt32(data, 0);
        int supported = SupportedVersion;
        Console.WriteLine($"Format:    {version} (this bridge's AES assembly writes {supported})" +
            (version < supported ? "; converted up on load, saved as " + supported : ""));
        if (version > supported)
        {
            Console.Error.WriteLine($"Refusing {path}: format {version} is newer than this AES assembly understands ({supported}). " +
                "Files from AES's servers always come in the newest format; update bridge/EventScheduler.exe to a Scheduler release that reads it.");
            return null;
        }
        if (version <= 0)
        {
            Console.Error.WriteLine($"Refusing {path}: header {version} is not a scheduler format version.");
            return null;
        }
        try { return SchedulerFile.Load(data); }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"AES could not load {path} (format {version}): {ex.Message}");
            return null;
        }
    }

    // ── --plays ────────────────────────────────────────────────────────────

    public static int Plays(string[] args)
    {
        if (args.Length != 1 || !File.Exists(args[0])) { Console.Error.WriteLine("Usage: --plays <in.vsf>"); return 2; }
        SchedulerFile sf = LoadChecked(File.ReadAllBytes(args[0]), args[0]);
        if (sf == null) return 1;
        foreach (Division d in sf.Event.Divisions)
        {
            Console.WriteLine($"DIVISION {d.CodeAlias}");
            foreach (Round r in d.Rounds)
            {
                Console.WriteLine($"  ROUND {r.RoundID} {r.ShortName}");
                foreach (Play p in r.Plays)
                {
                    Console.WriteLine($"    PLAY {p.PlayID} {p.CompleteShortName} ({(p is Pool ? "pool" : "bracket")})");
                    Play.TeamAssignment[] teams = p.Teams;
                    for (int i = 0; i < teams.Length; i++)
                        Console.WriteLine($"      slot {i}: entry {Show(teams[i].EntrySeed)}  finish {Show(teams[i].FinishRank)}" +
                            $"  rank {i + 1} reseeded {teams[i].RankWillBeReseeded}  reseed {Show(teams[i].ReseedSeed)}" +
                            $"  exit {Show(teams[i].ExitSeed)}  {Code(teams[i]) ?? "-"}");
                }
                Play.TeamAssignment[] reseeding = r.ReseedingTeams;
                if (reseeding.Length > 0)
                    Console.WriteLine($"    RESEEDING teams {string.Join(", ", reseeding.Select(Describe))}; seeds {string.Join(", ", r.ActualReseedingSeeds)}");
            }
        }
        return 0;
    }

    // ── --host-batch ───────────────────────────────────────────────────────

    public static int Run(string[] args)
    {
        if (args.Length < 3 || args.Length > 4)
        {
            Console.Error.WriteLine("Usage: --host-batch <in.vsf> <out.vsf> <changes.json> [<payload.bin>]");
            return 2;
        }
        string inPath = args[0], outPath = args[1], changesPath = args[2], payloadPath = args.Length > 3 ? args[3] : null;
        if (!File.Exists(inPath)) { Console.Error.WriteLine($"Not found: {inPath}"); return 2; }
        if (!File.Exists(changesPath)) { Console.Error.WriteLine($"Not found: {changesPath}"); return 2; }
        foreach (string p in new[] { outPath, payloadPath }.Where(x => x != null))
            if (string.Equals(Path.GetFullPath(p), Path.GetFullPath(inPath), StringComparison.OrdinalIgnoreCase))
            { Console.Error.WriteLine("Refusing to overwrite the input file."); return 2; }

        List<Dictionary<string, object>> changes;
        bool force;
        try { changes = ReadChanges(File.ReadAllText(changesPath), out force); }
        catch (Exception ex) { Console.Error.WriteLine($"Bad changes file: {ex.Message}"); return 2; }

        byte[] input = File.ReadAllBytes(inPath);
        SchedulerFile sf = LoadChecked(input, inPath);
        if (sf == null) return 1;
        Console.WriteLine($"Contents:  {sf.Teams.Length} teams, {sf.Courts.Length} courts, {sf.Event.Matches.Length} matches, {sf.Event.Plays.Length} plays");

        SchedulerFile original = SchedulerFile.Load(input);
        Dictionary<string, string> before = Snapshot(original);

        for (int i = 0; i < changes.Count; i++)
        {
            string label = $"#{i + 1} {Summary(changes[i])}";
            string error;
            try { error = Apply(sf, changes[i]); }
            catch (Exception ex) { error = "AES threw: " + ex.Message; }
            if (error != null) { Console.Error.WriteLine($"Change {label} failed: {error}"); return 1; }
            Console.WriteLine($"Applied:   {label}");
        }

        Dictionary<string, string> edited = Snapshot(sf);
        byte[] saved = sf.Save(outPath);
        Console.WriteLine($"Saved:     {Path.GetFullPath(outPath)} ({input.Length} -> {saved.Length} bytes)");
        Console.WriteLine($"Stable:    {VsfEditTool.SameData(saved, SchedulerFile.Load(saved).Save())}");

        // Reload from disk: does the file hold exactly what the model held before saving?
        SchedulerFile reloaded = SchedulerFile.Load(File.ReadAllBytes(outPath));
        Dictionary<string, string> after = Snapshot(reloaded);
        var lost = edited.Keys.Union(after.Keys)
            .Where(k => Get(edited, k) != Get(after, k)).OrderBy(k => k, StringComparer.Ordinal).ToList();
        foreach (string k in lost) Console.WriteLine($"MISMATCH:  {k}: in memory {Get(edited, k)}, reloaded {Get(after, k)}");
        Console.WriteLine(lost.Count == 0
            ? $"Reload:    identical to the edited model ({after.Count} values checked)"
            : $"Reload:    {lost.Count} value(s) differ from the edited model");

        // Every value that changed from the input, data first, then what AES now displays.
        var changed = before.Keys.Union(after.Keys)
            .Where(k => Get(before, k) != Get(after, k)).OrderBy(k => k, StringComparer.Ordinal).ToList();
        Console.WriteLine($"Changed:   {changed.Count} value(s) differ from the input");
        foreach (string k in changed.Where(k => !k.Contains(" text")))
            Console.WriteLine($"  data     {k}: {Get(before, k)} -> {Get(after, k)}");
        foreach (string k in changed.Where(k => k.Contains(" text")))
            Console.WriteLine($"  shows    {k}: {Get(before, k)} -> {Get(after, k)}");

        if (payloadPath != null)
        {
            string err = WritePayload(payloadPath, saved, force);
            if (err != null) { Console.Error.WriteLine($"Payload failed: {err}"); return 1; }
        }
        return lost.Count == 0 ? 0 : 1;
    }

    // ── changes ────────────────────────────────────────────────────────────

    static List<Dictionary<string, object>> ReadChanges(string json, out bool force)
    {
        object root = new JavaScriptSerializer().DeserializeObject(json);
        force = false;
        object list = root;
        if (root is Dictionary<string, object> obj)
        {
            if (obj.TryGetValue("forceResultsImport", out object f)) force = f is bool b ? b : throw new Exception("forceResultsImport must be true or false");
            if (!obj.TryGetValue("changes", out list)) throw new Exception("expected \"changes\"");
        }
        if (!(list is object[] items)) throw new Exception("changes must be an array");
        return items.Select(x => x as Dictionary<string, object> ?? throw new Exception("each change must be an object")).ToList();
    }

    static string Apply(SchedulerFile sf, Dictionary<string, object> c)
    {
        switch (Str(c, "op"))
        {
            case "move":
            {
                // Same code path as --edit move (no-bump rule included).
                string op = $"move:{Int(c, "match")}={Str(c, "court")}@{Str(c, "start")}";
                return VsfEditTool.Apply(sf, op, new List<Func<SchedulerFile, string>>());
            }
            case "score":
            {
                if (!(c.TryGetValue("sets", out object s) && s is object[] sets) || sets.Length == 0) return "\"sets\" must be [[ft, st], ...]";
                var parts = new List<string>();
                foreach (object set in sets)
                {
                    if (!(set is object[] pair) || pair.Length != 2 || !(pair[0] is int ft) || !(pair[1] is int st))
                        return "each set must be [ft, st]";
                    parts.Add($"{ft}-{st}");
                }
                int id = Int(c, "match");
                string error = VsfEditTool.Apply(sf, $"score:{id}={string.Join(",", parts)}", new List<Func<SchedulerFile, string>>());
                if (error != null) return error;
                Match m = sf.GetMatchByID(id);
                return m.Sets.Take(parts.Count).Select(x => $"{x.FirstTeamScore}-{x.SecondTeamScore}").SequenceEqual(parts)
                    ? null : "AES did not keep the sets";
            }
            case "finish": return Finish(sf, c);
            case "reseed": return Reseed(sf, c);
            case "reseed-flag":
            {
                Play play = sf.GetPlayByID(Int(c, "play"));
                if (play == null) return $"no play {Int(c, "play")}";
                int rank = Int(c, "rank");
                if (rank < 1 || rank > play.Teams.Length) return $"rank must be 1..{play.Teams.Length}";
                if (!(c.TryGetValue("value", out object v) && v is bool value)) return "\"value\" must be true or false";
                play.Teams[rank - 1].RankWillBeReseeded = value; // what the pool/bracket editor's Reseeds list sets
                return play.Teams[rank - 1].RankWillBeReseeded == value ? null : "AES did not keep the flag";
            }
            default: return $"unknown op '{Str(c, "op")}'";
        }
    }

    static string Finish(SchedulerFile sf, Dictionary<string, object> c)
    {
        int playId = Int(c, "play");
        Play play = sf.GetPlayByID(playId);
        if (play == null) return $"no play {playId}";
        if (play is Bracket b && b.OwningPool != null) return "AES does not take finishes on a pool's tiebreaker bracket";
        Play.TeamAssignment[] teams = play.Teams;
        if (!(c.TryGetValue("ranks", out object r) && r is object[] raw)) return "\"ranks\" must be an array";
        if (raw.Length != teams.Length) return $"play {playId} has {teams.Length} slots; give one rank (or null) per slot";
        var target = new int?[raw.Length];
        for (int i = 0; i < raw.Length; i++)
        {
            if (raw[i] == null) continue;
            if (!(raw[i] is int v) || v < 1 || v > teams.Length) return $"rank {raw[i]} out of range 1..{teams.Length}";
            target[i] = v;
        }
        if (target.Where(x => x.HasValue).GroupBy(x => x).Any(g => g.Count() > 1)) return "duplicate rank";

        // AES applies slot by slot and its FinishRank setter throws if another slot still holds
        // the rank. Clear first when that would happen.
        int?[] current = teams.Select(t => t.FinishRank).ToArray();
        bool clash = false;
        for (int i = 0; i < target.Length && !clash; i++)
        {
            current[i] = target[i];
            clash = target[i].HasValue && current.Where((x, j) => j != i && x == target[i]).Any();
        }
        if (clash)
        {
            if (sf.UpdateWithRemoteEntryData(FinishPayload(sf, playId, new int?[target.Length])) == null) return "AES did not apply the clearing payload";
            Console.WriteLine($"           (cleared play {playId}'s ranks first: AES's in-order apply would clash)");
        }
        if (sf.UpdateWithRemoteEntryData(FinishPayload(sf, playId, target)) == null) return "AES did not apply the finish ranks";
        return teams.Select(t => t.FinishRank).SequenceEqual(target) ? null : "AES did not keep the ranks";
    }

    // [fileId, "33282", playId, rank per slot ("" = none)]: Play.GetFinishRankSerialization()'s shape.
    static string[] FinishPayload(SchedulerFile sf, int playId, int?[] ranks) =>
        new[] { sf.FileID.ToString(), FinishDataType.ToString(CultureInfo.InvariantCulture), playId.ToString(CultureInfo.InvariantCulture) }
            .Concat(ranks.Select(x => x.HasValue ? x.Value.ToString(CultureInfo.InvariantCulture) : ""))
            .ToArray();

    static string Reseed(SchedulerFile sf, Dictionary<string, object> c)
    {
        c.TryGetValue("team", out object team);
        Play.TeamAssignment ta;
        Round round;
        if (c.ContainsKey("play"))
        {
            Play play = sf.GetPlayByID(Int(c, "play"));
            if (play == null) return $"no play {Int(c, "play")}";
            round = play.OwningGroup.OwningRound;
            if (team is int slot)
            {
                if (slot < 0 || slot >= play.Teams.Length) return $"slot must be 0..{play.Teams.Length - 1}";
                ta = play.Teams[slot];
            }
            else ta = play.Teams.FirstOrDefault(t => team is string s && string.Equals(Code(t), s, StringComparison.OrdinalIgnoreCase));
        }
        else if (c.ContainsKey("round"))
        {
            int roundId = Int(c, "round");
            round = sf.Event.Divisions.SelectMany(d => d.Rounds).FirstOrDefault(x => x.RoundID == roundId);
            if (round == null) return $"no round {roundId}";
            if (!(team is string code)) return "with \"round\", \"team\" must be a team code";
            ta = round.Plays.SelectMany(p => p.Teams).FirstOrDefault(t => string.Equals(Code(t), code, StringComparison.OrdinalIgnoreCase));
        }
        else return "give \"play\" or \"round\"";
        if (ta == null) return $"no team '{team}' there";

        // ReseedTeamsControl only offers the round's ReseedingTeams (teams whose finish rank is a
        // reseeded rank) and its ActualReseedingSeeds (those teams' seeds ignoring the reseed).
        Play.TeamAssignment[] reseeding = round.ReseedingTeams;
        if (!reseeding.Contains(ta))
            return $"{Describe(ta)} is not a reseeding team in round {round.RoundID} (needs a finish rank that is flagged RankWillBeReseeded); " +
                   (reseeding.Length == 0 ? "this round has none" : "reseeding: " + string.Join(", ", reseeding.Select(Describe)));
        c.TryGetValue("seed", out object seedValue);
        if (seedValue == null) { ta.ReseedSeed = null; return ta.ReseedSeed == null ? null : "AES did not clear the reseed"; }
        if (!(seedValue is int seed)) return "\"seed\" must be a number or null";
        int[] seeds = round.ActualReseedingSeeds;
        if (!seeds.Contains(seed)) return $"seed {seed} is not one of this round's reseeding seeds ({string.Join(", ", seeds)})";

        // The drag-drop in ReseedTeamsControl: clear whoever holds the seed, then set it.
        Play.TeamAssignment holder = reseeding.SingleOrDefault(x => x.ReseedSeed == seed);
        if (holder != null && holder != ta) holder.ReseedSeed = null;
        ta.ReseedSeed = seed;
        return ta.ReseedSeed == seed ? null : "AES did not keep the reseed";
    }

    // ── state snapshot ─────────────────────────────────────────────────────

    // Every match and play slot as key -> value: data, plus the text AES displays for it.
    static Dictionary<string, string> Snapshot(SchedulerFile sf)
    {
        var s = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (Match m in sf.Event.Matches)
        {
            string k = $"match {m.MatchID}";
            s[k + " court"] = m.ScheduledCourt?.Name ?? "-";
            s[k + " start"] = m.ScheduledCourt == null ? "-" : m.ScheduledStartDateTime.ToString("yyyy-MM-ddTHH:mm", CultureInfo.InvariantCulture);
            s[k + " length"] = m.MatchLength.ToString(CultureInfo.InvariantCulture);
            s[k + " outcome"] = m.TypeOfOutcome.ToString();
            s[k + " sets"] = string.Join(",", m.Sets.Select(x => $"{Show(x.FirstTeamScore)}-{Show(x.SecondTeamScore)}"));
            s[k + " work team"] = $"{m.TypeOfWorkTeam}/{m.WorkTeamNumber}";
            s[k + " text first"] = m.FirstTeamText;
            s[k + " text second"] = m.SecondTeamText;
            s[k + " text work"] = m.WorkTeamText;
        }
        foreach (Play p in sf.Event.Plays)
        {
            Play.TeamAssignment[] teams = p.Teams;
            for (int i = 0; i < teams.Length; i++)
            {
                string k = $"play {p.PlayID} slot {i}";
                s[k + " entry"] = Show(teams[i].EntrySeed);
                s[k + " finish"] = Show(teams[i].FinishRank);
                s[k + " reseeded"] = teams[i].RankWillBeReseeded.ToString();
                s[k + " reseed"] = Show(teams[i].ReseedSeed);
                s[k + " exit"] = Show(teams[i].ExitSeed);
                s[k + " text"] = teams[i].TeamText;
            }
        }
        return s;
    }

    // ── PostEvent payload ──────────────────────────────────────────────────

    // The plaintext frame NetConnection.SendCommand(PostEvent, object[] { Save(), force }) writes.
    static string WritePayload(string path, byte[] saved, bool force)
    {
        byte[] body;
#pragma warning disable SYSLIB0011
        using (var ms = new MemoryStream())
        {
            new BinaryFormatter().Serialize(ms, new object[] { saved, force });
            body = ms.ToArray();
        }
        bool compressed = body.Length > 4096; // NetConnection.WriteData's threshold
        byte[] data = compressed ? Colib.Util.MiscUtils.CompressData(body) : body;
        if (data.Length > 0xFFFFFF) return "too long for AES's 3-byte length";

        using (var ms = new MemoryStream())
        {
            ms.WriteByte(CommandWithObjectData);
            ms.Write(BitConverter.GetBytes(PostEventCommand), 0, 4);
            ms.Write(BitConverter.GetBytes(data.Length), 0, 3);
            ms.WriteByte((byte)(compressed ? 1 : 0));
            ms.Write(data, 0, data.Length);
            File.WriteAllBytes(path, ms.ToArray());
        }

        // Read it back the way NetConnection.ReceiveCommand + CommandSet.GetData would.
        byte[] frame = File.ReadAllBytes(path);
        int cmd = BitConverter.ToInt32(frame, 1);
        int len = frame[5] | frame[6] << 8 | frame[7] << 16;
        byte[] raw = frame.Skip(9).ToArray();
        if (frame[0] != CommandWithObjectData || cmd != PostEventCommand || len != raw.Length) return "frame header does not read back";
        if ((frame[8] & 1) == 1)
            using (var gz = new GZipStream(new MemoryStream(raw), CompressionMode.Decompress))
            using (var outMs = new MemoryStream()) { gz.CopyTo(outMs); raw = outMs.ToArray(); }
        object[] o = new BinaryFormatter().Deserialize(new MemoryStream(raw)) as object[];
#pragma warning restore SYSLIB0011
        if (o == null || o.Length != 2 || !(o[0] is byte[] file) || !(o[1] is bool f) || f != force || !file.SequenceEqual(saved))
            return "payload does not read back as object[] { saved file, forceResultsImport }";
        SchedulerFile.Load(file);
        Console.WriteLine($"Payload:   {Path.GetFullPath(path)}: PostEvent frame, {frame.Length} bytes " +
            $"({body.Length} serialized, {(compressed ? "gzip" : "uncompressed")}), forceResultsImport={force}; " +
            "reads back to the saved file. Not sent anywhere.");
        return null;
    }

    // ── helpers ────────────────────────────────────────────────────────────

    static string Code(Play.TeamAssignment ta)
    {
        try { return ta.DivisionTeamAssignment?.Team?.Code; } catch { return null; }
    }

    static string Describe(Play.TeamAssignment ta) =>
        $"{ta.OwningPlay.PlayID}/slot {ta.TeamNumber} ({Code(ta) ?? "no team"}, finish {Show(ta.FinishRank)}, reseed {Show(ta.ReseedSeed)})";

    static string Show(int? v) => v.HasValue ? v.Value.ToString(CultureInfo.InvariantCulture) : "-";

    static string Get(Dictionary<string, string> d, string k) => d.TryGetValue(k, out string v) ? v : "(absent)";

    static string Summary(Dictionary<string, object> c) =>
        string.Join(" ", c.Select(kv => $"{kv.Key}={Text(kv.Value)}"));

    static string Text(object v) => v switch
    {
        null => "null",
        object[] a => "[" + string.Join(",", a.Select(Text)) + "]",
        _ => Convert.ToString(v, CultureInfo.InvariantCulture),
    };

    static string Str(Dictionary<string, object> c, string key) =>
        c.TryGetValue(key, out object v) && v is string s ? s : throw new Exception($"\"{key}\" must be a string");

    static int Int(Dictionary<string, object> c, string key) =>
        c.TryGetValue(key, out object v) && v is int i ? i : throw new Exception($"\"{key}\" must be a whole number");
}
