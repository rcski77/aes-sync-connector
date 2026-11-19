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

Leave the "app" and "data" folders alone. They hold the files Relay Sync needs
and the working files it creates the first time it runs.

Notes
  - Keep only one .ini file in this folder.
  - Upgrading from an older, flat download? Unzip over the old folder or into a new one.
    Your .ini still works. Old working files left next to the program
    (scheduler_file.bin, tournament_data.json, ...) are ignored and can be deleted.
  - "app\aes_config.ini.example" lists every setting.

Build: {BUILD}
