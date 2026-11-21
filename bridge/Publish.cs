/*
 * Publish.cs  —  publish a .vsf to AES's servers without the Scheduler
 *
 *   AESBridge.exe --publish <file.vsf> --key <ServerSafeKey> [--force-results-import]   (by hand, interactive)
 *   AESBridge.exe --publish-json                                                         (Relay Sync, JSON on stdin)
 *
 * Does what the Scheduler's "Update Now" does (NetworkControl.UpdateThreadLoop), using
 * AES's own connection class (Colib.Util.NetConnection) from the referenced assembly:
 *   1. Open syncservice.advancedeventsystems.com:18743 with "SyncService Protocol v3.1"
 *   2. AuthenticationCommit  string[] { email, password }
 *   3. PostEvent             object[] { SchedulerFile.Save(), forceResultsImport }
 * then waits for the server's answer (PostEventSuccessful / Failed / Denied). The sign-in check
 * (--publish-json "signin") is step 2 alone; the service answers AuthenticationValid/Invalid/Failed,
 * as it does for the Scheduler's own sign-in (NetworkControl's AuthenticationValid handling).
 *
 * Safety:
 *   - The key must equal the file's ServerSafeKey (the id in the event's results URL), so a
 *     file can't be published to an event you didn't name.
 *   - --publish shows the event and asks you to type PUBLISH before connecting, then asks for
 *     the email and password at the console (the password masked).
 *   - --publish-json takes the email and password on stdin only (never the command line or the
 *     environment), and prints one JSON line that never contains them.
 *   - Neither mode prints, logs or saves the credentials.
 * Publishing replaces the whole event online.
 */

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using AES.Scheduler.Model;
using Colib.Util;

static class Publish
{
    const string Host = "syncservice.advancedeventsystems.com";
    const int Port = 18743;
    const string ProtocolPassword = "SyncService Protocol v3.1"; // what the Scheduler opens with

    // SchedulerNetCommand values (AES.Scheduler.Model.Util)
    const int ReceivedBadCommand = 256;
    const int AuthenticationCommit = 8192, AuthenticationValid = 8208, AuthenticationFailed = 8224, AuthenticationInvalid = 8225;
    const int PostEvent = 9216, PostEventSuccessful = 9232, PostEventFailed = 9248, PostEventDenied = 9250;

    // ── --publish (interactive) ────────────────────────────────────────────────

    public static int Run(string[] args)
    {
        string path = null, key = null;
        bool force = false;
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--key" && i + 1 < args.Length) key = args[++i];
            else if (args[i] == "--force-results-import") force = true;
            else if (path == null) path = args[i];
            else { Usage(); return 2; }
        }
        if (path == null || key == null || !File.Exists(path)) { Usage(); return 2; }

        SchedulerFile sf = HostBatch.LoadChecked(File.ReadAllBytes(path), path);
        if (sf == null) return 1;
        string fileKey = sf.ServerSafeKey;

        Console.WriteLine($"Event:     {sf.Event.Name}");
        Console.WriteLine($"Key:       {fileKey}  (https://results.advancedeventsystems.com/event/{fileKey}/home)");
        Console.WriteLine($"Hand-built:{(sf.Event.ManualAddition ? " yes" : " no")}, event id {sf.Event.EventID}");
        Console.WriteLine($"Contents:  {sf.Teams.Length} teams, {sf.Courts.Length} courts, {sf.Event.Matches.Length} matches");
        Console.WriteLine($"Force results import: {force}");
        if (!string.Equals(fileKey, key, StringComparison.Ordinal))
        {
            Console.Error.WriteLine($"Refusing: this file publishes to key {fileKey}, not the --key you gave.");
            return 1;
        }

        Console.WriteLine();
        Console.WriteLine("This REPLACES the whole event online with this file.");
        Console.Write("Type PUBLISH to continue: ");
        if (Console.ReadLine()?.Trim() != "PUBLISH") { Console.WriteLine("Cancelled."); return 1; }

