# Design notes — how PingMonitor was built, and why

This document tells the story behind the code: what I needed, what I tried, what I threw away, and the bugs that taught me something. The [README](../README.md) explains *what* the app does; this explains *why* it ended up this way.

---

## Where it started

I am the only IT person at a public healthcare facility, responsible for roughly 140 networked devices at the time: workstations, printers, IP phones, switches, access points, a NAS and a PBX. I had been using PingInfoView, which works, but it shows devices as a list, has no history and treats one lost packet the same as a dead switch.

I wanted something I could leave open on a second monitor all day and understand in five seconds. The first idea list (from brainstorming) had a search box, CSV export, availability charts, a "physical view" toggle and five colors. I cut most of it:

- **No search box.** With every device visible at once and filters by category, I would never type into it.
- **No export.** If I need evidence of an outage, I take a screenshot.
- **No charts.** They look good in a demo but do not help me find a failure faster.

The rule I kept repeating to myself: *this is a tool to glance at while I work, not a dashboard product.* Most of the decisions below come from that rule.

---

## Decision 1 — Native ICMP through `ctypes`, not a ping library

**Problem.** The obvious Python libraries (`ping3`, `icmplib`) use raw sockets, and raw sockets on Windows require running as administrator. I run this from my normal session on the support PC, not as an elevated service. Calling `ping.exe` once per device every 3 seconds was also out: spawning ~140 processes per cycle is slow and fragile.

**Decision.** Call `IcmpCreateFile` / `IcmpSendEcho` from Windows' own `iphlpapi.dll` through `ctypes`. It needs no admin rights and no extra dependency. One ICMP handle is opened when the scan thread starts and reused for every ping, instead of being opened and closed per request.

**What it cost me.** The first version crashed with `access violation reading 0x00000000C68F9344`. I had not declared `argtypes` / `restype` for the DLL functions, so `ctypes` assumed 32-bit integers. `IcmpCreateFile` returns a 64-bit `HANDLE` on modern Windows; it was being silently truncated, and the corrupted handle crashed `IcmpSendEcho`. Declaring every signature explicitly fixed it. That lesson shaped the rest of the project: every later `ctypes` call (DPAPI, `WNetAddConnection2W`) declares its types from the start.

---

## Decision 2 — The blink *is* the confirmation window

**Problem.** My first idea was purely visual: when a device fails, make it blink for about five seconds so my eye catches the change, then turn solid red. But a single lost ICMP packet is normal, especially on Wi-Fi devices like phones and access points. Alerting on the first failure would mean false alarms and pointless beeps all day.

**Decision.** Turn the blink into the actual confirmation step:

1. First failed ping → the circle blinks. Nothing is recorded, no sound, it does not count in the statistics.
2. Two more consecutive failures (3 in total) → the outage is confirmed: solid red, event saved to SQLite, sound alert.

I defined it as **N consecutive failures, not a number of seconds**. If I ever change the scan interval from 3 s to 1 s or 10 s, the logic still behaves correctly without touching anything else.

A second, softer signal came out of the same thinking: a device that answers *now* but failed ≥ 20 % of its last 10 pings is shown in orange (packet loss). That catches a flaky cable or a congested port before it becomes a full outage.

---

## Decision 3 — Grey means "muted", not "unused"

**Problem.** The grid can hold up to 254 devices (a full /24), but I only monitor what is in the inventory. An early proposal used grey for unassigned slots. That did not match my real need, which was: *I know this device is going to fail — I'm working on it, or it's broken and waiting for a replacement — and I don't want it bothering me.*

**Decision.** Only devices in the inventory are drawn. Grey means **muted**: the device is still pinged and its outages are still recorded, but it is excluded from alerts, from the statistics and from the "top disconnections" ranking. Muted outages are stored with a `muted=1` flag instead of being discarded, so the ranking stays clean but the full history is still there if I ever need to reconstruct what happened to a device.

