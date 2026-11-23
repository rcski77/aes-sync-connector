/*
 * ReseedFeed.cs  —  the "reseeds" block of tournament_data.json (Relay hosting phase 4b)
 *
 * One entry per round that has reseeds (a play in it with a finish rank flagged RankWillBeReseeded), read from
 * AES's own model so the dashboard never re-derives AES's rules:
 *   - divisionRounds: every round of the age group (id, short name, position), as rounds can share a name.
 *   - plays:    the round's plays with reseeds: kind, the flagged ranks and the seed each rank exits with
 *               (rankSeeds[r-1] = Play.SortedSeeds[r-1], what ExitSeedIgnoringReseed gives a team at rank r).
 *               Brackets add their matches (sides as slot / winnerOf / loserOf) for the elimination rank blocks.
 *   - teams:    every team at a flagged rank with a finish (Round.ReseedingTeams), plus any team still holding a
 *               ReseedSeed it no longer reseeds with (a finish changed after the reseed): natural seed
 *               (ExitSeedIgnoringReseed), ReseedSeed, ExitSeed, record and AES's match %, set %, point ratio
 *               (Play.TeamAssignment.TeamStats, null where AES's would be NaN), club, region, team code.
 *   - played:   team-code pairs with a decided match in the age group's rounds up to and including this one (replays).
 *   - seedsOnOffer: Round.ActualReseedingSeeds. suggestedOrder: Round.SuggestedReseedOrder as play/slot pairs.
 *   - next:     the plays the reseeded seeds enter (Division.GetNextPlayForSeed: the first later round with a slot
 *               of that entry seed), every slot with its entry seed and current team (null while pending), and
 *               their matches, sides as above, with whether each is decided.
 * Never names a person; teams are named as everywhere else in the parse.
 */

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using AES.Scheduler.Model;

static class ReseedFeed
{
    static string S(string s) => AESBridge.S(s);
    static string N(int? n) => AESBridge.N(n);
    static string B(bool b) => AESBridge.B(b);

    static string D(double d) =>
        double.IsNaN(d) || double.IsInfinity(d) ? "null" : Math.Round(d, 6).ToString("R", CultureInfo.InvariantCulture);

    static bool Reseedable(Play p) => p.HasReseeds && !(p is Bracket b && b.IsPlayoff);

    internal static string Json(SchedulerFile sf)
    {
        var rounds = new List<string>();
        foreach (Division d in sf.Event.Divisions)
            foreach (Round r in d.Rounds)
            {
                Play[] plays;
                try { plays = r.Plays.Where(Reseedable).ToArray(); } catch { continue; }
                if (plays.Length == 0) continue;
                try { rounds.Add(RoundJson(d, r, plays)); }
                catch (Exception ex) { Console.Error.WriteLine($"reseeds: round {r.RoundID} skipped ({ex.GetType().Name}: {ex.Message})"); }
            }
        return "[" + string.Join(", ", rounds) + "]";
    }

    static int?[] SortedSeeds(Play p) =>
        p.Teams.OrderBy(t => !t.EntrySeed.HasValue).ThenBy(t => t.EntrySeed).Select(t => t.EntrySeed).ToArray();

