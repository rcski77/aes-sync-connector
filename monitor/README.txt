Relay Sync
==========

Sends your AES Scheduler tournament to the Relay dashboard while you run the event.

Setup (once per laptop)
  1. Unzip this folder onto the laptop that runs AES Scheduler.
  2. In Relay, open the event's Settings screen, create the Sync client key, and
     download the config file (connector-config-<event>.ini).
  3. Save that .ini file in this folder, right next to "Relay Sync.exe".
  4. Open the .ini in Notepad and type your AES Scheduler password after
     "password =" under [aes]. Save it.
  5. Open AES Scheduler, then double-click "Relay Sync.exe".

Running without AES Scheduler (Host in Relay)
  Relay Sync can show the event from the event's own file, with AES Scheduler closed.
  1. In AES Scheduler, save the event, then close it.
  2. Copy the event's .vsf file into this folder, next to the .ini file.
     Only one .vsf, and it must be this event's.
  3. Start "Relay Sync.exe", then turn on "Host in Relay" on the event's Settings
     screen in Relay. Relay Sync's window says "HOSTING" with the event name, and
     the line under the switch says which file it is hosting.
  4. To go back to AES, turn the switch off (or remove the file) and open the event
     in AES Scheduler again.
  If Relay Sync won't host the file, its window and the Settings screen say why:
  no file, more than one file, a file for a different event, or a file from a
  newer AES than this Relay Sync opens (download the latest Relay Sync).
  The file stays on this laptop; Relay only gets its name and format.
  For now, make changes in AES and save the file in this folder. Relay Sync
  picks up the saved file within a few seconds.

Leave the "app" and "data" folders alone. They hold the files Relay Sync needs
and the working files it creates the first time it runs.

Notes
  - Keep only one .ini file in this folder, and at most one .vsf file.
  - Upgrading from an older, flat download? Unzip over the old folder or into a new one.
    Your .ini still works. Old working files left next to the program
    (scheduler_file.bin, tournament_data.json, ...) are ignored and can be deleted.
  - "app\aes_config.ini.example" lists every setting.

Build: {BUILD}
