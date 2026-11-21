/*
 * Publish.cs  —  spike: publish a .vsf to AES's servers without the Scheduler
 *
 *   AESBridge.exe --publish <file.vsf> --key <ServerSafeKey> [--force-results-import]
 *
 * Does what the Scheduler's "Update Now" does (NetworkControl.UpdateThreadLoop), using
 * AES's own connection class (Colib.Util.NetConnection) from the referenced assembly:
 *   1. Open syncservice.advancedeventsystems.com:18743 with "SyncService Protocol v3.1"
 *   2. AuthenticationCommit  string[] { email, password }
 *   3. PostEvent             object[] { SchedulerFile.Save(), forceResultsImport }
 * then waits for the server's answer (PostEventSuccessful / Failed / Denied).
 *
 * Safety:
 *   - --key must equal the file's ServerSafeKey (the id in the event's results URL), so a
 *     file can't be published to an event you didn't name.
 *   - It shows the event and asks you to type PUBLISH before connecting.
 *   - The email and password are typed at the console (the password masked), held only in
 *     memory for this run, never printed, logged, passed on the command line or saved.
 * Publishing replaces the whole event online. Run it only by hand, on a test event.
 */

using System;
using System.Collections.Concurrent;
using System.IO;
using System.Text;
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
        var replies = new BlockingCollection<int>();
        var nc = new NetConnection();
        nc.OnDataReceived += (c, cmd, type, data) => replies.Add(cmd);
        nc.OnConnectionLost += c => replies.Add(-1);

        try
        {
            Console.WriteLine($"Connecting to {Host}:{Port} ...");
            if (!nc.Open(Host, Port, ProtocolPassword)) { Console.Error.WriteLine("Could not connect."); return 1; }
            nc.StartReceiveThread();

            nc.SendCommand(AuthenticationCommit, (object)new string[] { email, password });
            nc.SendCommand(PostEvent, (object)new object[] { body, force });
            Console.WriteLine($"Sent sign-in and PostEvent ({body.Length} bytes). Waiting for the server ...");

            DateTime deadline = DateTime.UtcNow.AddSeconds(90);
            while (DateTime.UtcNow < deadline)
            {
                if (!replies.TryTake(out int cmd, TimeSpan.FromSeconds(1))) continue;
                switch (cmd)
                {
                    case AuthenticationValid: Console.WriteLine("Server:    sign-in accepted"); break;
                    case AuthenticationInvalid: Console.WriteLine("Server:    sign-in REJECTED (email or password)"); return 1;
                    case AuthenticationFailed: Console.WriteLine("Server:    sign-in error on the server"); return 1;
                    case PostEventSuccessful: Console.WriteLine($"Server:    PostEventSuccessful at {DateTime.Now:HH:mm:ss}"); return 0;
                    case PostEventFailed: Console.WriteLine("Server:    PostEventFailed (a server error while posting)"); return 1;
                    case PostEventDenied: Console.WriteLine("Server:    PostEventDenied (this account may not post this event)"); return 1;
                    case ReceivedBadCommand: Console.WriteLine("Server:    ReceivedBadCommand"); return 1;
                    case -1: Console.WriteLine("Server:    connection closed"); return 1;
                    default: Console.WriteLine($"Server:    command {cmd} (not handled)"); break;
                }
            }
            Console.WriteLine("No answer to PostEvent within 90 seconds.");
            return 1;
        }
        catch (InvalidPasswordException)
        {
            Console.Error.WriteLine("The server refused this client's protocol version (the Scheduler's 'version not compatible').");
            return 1;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Error: {ex.GetType().Name}: {ex.Message}");
            return 1;
        }
        finally
        {
            try { nc.Close(); } catch { }
            password = null;
            email = null;
        }
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