    static string RoundJson(Division d, Round r, Play[] plays)
    {
        var sb = new StringBuilder("{");
        sb.Append($"\"roundId\": {r.RoundID}, ");
        sb.Append($"\"roundShortName\": {S(r.ShortName ?? "")}, ");
        sb.Append($"\"roundName\": {S(r.FullName ?? "")}, ");
        sb.Append($"\"roundIndex\": {AESBridge.RoundIndex(r)}, ");
        sb.Append($"\"divisionCode\": {S(d.CodeAlias ?? "")}, ");
        sb.Append($"\"divisionName\": {S(d.DescriptionAlias ?? "")}, ");
        sb.Append($"\"divisionId\": {d.EventDivisionAssignmentID}, ");
        // Every round of the age group in AES's order, so the dashboard can tell apart two rounds both named "R2".
        sb.Append("\"divisionRounds\": [");
        sb.Append(string.Join(", ", d.Rounds.Select(x =>
            $"{{\"roundId\": {x.RoundID}, \"shortName\": {S((x.ShortName ?? "").Trim())}, \"index\": {AESBridge.RoundIndex(x)}}}")));
        sb.Append("], ");

        // Plays with reseeds.
        sb.Append("\"plays\": [");
        sb.Append(string.Join(", ", plays.Select(p =>
        {
            int?[] seeds = SortedSeeds(p);
            Play.TeamAssignment[] teams = p.Teams;
            var ranks = Enumerable.Range(0, teams.Length).Where(i => teams[i].RankWillBeReseeded).Select(i => i + 1).ToArray();
            var ps = new StringBuilder("{");
            ps.Append($"\"playId\": {p.PlayID}, ");
            ps.Append($"\"kind\": {S(p is Pool ? "pool" : "bracket")}, ");
            ps.Append($"\"shortName\": {S(p.CompleteShortName ?? "")}, ");
            // The play's group (Play.OwningGroup): a round's pools reseed per group, e.g. "Club" and "Prime" 2nds apart.
            ps.Append($"\"groupId\": {N(p.OwningGroup?.GroupID)}, ");
            ps.Append($"\"groupName\": {S(AESBridge.Blank2Null(p.OwningGroup?.ShortName) ?? AESBridge.Blank2Null(p.OwningGroup?.FullName))}, ");
            ps.Append($"\"teamCount\": {teams.Length}, ");
            ps.Append($"\"reseedRanks\": [{string.Join(", ", ranks)}], ");
            ps.Append($"\"rankSeeds\": [{string.Join(", ", seeds.Select(N))}]");
            if (p is Bracket) ps.Append($", \"matches\": {MatchesJson(p)}");
            ps.Append("}");
            return ps.ToString();
        })));
        sb.Append("], ");

        // Reseeding teams, then any team still holding a reseed seed it no longer reseeds with.
        Play.TeamAssignment[] reseeding = r.ReseedingTeams;
        var stale = plays.SelectMany(p => p.Teams).Where(t => t.ReseedSeed.HasValue && !reseeding.Contains(t)).ToArray();
        sb.Append("\"teams\": [");
        sb.Append(string.Join(", ", reseeding.Select(t => TeamJson(t, true)).Concat(stale.Select(t => TeamJson(t, false)))));
        sb.Append("], ");

        // Replays: the pairs that played each other (a decided match) in this age group's rounds up to and including this
        // one, by AES's round order. The next round and later ones are what's being planned, so they never count.
        int index = AESBridge.RoundIndex(r);
        var played = new SortedSet<string>(StringComparer.Ordinal);
        foreach (Round x in d.Rounds)
        {
            if (AESBridge.RoundIndex(x) > index) continue;
            foreach (Match m in x.Matches)
            {
                if (m.TypeOfOutcome == Match.OutcomeType.Undecided) continue;
                string a = AESBridge.TeamCode(m.FirstTeam), b = AESBridge.TeamCode(m.SecondTeam);
                if (a == null || b == null || a == b) continue;
                played.Add(string.CompareOrdinal(a, b) < 0 ? S(a) + ", " + S(b) : S(b) + ", " + S(a));
            }
        }
        sb.Append("\"played\": [" + string.Join(", ", played.Select(x => "[" + x + "]")) + "], ");

        sb.Append($"\"seedsOnOffer\": [{string.Join(", ", r.ActualReseedingSeeds)}], ");
        sb.Append("\"suggestedOrder\": [");
        sb.Append(string.Join(", ", r.SuggestedReseedOrder.Select(t => $"{{\"playId\": {t.OwningPlay.PlayID}, \"teamNumber\": {t.TeamNumber}}}")));
        sb.Append("], ");

        // The plays the reseeded seeds go to next.
        var seedsOut = new HashSet<int>();
        foreach (Play p in plays)
        {
            int?[] seeds = SortedSeeds(p);
            Play.TeamAssignment[] teams = p.Teams;
            for (int i = 0; i < teams.Length; i++)
                if (teams[i].RankWillBeReseeded && i < seeds.Length && seeds[i].HasValue) seedsOut.Add(seeds[i].Value);
        }
        var nextPlays = new List<Play>();
        foreach (int seed in seedsOut.OrderBy(x => x))
        {
            Play.TeamAssignment slot = NextSlotForSeed(r.NextRound, seed);
            if (slot != null && !nextPlays.Contains(slot.OwningPlay)) nextPlays.Add(slot.OwningPlay);
        }
        sb.Append("\"next\": [");
        sb.Append(string.Join(", ", nextPlays.Select(NextPlayJson)));
        sb.Append("]}");
        return sb.ToString();
    }

    // Division.GetNextPlayForSeed (internal): the first round from `r` on with a slot of this entry seed.
    static Play.TeamAssignment NextSlotForSeed(Round r, int seed)
    {
        for (Round round = r; round != null; round = round.NextRound)
        {
            Play.TeamAssignment found = round.Plays.SelectMany(p => p.Teams).FirstOrDefault(t => t.EntrySeed == seed);
            if (found != null) return found;
        }
        return null;
    }