The muted state is written back to the inventory CSV, so restarting the app does not silently re-enable every alert.

---

## Decision 4 — One custom-painted widget instead of 140 widgets

**Problem.** The natural Qt approach is one `QLabel` or `QPushButton` per device. With 140–254 of them repainting every 3 seconds, plus blinking, the UI starts to feel heavy.

**Decision.** The whole grid is a single `QWidget` with a custom `paintEvent`. It calculates how many circles fit in the current window width and reflows automatically when the window is resized. Hover tooltips, double-click and right-click are resolved by hit-testing the mouse coordinates against the circle positions.

More code up front, but the grid stays smooth and uses the full width of the screen — which is what I wanted: something that looks like the status LEDs on a switch, not a list.

---

## Decision 5 — Threads talk through Qt signals, not locks

The ping loop runs in a `QThread` so the window never freezes. Results reach the GUI through Qt signals. Because the receiving methods live on the main window (an object owned by the GUI thread), Qt automatically queues those calls onto the main thread.

The practical result: **all SQLite access happens on a single thread**, and I never had to write a single mutex around the database.

---

## Decision 6 — A CSV for the inventory, SQLite for history

- **Inventory → one CSV file.** I already maintain the fleet in a spreadsheet, so a CSV is something I can edit in Excel or generate from the existing asset list. The first design had two files (names, and switch/port data); I merged them into one row per IP to avoid keeping two files in sync.
- **History → SQLite.** Outage events need queries ("top disconnections in the last 7 days"), and SQLite gives that with zero installation.

**A bug that came from this split.** If the app was closed while a device was down, its event stayed open in the database. On the next start, the device objects were created from scratch, the engine needed three new failures to "rediscover" the outage and created a *second* event, leaving the first one open forever. The fix: on start-up, any device with an open event is restored to the offline state before scanning begins. If it is still down, the same event continues; if it recovered while the app was closed, the event is closed with its real duration.

---

## Decision 7 — Alerts that respect the person receiving them

- **Sound:** one beep per new confirmed outage, with an **8-second cooldown**. When a switch fails it takes many devices down at once; without the cooldown, that is a burst of overlapping beeps exactly when I need to think.
- **Telegram:** I did not want a phone notification per device. The bot only fires when **more than 8 devices fail within 60 seconds** — the signature of a switch or zone failure, which is the kind of event worth interrupting me for. It fires once per "storm" and re-arms only after the count drops back below the threshold. I tested the logic with simulated outages (7 → no alert, 9 → one alert, 11 → still one alert) before trying it on the real network.

---

## Decision 8 — Right-click actions, chosen by risk

Once the monitor worked, the natural next question was: *I can see the PC is in trouble — can I act on it without switching tools?* Candidate actions were ranked by risk:

1. **Open `\\ip\c$`** — no risk, immediate value.
2. **Wake-on-LAN** — useful, but it only works on PCs that have it enabled in firmware, so a failure can mean "hardware not configured" rather than "code broken".
3. **Restart** — useful, but needs a serious confirmation step.
4. **Shut down** — **dropped.** Once I saw it ranked next to the others, I realized I rarely need it, and having it one click away was not worth the risk.

Along the way I added one of my own: **View processes**, to see what is eating a PC's memory without walking to it. It uses `Invoke-Command` over WinRM — the same channel (port 5985) my Ansible automation already uses for the fleet — so no new firewall rule was needed. The older `Get-Process -ComputerName` was not an option; it no longer exists in modern PowerShell.

Other details:

- **Actions depend on the category.** Restart or Wake-on-LAN make no sense on a printer, so PC actions only appear for the `PC` category. Later I asked for an *Open in browser* entry for printers and phones, which have web admin pages.
- **Actions depend on the state.** Wake-on-LAN is only enabled when the PC is *not* answering; Restart only when it *is*.
- **Restart uses `Restart-Computer`, not `Invoke-Command`.** When a machine reboots, the connection drops, and with `Invoke-Command` that looks like a transport error even when the restart worked. The confirmation dialog shows the device name and IP and defaults to **No**, so an accidental Enter never reboots anyone's PC.
- **One fix after deployment:** a restart failed on one PC with an RPC error; forcing `-Protocol WSMan` made it consistent.