        Console.Write("AES email: ");
        string email = Console.ReadLine()?.Trim() ?? "";
        Console.Write("AES password: ");
        string password = ReadMasked();
        if (email == "" || password == "") { Console.Error.WriteLine("Email and password are required."); return 1; }

        byte[] body = sf.Save(); // what the Scheduler sends: a fresh save of the open file
        Console.WriteLine($"Connecting to {Host}:{Port} and sending PostEvent ({body.Length} bytes) ...");
        string result = Session(email, password, body, force, out string detail);
        email = null; password = null;
        switch (result)
        {
            case "published": Console.WriteLine($"Server:    PostEventSuccessful at {DateTime.Now:HH:mm:ss}"); return 0;
            case "sign_in_invalid": Console.WriteLine("Server:    sign-in REJECTED (email or password)"); return 1;
            case "sign_in_failed": Console.WriteLine("Server:    sign-in error on the server"); return 1;
            case "post_failed": Console.WriteLine("Server:    PostEventFailed (a server error while posting)"); return 1;
            case "post_denied": Console.WriteLine("Server:    PostEventDenied (this account may not post this event)"); return 1;
            default: Console.WriteLine($"Server:    {result}{(detail == null ? "" : ": " + detail)}"); return 1;
        }
    }

    // ── --publish-json (Relay Sync) ────────────────────────────────────────────
    // Reads ONE JSON object from stdin (never the command line or the environment):
    //   {"action": "signin",  "email": "...", "password": "..."}
    //   {"action": "publish", "email": "...", "password": "...", "file": "<.vsf>", "key": "<ServerSafeKey>",
    //    "forceResultsImport": false}
    // and prints ONE JSON line: {"ok": bool, "result": "<word>", "bytes": N, "detail": "..."}. `result` is a word
    // from Session(), or bad_input / refused_key / load_failed (found before connecting). The line never holds the
    // credentials. Exit 0 whatever the result; 2 for bad arguments.
    public static int RunJson(string[] args)
    {
        if (args.Length != 0) { Console.Error.WriteLine("Usage: --publish-json  (reads one JSON object on stdin)"); return 2; }
        TextWriter stdout = Console.Out;

        Dictionary<string, object> input = null;
        try
        {
            string text;
            using (var r = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false))) text = r.ReadToEnd();
            input = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(text);
        }
        catch { }
        if (input == null) { stdout.WriteLine(Line(false, "bad_input", 0, "stdin is not a JSON object")); return 0; }

        string Str(string k) => input.TryGetValue(k, out object v) ? v as string : null;
        string action = Str("action"), email = Str("email")?.Trim(), password = Str("password");
        string path = Str("file"), key = Str("key");
        bool force = input.TryGetValue("forceResultsImport", out object f) && f is bool fb && fb;
        input = null;
        if ((action != "signin" && action != "publish") || string.IsNullOrEmpty(email) || string.IsNullOrEmpty(password))
        {
            stdout.WriteLine(Line(false, "bad_input", 0, "action must be signin or publish, with an email and a password"));
            return 0;
        }

        byte[] body = null;
        if (action == "publish")
        {
            if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(key) || !File.Exists(path))
            {
                stdout.WriteLine(Line(false, "bad_input", 0, "publish needs an existing file and a key"));
                return 0;
            }
            SchedulerFile sf;
            Console.SetOut(TextWriter.Null); // LoadChecked's notes: only the one result line goes to stdout
            try { sf = HostBatch.LoadChecked(File.ReadAllBytes(path), Path.GetFileName(path)); }
            catch (Exception ex) { sf = null; Console.Error.WriteLine(ex.GetType().Name); }
            finally { Console.SetOut(stdout); }
            if (sf == null) { stdout.WriteLine(Line(false, "load_failed", 0, "the file could not be loaded")); return 0; }
            if (!string.Equals(sf.ServerSafeKey, key, StringComparison.Ordinal))
            {
                stdout.WriteLine(Line(false, "refused_key", 0, "the file publishes to a different event"));
                return 0;
            }
            body = sf.Save();
        }

        string result = Session(email, password, body, force, out string detail);
        email = null; password = null;
        stdout.WriteLine(Line(result == "signed_in" || result == "published", result, body?.Length ?? 0, detail));
        return 0;
    }

    // The sign-in, and with a body the post, over one connection to AES's sync service. Returns a result word:
    //   signed_in (no body) | published | sign_in_invalid | sign_in_failed | post_failed | post_denied |
    //   bad_command | connection_closed | no_answer | connect_failed | version_refused | error
    // `detail` is a short reason for version_refused and error (an exception type, never the credentials).
    static string Session(string email, string password, byte[] body, bool force, out string detail)
    {
        detail = null;
        var replies = new BlockingCollection<int>();
        var nc = new NetConnection();
        nc.OnDataReceived += (c, cmd, type, data) => replies.Add(cmd);
        nc.OnConnectionLost += c => replies.Add(-1);
        try
        {
            if (!nc.Open(Host, Port, ProtocolPassword)) return "connect_failed";
            nc.StartReceiveThread();
            nc.SendCommand(AuthenticationCommit, (object)new string[] { email, password });
            if (body != null) nc.SendCommand(PostEvent, (object)new object[] { body, force });

            DateTime deadline = DateTime.UtcNow.AddSeconds(body != null ? 90 : 30);
            while (DateTime.UtcNow < deadline)
            {
                if (!replies.TryTake(out int cmd, TimeSpan.FromSeconds(1))) continue;
                switch (cmd)
                {
                    case AuthenticationValid: if (body == null) return "signed_in"; break;
                    case AuthenticationInvalid: return "sign_in_invalid";
                    case AuthenticationFailed: return "sign_in_failed";
                    case PostEventSuccessful: return "published";
                    case PostEventFailed: return "post_failed";
                    case PostEventDenied: return "post_denied";
                    case ReceivedBadCommand: return "bad_command";
                    case -1: return "connection_closed";
                    default: break; // anything else the service volunteers
                }
            }
            return "no_answer";
        }
        catch (InvalidPasswordException)
        {
            detail = "the server refused this client's protocol version";
            return "version_refused";
        }
        catch (Exception ex)
        {
            detail = ex.GetType().Name;
            return "error";
        }
        finally
        {
            try { nc.Close(); } catch { }
        }
    }

    static string Line(bool ok, string result, int bytes, string detail) =>
        "{\"ok\": " + (ok ? "true" : "false") + ", \"result\": " + Q(result) +
        (bytes > 0 ? ", \"bytes\": " + bytes : "") + (detail == null ? "" : ", \"detail\": " + Q(detail)) + "}";

    static string Q(string s)
    {
        var sb = new StringBuilder("\"");
        foreach (char c in s ?? "")
        {
            if (c == '"' || c == '\\') sb.Append('\\').Append(c);
            else if (c < ' ') sb.Append("\\u").Append(((int)c).ToString("x4"));
            else sb.Append(c);
        }
        return sb.Append('"').ToString();
    }

    static string ReadMasked()
    {
        var sb = new StringBuilder();
        while (true)
        {
            ConsoleKeyInfo k = Console.ReadKey(true);
            if (k.Key == ConsoleKey.Enter) { Console.WriteLine(); return sb.ToString(); }
            if (k.Key == ConsoleKey.Backspace) { if (sb.Length > 0) { sb.Length--; Console.Write("\b \b"); } continue; }
            if (!char.IsControl(k.KeyChar)) { sb.Append(k.KeyChar); Console.Write('*'); }
        }
    }

    static void Usage() =>
        Console.Error.WriteLine("Usage: --publish <file.vsf> --key <ServerSafeKey> [--force-results-import]");
}
