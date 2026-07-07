/*
 * AESBridge.cs  —  fixed for external assembly access
 * Internal properties accessed via reflection; all public API used directly.
 */

using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Reflection;
using System.Collections.Generic;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Formatters.Binary;
using AES.Scheduler.Model;

class AESBridge
{
    // Court name → ID lookup built once from sf.Courts
    static Dictionary<string, int> _courtIdByName;

    static int Main(string[] args)
    {
        try
        {
            // --remote mode: decode a RemoteEntryUpdateAttached BinaryFormatter payload
            if (args.Length >= 2 && args[1] == "--remote" && File.Exists(args[0]))
            {
                byte[] payload = File.ReadAllBytes(args[0]);
                Console.WriteLine(DecodeRemoteEntry(payload));
                return 0;
            }

            byte[] data;
            if (args.Length > 0 && File.Exists(args[0]))
                data = File.ReadAllBytes(args[0]);
            else
            {
                using var ms = new MemoryStream();
                Console.OpenStandardInput().CopyTo(ms);
                data = ms.ToArray();
            }

            if (data.Length == 0) { Console.Error.WriteLine("No data."); return 1; }

            SchedulerFile sf = SchedulerFile.Load(data);
            sf.EnableCaching();

            // Build court name → ID lookup (used to resolve internal ScheduledCourtID)
            _courtIdByName = sf.Courts.ToDictionary(
                c => c.Name ?? "",
                c => c.CourtID,
                StringComparer.OrdinalIgnoreCase);

            string json = ToJson(sf);

            // Write to tournament_data.json next to the input file (or exe if piped)
            string outPath = args.Length > 0
                ? Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[0])), "tournament_data.json")
                : Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "tournament_data.json");

            File.WriteAllText(outPath, json, Encoding.UTF8);
            Console.WriteLine($"Written to: {outPath}");
            Console.WriteLine($"  Event:   {sf.Event.Name}");
            Console.WriteLine($"  Courts:  {sf.Courts.Length}");
            Console.WriteLine($"  Matches: {sf.Event.Matches.Count(m => m.IsScheduled)} scheduled");
            Console.WriteLine($"  Pools:   {sf.Event.Plays.OfType<Pool>().Count()}");
            Console.WriteLine($"  Updated: {sf.LastUpdatedTimestamp:yyyy-MM-dd HH:mm:ss} UTC");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Error: {ex.Message}\n{ex.StackTrace}");
            return 1;
        }
    }

    // ── Remote entry decoder ───────────────────────────────────────────────────
    // Decodes a RemoteEntryUpdateAttached BinaryFormatter payload.
    // Uses a custom binder so version differences between the monitor and
    // the EventScheduler assembly don't prevent deserialization.

#pragma warning disable SYSLIB0011
    class AnyVersionBinder : SerializationBinder
    {
        public override Type BindToType(string assemblyName, string typeName)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                var t = asm.GetType(typeName);
                if (t != null) return t;
            }
            // Try partial name match (strips version/culture/token)
            string shortName = typeName.Split(',')[0].Trim();
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                var t = asm.GetType(shortName);
                if (t != null) return t;
            }
            return Type.GetType(typeName);  // last resort, may return null — acceptable
        }
    }
#pragma warning restore SYSLIB0011

#pragma warning disable SYSLIB0011  // BinaryFormatter is obsolete but functional on net48
    static string DecodeRemoteEntry(byte[] data)
    {
        try
        {
            var fmt = new BinaryFormatter { Binder = new AnyVersionBinder() };
            object obj;
            using (var ms = new MemoryStream(data))
                obj = fmt.Deserialize(ms);

            if (obj == null)
                return "{}";

            var sb = new StringBuilder("{");
            sb.Append($"\"objectType\": {S(obj.GetType().Name)}");

            // String[] is the actual format of GetMatchSerialization/GetFinishRankSerialization
            if (obj is string[] arr)
            {
                sb.Append(", \"values\": [");
                for (int i = 0; i < arr.Length; i++)
                {
                    if (i > 0) sb.Append(", ");
                    sb.Append(arr[i] == null ? "null" : S(arr[i]));
                }
                sb.Append("]");
            }
            else
            {
                // Fallback: reflection dump for other types
                var flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
                foreach (var prop in obj.GetType().GetProperties(flags))
                {
                    try
                    {
                        var val = prop.GetValue(obj);
                        if (val == null) continue;
                        string vs = val is Array a
                            ? $"[{string.Join(", ", a.Cast<object>().Select(x => x?.ToString() ?? "null"))}]"
                            : val.ToString();
                        sb.Append($", \"{prop.Name}\": {S(vs)}");
                    }
                    catch { }
                }
            }

            sb.Append("}");
            return sb.ToString();
        }
        catch (Exception ex)
        {
            return $"{{\"error\": {S(ex.Message)}}}";
        }
    }