---

## Decision 9 — Emergency mode is manual, not automatic

**Problem.** Sometimes domain authentication fails on a specific PC (for example, a broken trust relationship). The fallback is that PC's local Administrator account.

**The tempting option** was to retry automatically with the local account whenever the domain attempt failed. I chose not to, for three reasons:

- **Audit trail.** Actions performed with my domain session are logged under my real username on the remote PC. Actions done with a shared local Administrator account all look the same, so nobody can tell later who restarted what. In a regulated environment, that traceability matters.
- **Kerberos vs NTLM.** The domain session uses Kerberos; the local account falls back to NTLM, which is older and exposed to attacks like pass-the-hash and relay. It should be used only when there is no other option, not silently by default.
- **Explorer cannot report failures back.** When `explorer.exe \\pc\c$` fails, it shows its own error dialog to the user; the app never finds out. There is no reliable way to detect the failure and retry.

So the local-account actions are separate menu entries that I choose on purpose. I originally called it "emergency mode" and later renamed it **local mode**: both paths use administrator privileges, so "admin mode" would not distinguish anything, while "local" describes the real difference — the PC's own account versus my domain session. Normal actions sit at the top of the menu, local ones at the bottom.

**Storing the passwords.** The fleet uses three groups of local admin passwords, so each PC has a `GrupoCredencial` in the inventory. Passwords are encrypted with Windows **DPAPI** (`CryptProtectData`, through `ctypes` again, so no extra dependency) in **machine scope** instead of user scope. When I am on vacation, a colleague covers for me from the same support PC, signing in with their own Windows account. Machine scope lets either account use local mode on that PC — no passwords to hand over — while the file stays unreadable if it is copied to another machine. DPAPI decryption is a single native call on a few hundred bytes — effectively instant.

**The vulnerability I found before publishing.** When I prepared this repository, a review of the code showed that local mode was passing the decrypted password *as a command-line argument* (`net use … PASSWORD`, and an inline `ConvertTo-SecureString 'PASSWORD'` for PowerShell). Command-line arguments are visible in the process list to other users and administrators, and PowerShell script-block logging can store them in the event log. I fixed it before going public:

- The `C$` connection now calls the Win32 API `WNetAddConnection2W` directly — no child process, no arguments.
- PowerShell receives the password in an environment variable that exists only in its own process; the script reads it, converts it, and deletes it immediately. The visible command contains only the variable's *name*.

The full reasoning and the remaining known limitation are documented in [`conexion_segura.py`](../conexion_segura.py).

---

## Packaging and deployment

- **PyInstaller, one-folder build.** One executable, nothing to install on the support PC. PyInstaller 6 puts its internal files in an `_internal` subfolder, and in my build `sys.executable` resolved *inside* that folder — so the app looked for `inventario.csv` in the wrong place and crashed on first launch. The fix detects `_internal` and moves up one level, so all data files live next to the `.exe`.
- **Updating without losing data.** The deployed folder accumulates real data (history, encrypted credentials, the edited inventory). Updates replace only the `.exe` and `_internal`, never the data files — a near-mistake that taught me to treat the deployed folder as production, not as a build output.
- **Antivirus.** A freshly compiled, unsigned `.exe` has no reputation, and Windows Defender blocked it at first. The proper path in a managed environment is an exception request through the security team, not disabling protection.
- **Reproducible environment.** The exact dependency versions are pinned, and I keep an offline cache of the packages (a "wheelhouse") so the build environment can be rebuilt on a machine without internet access. The cache is not part of this repository.

---

For what the app *cannot* do, see [LIMITATIONS.md](LIMITATIONS.md).