    static string TeamJson(Play.TeamAssignment t, bool reseeding)
    {
        TeamStats st = t.TeamStats;
        var club = AESBridge.TeamClub(t);
        string name = "";
        try { name = AESBridge.TeamName(t, () => t.TeamText); } catch { }
        var sb = new StringBuilder("{");
        sb.Append($"\"playId\": {t.OwningPlay.PlayID}, ");
        sb.Append($"\"teamNumber\": {t.TeamNumber}, ");
        sb.Append($"\"reseeding\": {B(reseeding)}, ");
        sb.Append($"\"entrySeed\": {N(t.EntrySeed)}, ");
        sb.Append($"\"finishRank\": {N(t.FinishRank)}, ");
        sb.Append($"\"naturalSeed\": {N(t.ExitSeedIgnoringReseed)}, ");
        sb.Append($"\"reseedSeed\": {N(t.ReseedSeed)}, ");
        sb.Append($"\"exitSeed\": {N(t.ExitSeed)}, ");
        sb.Append($"\"teamCode\": {S(AESBridge.TeamCode(t))}, ");
        // A bracket team's losing match (its finish came from it): the dashboard splits a round of losers by the time of
        // these matches when they didn't all start together.
        int? lostMatchId = null;
        if (t.OwningPlay is Bracket)
        {
            try { lostMatchId = t.OwningPlay.Matches.FirstOrDefault(m => !m.IsFromPlayoffBracket && m.TypeOfOutcome != Match.OutcomeType.Undecided && m.LosingTeam == t)?.MatchID; }
            catch { }
        }
        sb.Append($"\"lostMatchId\": {N(lostMatchId)}, ");
        sb.Append($"\"team\": {S(name)}, ");
        sb.Append($"\"club\": {S(AESBridge.Blank2Null(club?.Name))}, ");
        sb.Append($"\"region\": {S(AESBridge.Blank2Null(club?.RegionAbbr))}, ");
        sb.Append($"\"matchesWon\": {st.MatchesWon}, \"matchesLost\": {st.MatchesLost}, ");
        sb.Append($"\"setsWon\": {st.SetsWon}, \"setsLost\": {st.SetsLost}, ");
        sb.Append($"\"pointsFor\": {st.OurPoints}, \"pointsAgainst\": {st.TheirPoints}, ");
        sb.Append($"\"matchPercent\": {D(st.MatchPercent)}, \"setPercent\": {D(st.SetPercent)}, \"pointRatio\": {D(st.PointRatio)}");
        sb.Append("}");
        return sb.ToString();
    }

    static string NextPlayJson(Play p)
    {
        Round round = p.OwningGroup.OwningRound;
        var sb = new StringBuilder("{");
        sb.Append($"\"playId\": {p.PlayID}, ");
        sb.Append($"\"roundId\": {round.RoundID}, ");
        sb.Append($"\"kind\": {S(p is Pool ? "pool" : "bracket")}, ");
        sb.Append($"\"shortName\": {S(p.CompleteShortName ?? "")}, ");
        sb.Append($"\"fullName\": {S(p.CompleteFullName ?? "")}, ");
        sb.Append("\"slots\": [");
        sb.Append(string.Join(", ", p.Teams.Select(t =>
        {
            var club = AESBridge.TeamClub(t);
            string text = "";
            try { text = AESBridge.TeamName(t, () => t.TeamText); } catch { }
            string code = AESBridge.TeamCode(t);
            return $"{{\"teamNumber\": {t.TeamNumber}, \"entrySeed\": {N(t.EntrySeed)}, \"teamCode\": {S(code)}, " +
                   $"\"team\": {S(text)}, \"club\": {S(code == null ? null : AESBridge.Blank2Null(club?.Name))}, " +
                   $"\"region\": {S(code == null ? null : AESBridge.Blank2Null(club?.RegionAbbr))}}}";
        })));
        sb.Append("], ");
        sb.Append($"\"matches\": {MatchesJson(p)}");
        sb.Append("}");
        return sb.ToString();
    }

    // A play's own matches (not a pool's tiebreaker bracket), each side as AES models it.
    static string MatchesJson(Play p)
    {
        Match[] matches = p.Matches.Where(m => !m.IsFromPlayoffBracket).ToArray();
        return "[" + string.Join(", ", matches.Select(m =>
            $"{{\"matchId\": {m.MatchID}, \"shortName\": {S(m.CompleteShortName ?? "")}, " +
            $"\"a\": {Side(m.FirstTypeOfCompetingTeam, m.FirstTeamNumber, m.FirstTeamSourceMatch)}, " +
            $"\"b\": {Side(m.SecondTypeOfCompetingTeam, m.SecondTeamNumber, m.SecondTeamSourceMatch)}, " +
            $"\"start\": {S(m.IsScheduled ? m.ScheduledStartDateTime.ToString("yyyy-MM-ddTHH:mm", CultureInfo.InvariantCulture) : null)}, " +
            $"\"decided\": {B(m.TypeOfOutcome != Match.OutcomeType.Undecided)}}}")) + "]";
    }

    static string Side(Match.CompetingTeamType type, int number, Match source)
    {
        switch (type)
        {
            case Match.CompetingTeamType.AbsoluteTeamNumber: return $"{{\"slot\": {number}}}";
            case Match.CompetingTeamType.WinnerOfAnotherMatch: return source == null ? "null" : $"{{\"winnerOf\": {source.MatchID}}}";
            case Match.CompetingTeamType.LoserOfAnotherMatch: return source == null ? "null" : $"{{\"loserOf\": {source.MatchID}}}";
            default: return "null";
        }
    }
}