#pragma warning restore SYSLIB0011

    // ── Reflection helpers ─────────────────────────────────────────────────
    // Used for properties that are public in source but internal in the binary,
    // or where the decompiler showed a different name than the compiled assembly.

    static T Reflect<T>(object obj, string propName, T fallback = default)
    {
        if (obj == null) return fallback;
        try
        {
            var flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
            var val = obj.GetType().GetProperty(propName, flags)?.GetValue(obj)
                   ?? obj.GetType().GetField(propName, flags)?.GetValue(obj);
            return val is T t ? t : fallback;
        }
        catch { return fallback; }
    }

    static string RStr(object obj, string propName) => Reflect<string>(obj, propName, "");
    static int    RInt(object obj, string propName) => Reflect<int>(obj, propName, -1);

    // ── Top-level ──────────────────────────────────────────────────────────

    static string ToJson(SchedulerFile sf)
    {
        var sb = new StringBuilder();
        sb.AppendLine("{");

        // Event
        sb.AppendLine("  \"event\": {");
        sb.AppendLine($"    \"name\":        {S(sf.Event.Name)},");
        sb.AppendLine($"    \"eventId\":     {sf.Event.EventID},");
        sb.AppendLine($"    \"startDate\":   {S(sf.Event.StartDate.ToString("yyyy-MM-dd"))},");
        sb.AppendLine($"    \"endDate\":     {S(sf.Event.EndDate.ToString("yyyy-MM-dd"))},");
        sb.AppendLine($"    \"lastUpdated\": {S(sf.LastUpdatedTimestamp.ToString("o"))},");
        sb.AppendLine($"    \"manualAddition\": {B(sf.Event.ManualAddition)}");
        sb.AppendLine("  },");

        // Courts
        AppendArray(sb, "courts", sf.Courts,
            c => $"{{\"courtId\": {c.CourtID}, \"name\": {S(c.Name)}}}");

        // Divisions — final place groups (Gold/Silver/Bronze) and their seed resolution.
        // Exploratory addition: Division.FinalPlaces is public, no reflection needed.
        AppendArray(sb, "divisions", sf.Event.Divisions, DivisionJson);

        // Matches
        var matches = sf.Event.Matches
            .Where(m => m.IsScheduled)
            .OrderBy(m => m.ScheduledStartDateTime)
            .ThenBy(m => CourtId(m))
            .ToArray();
        AppendArray(sb, "matches", matches, MatchJson);

        // Pools
        var pools = sf.Event.Plays
            .OfType<Pool>()
            .OrderBy(p => { try { return p.OwningGroup?.OwningRound?.OwningDivision?.CodeAlias ?? ""; } catch { return ""; } })
            .ThenBy(p => p.CompleteShortName ?? "")
            .ToArray();
        AppendArray(sb, "pools", pools, PoolJson);

        // Brackets
        var brackets = sf.Event.Plays
            .OfType<Bracket>()
            .OrderBy(p => { try { return p.OwningGroup?.OwningRound?.OwningDivision?.CodeAlias ?? ""; } catch { return ""; } })
            .ThenBy(p => p.CompleteShortName ?? "")
            .ToArray();
        AppendArrayLast(sb, "brackets", brackets, BracketJson);

        sb.AppendLine("}");
        return sb.ToString();
    }

    // ── Court ID ───────────────────────────────────────────────────────────
    // ScheduledCourtID is internal — get it via reflection, or fall back to
    // looking up the court name from the public ScheduledCourtText property.

    static int CourtId(Match m)
    {
        // Try reflection on internal property first
        int id = RInt(m, "ScheduledCourtID");
        if (id >= 0) return id;

        // Fall back: resolve from court name
        try
        {
            string courtName = m.ScheduledCourtText; // "Court 1" or "No Court"
            if (_courtIdByName.TryGetValue(courtName, out int found)) return found;
        }
        catch { }
        return -1;
    }

    // ── Match ──────────────────────────────────────────────────────────────

    static string MatchJson(Match m)
    {
        // Division / play info via reflection (internal in binary)
        string divCode  = "", divName = "", playName = "", playType = "";
        int playId = -1;
        try
        {
            var div = m.OwningPlay?.OwningGroup?.OwningRound?.OwningDivision;
            if (div != null)
            {
                divCode = div.CodeAlias ?? "";
                divName = div.DescriptionAlias ?? "";
            }
            if (m.OwningPlay is Bracket tieBracket && tieBracket.IsPlayoff)
            {
                // This match belongs to a pool's own internal tiebreaker bracket
                // (Pool.PlayoffBracket) — attribute it to the owning pool's PlayID
                // instead of the tiebreaker bracket's own separate PlayID, so
                // downstream consumers can group it with the pool's other matches.
                playId = tieBracket.OwningPool.PlayID;
            }
            else
            {
                playId = RInt(m.OwningPlay, "PlayID");
            }
            playName = RStr(m.OwningPlay, "Name");
            if (string.IsNullOrEmpty(playName)) playName = m.OwningPlay?.CompleteShortName ?? "";
            playType = m.OwningPlay is Pool    ? "pool"
                     : m.OwningPlay is Bracket b && (bool)(typeof(Bracket)
                           .GetProperty("IsPlayoff")?.GetValue(b) ?? false) ? "playoff"
                     : m.OwningPlay is Bracket ? "bracket"
                     : "other";
        }
        catch { }

        // Sets — only include sets where both scores were actually entered
        var sets = m.Sets.Where(s => s.FirstTeamScore.HasValue && s.SecondTeamScore.HasValue).ToArray();
        var setsJson = new StringBuilder("[");
        for (int i = 0; i < sets.Length; i++)
        {
            if (i > 0) setsJson.Append(", ");
            setsJson.Append($"{{\"team1\": {sets[i].FirstTeamScore.Value}, " +
                            $"\"team2\": {sets[i].SecondTeamScore.Value}}}");
        }
        setsJson.Append("]");

        var sb = new StringBuilder("{");
        sb.Append($"\"matchId\":      {m.MatchID}, ");
        sb.Append($"\"courtId\":      {CourtId(m)}, ");
        sb.Append($"\"courtName\":    {S(m.ScheduledCourtText)}, ");
        sb.Append($"\"startTime\":    {S(m.ScheduledStartDateTime.ToString("o"))}, ");
        sb.Append($"\"endTime\":      {S(m.ScheduledEndDateTime.ToString("o"))}, ");
        sb.Append($"\"matchLength\":  {m.MatchLength}, ");
        sb.Append($"\"team1\":        {S(m.FirstTeamText)}, ");
        sb.Append($"\"team2\":        {S(m.SecondTeamText)}, ");
        sb.Append($"\"workTeam\":     {S(m.WorkTeamText)}, ");
        sb.Append($"\"divisionCode\": {S(divCode)}, ");
        sb.Append($"\"divisionName\": {S(divName)}, ");
        sb.Append($"\"playId\":       {playId}, ");
        sb.Append($"\"playName\":     {S(playName)}, ");
        sb.Append($"\"playType\":     {S(playType)}, ");
        sb.Append($"\"outcome\":      {S(m.TypeOfOutcome.ToString())}, ");
        sb.Append($"\"decided\":      {B(m.TypeOfOutcome != Match.OutcomeType.Undecided)}, ");
        sb.Append($"\"firstTeamWon\": {B(m.FirstTeamWon)}, ");
        sb.Append($"\"scoreText\":    {S(m.ScoreText)}, ");
        sb.Append($"\"sets\":         {setsJson}, ");
        sb.Append($"\"shortName\":    {S(m.CompleteShortName)}, ");
        sb.Append($"\"fullName\":     {S(m.CompleteFullName)}");
        sb.Append("}");
        return sb.ToString();
    }

    // ── Round ordering ─────────────────────────────────────────────────────
    // Round.PreviousRound is public; walking it gives a reliable round order
    // for backward seed-tracing (e.g. gold bracket -> feeder pools) without
    // parsing naming conventions like the "R1"/"R2" prefix in CompleteShortName.

    static int RoundIndex(AES.Scheduler.Model.Round round)
    {
        int idx = 0;
        for (var r = round; r?.PreviousRound != null; r = r.PreviousRound) idx++;
        return idx;
    }

    // ── Pool ───────────────────────────────────────────────────────────────

    static string PoolJson(Pool pool)
    {
        string divCode = "", divName = "", poolName = "", shortName = "", fullShortName = "", date = "";
        int roundIndex = -1;
        string roundShortName = "", groupName = "", groupShortName = "";
        int? divisionId = null;
        var courtsJson = new StringBuilder("[");
        try
        {
            var div = pool.OwningGroup?.OwningRound?.OwningDivision;
            if (div != null)
            {
                divCode = div.CodeAlias ?? "";
                divName = div.DescriptionAlias ?? "";
                divisionId = div.EventDivisionAssignmentID;
            }
            var round = pool.OwningGroup?.OwningRound;
            if (round != null)
            {
                roundShortName = round.ShortName ?? "";
                roundIndex = RoundIndex(round);
            }
            groupName = pool.OwningGroup?.FullName ?? "";
            groupShortName = pool.OwningGroup?.ShortName ?? "";
            poolName  = RStr(pool, "FullName");
            if (string.IsNullOrEmpty(poolName)) poolName = pool.CompleteFullName ?? pool.CompleteShortName ?? "";
            shortName = RStr(pool, "ShortName");
            if (string.IsNullOrEmpty(shortName)) shortName = pool.CompleteShortName ?? "";
            // CompleteShortName = Round.ShortName + Group.ShortName + Pool.ShortName,
            // e.g. "R2G1P5" — unlike `shortName` above (bare "P5"), this always includes
            // round/group context, which the dashboard prefers to display.
            fullShortName = pool.CompleteShortName ?? "";

            // Try to read Courts collection directly from the Pool/Play object
            bool gotCourts = false;
            try
            {
                var flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
                var courtsVal = pool.GetType().GetProperty("Courts", flags)?.GetValue(pool)
                             ?? pool.GetType().GetField("Courts", flags)?.GetValue(pool);
                if (courtsVal is System.Collections.IEnumerable seq)
                {
                    bool firstC = true;
                    foreach (var c in seq)
                    {
                        if (!firstC) courtsJson.Append(", ");
                        courtsJson.Append($"{{\"courtId\": {RInt(c, "CourtID")}, \"name\": {S(RStr(c, "Name"))}}}");
                        firstC = false;
                        gotCourts = true;
                    }
                }
            }
            catch { }

            // Fall back: derive single court from first scheduled match
            var first = pool.Matches?.FirstOrDefault(m => m.IsScheduled);
            if (first != null)
            {
                date = first.ScheduledStartDateTime.ToString("yyyy-MM-dd");
                if (!gotCourts)
                    courtsJson.Append($"{{\"courtId\": {CourtId(first)}, \"name\": {S(first.ScheduledCourtText ?? "")}}}");
            }
        }
        catch { }
        courtsJson.Append("]");

        List<Standing> standings;
        try { standings = ComputeStandings(pool); }
        catch { standings = new List<Standing>(); }

        // Emit standings in AES's own pool.Teams roster order (the same array
        // used below to build teamAssignments), NOT sorted by any computed or
        // AES-ranked finish position. Sorting by finish position — whether our
        // own win/set-diff/point-diff order, or AES's real FinishRank — makes
        // the array reorder as the pool plays out, which makes pool-flyout rows
        // visibly reshuffle on the dashboard (it writes `teams` straight to the
        // UI with no re-sort of its own). pool.Teams never reorders, so we use
        // it as the stable display order and just attach each team's current
        // computed stats (and its real FinishRank, as a display-only field) by
        // team-name lookup.
        var standingByTeam = new Dictionary<string, Standing>();
        foreach (var st in standings)
            if (!string.IsNullOrEmpty(st.Team)) standingByTeam[st.Team] = st;

        var orderedStandings = new List<Standing>();
        var usedTeams = new HashSet<string>();
        try
        {
            foreach (var ta in pool.Teams)
            {
                string tt;
                try { tt = ta.TeamText; } catch { continue; }
                if (string.IsNullOrEmpty(tt) || !usedTeams.Add(tt)) continue;
                var st = standingByTeam.TryGetValue(tt, out var found) ? found : new Standing { Team = tt };
                st.FinishRank = ta.FinishRank;
                orderedStandings.Add(st);
            }
        }
        catch { }
        // Any team present in computed standings but not in pool.Teams (shouldn't
        // normally happen) is appended at the end so its stats are never dropped.
        foreach (var st in standings)
            if (!string.IsNullOrEmpty(st.Team) && usedTeams.Add(st.Team))
                orderedStandings.Add(st);
        standings = orderedStandings;

        var sb = new StringBuilder("{");
        sb.Append($"\"poolId\":       {pool.PlayID}, ");
        sb.Append($"\"name\":         {S(poolName)}, ");
        sb.Append($"\"shortName\":    {S(shortName)}, ");
        sb.Append($"\"fullShortName\": {S(fullShortName)}, ");
        sb.Append($"\"divisionCode\": {S(divCode)}, ");
        sb.Append($"\"divisionName\": {S(divName)}, ");
        sb.Append($"\"divisionId\":   {N(divisionId)}, ");
        sb.Append($"\"courts\":       {courtsJson}, ");
        sb.Append($"\"date\":         {S(date)}, ");
        sb.Append($"\"goldSpotsCount\": null, ");
        sb.Append($"\"roundShortName\": {S(roundShortName)}, ");
        sb.Append($"\"roundIndex\":     {roundIndex}, ");
        sb.Append($"\"groupName\":      {S(groupName)}, ");
        sb.Append($"\"groupShortName\": {S(groupShortName)}, ");
        sb.Append("\"standings\": [");
        for (int i = 0; i < standings.Count; i++)
        {
            if (i > 0) sb.Append(", ");
            var st = standings[i];
            sb.Append("{");
            sb.Append($"\"team\": {S(st.Team)}, ");
            sb.Append($"\"wins\": {st.Wins}, ");
            sb.Append($"\"losses\": {st.Losses}, ");
            sb.Append($"\"setsWon\": {st.SetsWon}, ");
            sb.Append($"\"setsLost\": {st.SetsLost}, ");
            sb.Append($"\"ptsFor\": {st.PtsFor}, ");
            sb.Append($"\"ptsAgainst\": {st.PtsAgainst}, ");
            sb.Append($"\"finishRank\": {N(st.FinishRank)}");
            sb.Append("}");
        }
        sb.Append("], ");

        // Exploratory addition: the pool's Play.TeamAssignment seed chain
        // (EntrySeed -> FinishRank -> ExitSeed). Play.Teams is public, no reflection needed.
        sb.Append("\"teamAssignments\": [");
        var poolTeamAssignments = pool.Teams;
        for (int i = 0; i < poolTeamAssignments.Length; i++)
        {
            if (i > 0) sb.Append(", ");
            sb.Append(TeamAssignmentJson(poolTeamAssignments[i]));
        }
        sb.Append("]}");
        return sb.ToString();
    }

    // ── Bracket ────────────────────────────────────────────────────────────
    // Uses Bracket.PlotMatchPositions() — the same method AES uses internally
    // to build the bracket tree — giving us the recursive TopSource/BottomSource
    // structure with X/Y coordinates, matching the public results API format.

    static string BracketJson(Bracket bracket)
    {
        string divCode = "", divName = "", bracketName = "", bracketShortName = "", notes = "";
        bool isPlayoff = false;
        int roundIndex = -1;
        string roundShortName = "", groupName = "", groupShortName = "";
        try
        {
            var div  = bracket.OwningGroup?.OwningRound?.OwningDivision;
            divCode  = div?.CodeAlias ?? "";
            divName  = div?.DescriptionAlias ?? "";
            // Bracket (a Play subclass, same as Pool) exposes bare FullName/ShortName
            // reflection properties distinct from the round-prefixed CompleteFullName/
            // CompleteShortName — e.g. FullName "Championship Division" vs CompleteFullName
            // "Round 4 Championship Division"; ShortName "Gold" vs CompleteShortName "R4Gold".
            // Previously this reflected a nonexistent "Name" property (always fell back to
            // CompleteShortName), so "name" and "shortName" were both wrongly emitting the
            // round-prefixed "R4Gold" instead of the bare "Championship Division"/"Gold".
            bracketName = RStr(bracket, "FullName");
            if (string.IsNullOrEmpty(bracketName)) bracketName = bracket.CompleteFullName ?? "";
            bracketShortName = RStr(bracket, "ShortName");
            if (string.IsNullOrEmpty(bracketShortName)) bracketShortName = bracket.CompleteShortName ?? "";
            isPlayoff = bracket.IsPlayoff;
            notes     = bracket.Notes ?? "";
            var round = bracket.OwningGroup?.OwningRound;
            if (round != null)
            {
                roundShortName = round.ShortName ?? "";
                roundIndex = RoundIndex(round);
            }
            groupName = bracket.OwningGroup?.FullName ?? "";
            groupShortName = bracket.OwningGroup?.ShortName ?? "";
        }
        catch { }

        // Build the bracket tree using AES's own layout engine
        List<Bracket.MatchPlacement> placements = null;
        try { placements = bracket.PlotMatchPositions(); }
        catch { }

        // Find root nodes — those not referenced as TopSource or BottomSource by any other
        string rootsJson = "[]";
        if (placements != null && placements.Count > 0)
        {
            var referenced = new HashSet<Bracket.MatchPlacement>(
                placements.Where(p => p.TopSource    != null).Select(p => p.TopSource)
                .Concat(placements.Where(p => p.BottomSource != null).Select(p => p.BottomSource))
            );
            var roots = placements.Where(p => !referenced.Contains(p)).ToList();

            var rSb = new StringBuilder("[");
            for (int i = 0; i < roots.Count; i++)
            {
                if (i > 0) rSb.Append(", ");
                rSb.Append(MatchPlacementJson(roots[i], new HashSet<int>()));
            }
            rSb.Append("]");
            rootsJson = rSb.ToString();
        }

        var sb = new StringBuilder("{");
        sb.Append($"\"bracketId\":    {bracket.PlayID}, ");
        sb.Append($"\"name\":         {S(bracketName)}, ");
        sb.Append($"\"shortName\":    {S(bracketShortName)}, ");
        sb.Append($"\"fullName\":     {S(bracket.CompleteFullName)}, ");
        sb.Append($"\"fullShortName\": {S(bracket.CompleteShortName)}, ");
        sb.Append($"\"divisionCode\": {S(divCode)}, ");
        sb.Append($"\"divisionName\": {S(divName)}, ");
        sb.Append($"\"isPlayoff\":    {B(isPlayoff)}, ");
        sb.Append($"\"notes\":        {S(notes)}, ");
        sb.Append($"\"roundShortName\": {S(roundShortName)}, ");
        sb.Append($"\"roundIndex\":     {roundIndex}, ");
        sb.Append($"\"groupName\":      {S(groupName)}, ");
        sb.Append($"\"groupShortName\": {S(groupShortName)}, ");
        sb.Append($"\"matchCount\":   {bracket.Matches.Length}, ");
        sb.Append($"\"decided\":      {bracket.Matches.Count(m => m.TypeOfOutcome != Match.OutcomeType.Undecided)}, ");
        sb.Append($"\"roots\": {rootsJson}, ");

        // Exploratory addition: the bracket's Play.TeamAssignment seed chain
        // (EntrySeed -> FinishRank -> ExitSeed). Play.Teams is public, no reflection needed.
        sb.Append("\"teamAssignments\": [");
        var bracketTeamAssignments = bracket.Teams;
        for (int i = 0; i < bracketTeamAssignments.Length; i++)
        {
            if (i > 0) sb.Append(", ");
            sb.Append(TeamAssignmentJson(bracketTeamAssignments[i]));
        }
        sb.Append("]");
        sb.Append("}");
        return sb.ToString();
    }

    // ── Team assignment (seed chain) ──────────────────────────────────────
    // Play.TeamAssignment: EntrySeed -> FinishRank (in this play) -> ExitSeed
    // (becomes next round's EntrySeed). Shared by pools and brackets.

    static string TeamAssignmentJson(Play.TeamAssignment ta)
    {
        string team = "";
        try { team = ta.TeamText; } catch { }

        var sb = new StringBuilder("{");
        sb.Append($"\"teamNumber\": {ta.TeamNumber}, ");
        sb.Append($"\"entrySeed\":  {N(ta.EntrySeed)}, ");
        sb.Append($"\"finishRank\": {N(ta.FinishRank)}, ");
        sb.Append($"\"reseedSeed\": {N(ta.ReseedSeed)}, ");
        sb.Append($"\"exitSeed\":   {N(ta.ExitSeed)}, ");
        sb.Append($"\"team\":       {S(team)}");
        sb.Append("}");
        return sb.ToString();
    }

    // ── Division ───────────────────────────────────────────────────────────
    // Division.FinalPlaces: flat, director-assigned list of (GroupName, Seed)
    // entries. Count of entries where GroupName == "Gold" is the gold-bracket
    // advancement count — see WRITE_BACK_EDITOR_SCOPING.md for context.

    static string DivisionJson(Division div)
    {
        var sb = new StringBuilder("{");
        sb.Append($"\"divisionCode\": {S(div.CodeAlias)}, ");
        sb.Append($"\"divisionName\": {S(div.DescriptionAlias)}, ");
        sb.Append("\"finalPlaces\": [");
        var finalPlaces = div.FinalPlaces;
        for (int i = 0; i < finalPlaces.Length; i++)
        {
            if (i > 0) sb.Append(", ");
            var fp = finalPlaces[i];
            string team = "";
            try { team = fp.TeamText; } catch { }

            sb.Append("{");
            sb.Append($"\"absoluteRank\": {fp.AbsoluteRank}, ");
            sb.Append($"\"groupName\":    {S(fp.GroupName)}, ");
            sb.Append($"\"groupRank\":    {fp.GroupRank}, ");
            sb.Append($"\"overallRank\":  {fp.OverallRank}, ");
            sb.Append($"\"seed\":         {N(fp.Seed)}, ");
            sb.Append($"\"team\":         {S(team)}");
            sb.Append("}");
        }
        sb.Append("]}");
        return sb.ToString();
    }

    static string MatchPlacementJson(Bracket.MatchPlacement mp, HashSet<int> visited)
    {
        if (mp?.Match == null) return "null";
        if (!visited.Add(mp.Match.MatchID)) return "null";

        var m = mp.Match;

        // Include all set slots so renderer knows max sets
        var allSets = m.Sets;
        var setsJson = new StringBuilder("[");
        for (int i = 0; i < allSets.Length; i++)
        {
            if (i > 0) setsJson.Append(", ");
            setsJson.Append($"{{\"team1\": {N(allSets[i].FirstTeamScore)}, " +
                            $"\"team2\": {N(allSets[i].SecondTeamScore)}}}");
        }
        setsJson.Append("]");

        // Recurse — copy visited so sibling branches don't block each other
        string topSrc = mp.TopSource    != null
            ? MatchPlacementJson(mp.TopSource,    new HashSet<int>(visited)) : "null";
        string botSrc = mp.BottomSource != null
            ? MatchPlacementJson(mp.BottomSource, new HashSet<int>(visited)) : "null";

        var sb = new StringBuilder("{");
        sb.Append($"\"x\":            {mp.X?.ToString("F1") ?? "null"}, ");
        sb.Append($"\"y\":            {mp.Y?.ToString("F1") ?? "null"}, ");
        sb.Append($"\"reversed\":     {B(mp.Reversed)}, ");
        sb.Append($"\"doubleCapped\": {B(mp.DoubleCapped)}, ");
        sb.Append("\"match\": {");
        sb.Append($"\"matchId\":      {m.MatchID}, ");
        sb.Append($"\"shortName\":    {S(m.CompleteShortName)}, ");
        sb.Append($"\"fullName\":     {S(m.CompleteFullName)}, ");
        sb.Append($"\"team1\":        {S(m.FirstTeamText)}, ");
        sb.Append($"\"team2\":        {S(m.SecondTeamText)}, ");
        sb.Append($"\"courtId\":      {CourtId(m)}, ");
        sb.Append($"\"courtName\":    {S(m.ScheduledCourtText)}, ");
        sb.Append($"\"startTime\":    {S(m.ScheduledStartDateTime.ToString("o"))}, ");
        sb.Append($"\"endTime\":      {S(m.ScheduledEndDateTime.ToString("o"))}, ");
        sb.Append($"\"outcome\":      {S(m.TypeOfOutcome.ToString())}, ");
        sb.Append($"\"decided\":      {B(m.TypeOfOutcome != Match.OutcomeType.Undecided)}, ");
        sb.Append($"\"firstTeamWon\": {B(m.FirstTeamWon)}, ");
        sb.Append($"\"scoreText\":    {S(m.ScoreText)}, ");
        sb.Append($"\"sets\":         {setsJson}");
        sb.Append("}, ");
        sb.Append($"\"topSource\":    {topSrc}, ");
        sb.Append($"\"bottomSource\": {botSrc}");
        sb.Append("}");
        return sb.ToString();
    }

    // ── Standings ──────────────────────────────────────────────────────────

    class Standing
    {
        public string Team;
        public int Wins, Losses, SetsWon, SetsLost, PtsFor, PtsAgainst;
        public int? FinishRank;
    }

    static List<Standing> ComputeStandings(Pool pool)
    {
        var wins = new Dictionary<string, int>();
        var losses = new Dictionary<string, int>();
        var sw = new Dictionary<string, int>();
        var sl = new Dictionary<string, int>();
        var pf = new Dictionary<string, int>();
        var pa = new Dictionary<string, int>();

        void Inc(Dictionary<string, int> d, string k, int by = 1)
        {
            if (!d.ContainsKey(k)) d[k] = 0;
            d[k] += by;
        }

        // Every team scheduled into the pool should appear in standings (with a
        // 0-0 record) even before it has played — not just teams with a decided
        // win/loss — so the roster doesn't shrink/disappear mid-pool-play.
        var teams = new List<string>();
        var seenTeams = new HashSet<string>();
        void Reg(string t) { if (!string.IsNullOrEmpty(t) && seenTeams.Add(t)) teams.Add(t); }

        var allMatches = (pool.Matches ?? new Match[0])
            .Where(m => !string.IsNullOrEmpty(m.FirstTeamText) && !string.IsNullOrEmpty(m.SecondTeamText))
            .ToArray();

        foreach (var m in allMatches) { Reg(m.FirstTeamText); Reg(m.SecondTeamText); }

        // Pool.Matches concatenates the pool's own round-robin matches with any
        // matches from its internal tiebreaker bracket (Pool.PlayoffBracket) —
        // used to resolve 2-3 way ties via head-to-head mini-bracket play, not
        // to be confused with a division's actual playoff/gold bracket. Those
        // tiebreaker matches must NOT count toward standings. IsFromPlayoffBracket
        // identifies them exactly (same filter AES's own Play.TeamStats and
        // Division.TeamStats use internally), instead of inferring by count.
        var regularMatches = allMatches.Where(m => !m.IsFromPlayoffBracket);

        foreach (var m in regularMatches)
        {
            string t1 = m.FirstTeamText, t2 = m.SecondTeamText;

            if (m.TypeOfOutcome == Match.OutcomeType.Undecided) continue;

            if (m.FirstTeamWon)  { Inc(wins, t1); Inc(losses, t2); }
            if (m.SecondTeamWon) { Inc(wins, t2); Inc(losses, t1); }

            foreach (var s in m.Sets)
            {
                int? s1 = s.FirstTeamScore, s2 = s.SecondTeamScore;
                if (!s1.HasValue || !s2.HasValue) continue;
                Inc(pf, t1, s1.Value); Inc(pa, t1, s2.Value);
                Inc(pf, t2, s2.Value); Inc(pa, t2, s1.Value);
                if (s1 > s2) { Inc(sw, t1); Inc(sl, t2); }
                else if (s2 > s1) { Inc(sw, t2); Inc(sl, t1); }
            }
        }

        int G(Dictionary<string, int> d, string k) => d.TryGetValue(k, out var v) ? v : 0;

        // This win/set-diff/point-diff order is discarded by PoolJson() — it only
        // uses this list to look up each team's computed stats by name, then
        // re-emits them in AES's own pool.Teams roster order (stable across
        // snapshots), attaching AES's real FinishRank per-team as a display field.
        return teams
            .Select(t => new Standing { Team=t, Wins=G(wins,t), Losses=G(losses,t),
                                      SetsWon=G(sw,t), SetsLost=G(sl,t), PtsFor=G(pf,t), PtsAgainst=G(pa,t) })
            .OrderByDescending(s => s.Wins)
            .ThenByDescending(s => s.SetsWon - s.SetsLost)
            .ThenByDescending(s => s.PtsFor - s.PtsAgainst)
            .ToList();
    }

    // ── JSON primitives ────────────────────────────────────────────────────

    static string S(string s) => s == null ? "null"
        : "\"" + s.Replace("\\","\\\\").Replace("\"","\\\"")
                  .Replace("\r","").Replace("\n"," ") + "\"";
    static string N(int? n) => n.HasValue ? n.Value.ToString() : "null";
    static string B(bool b) => b ? "true" : "false";

    static void AppendArray<T>(StringBuilder sb, string key,
        IEnumerable<T> items, Func<T,string> toJson)
    {
        sb.Append($"  \"{key}\": [");
        bool first = true;
        foreach (var item in items)
        {
            if (!first) sb.Append(", ");
            sb.Append(toJson(item));
            first = false;
        }
        sb.AppendLine("],");
    }

    static void AppendArrayLast<T>(StringBuilder sb, string key,
        IEnumerable<T> items, Func<T,string> toJson)
    {
        sb.Append($"  \"{key}\": [");
        bool first = true;
        foreach (var item in items)
        {
            if (!first) sb.Append(", ");
            sb.Append(toJson(item));
            first = false;
        }
        sb.AppendLine("]");
    }
}