# Known limitations

Every tool is built around trade-offs. These are the ones I know about in PingMonitor, why they exist, and how I would address them. Some were deliberate choices for a single-site, single-admin tool; others are things I would fix first if the tool had to grow.

---

## 1. Pings are sent one after another

**What happens.** The scan thread pings every device sequentially, waiting up to **800 ms** for each reply before moving on. Healthy devices answer in a few milliseconds, so a normal cycle easily fits in the 3-second interval. But every *unreachable* device costs the full 800 ms timeout.

**Why it matters.** The cost grows exactly when it hurts most. If a switch fails and takes 30 devices down, each cycle takes about 24 extra seconds instead of 3 — so the app becomes slower to confirm outages and to notice recoveries during the very incident I am trying to follow. In the extreme case of a full /24 with every device down, one cycle would take over three minutes.

**Possible fix.** Send pings concurrently: either `IcmpSendEcho2` with asynchronous completion, or a small pool of worker threads each with its own ICMP handle. The confirmation logic would not change; only the scheduling.

## 2. Reachability is not service health

ICMP answers the question *"is this device on the network?"*, not *"is it working?"*. A printer can answer ping with a paper jam; a PC can answer ping while the application users need is frozen. The reverse also happens: Windows Firewall blocks inbound ICMP by default, so a PC without the *File and Printer Sharing (Echo Request – ICMPv4-In)* rule enabled looks permanently down even when it is fine.

This is the main reason my next portfolio project moves to a full monitoring platform (Zabbix with SNMP and agents), where checks can go beyond ping.

## 3. No maintenance windows

Planned work looks exactly like a failure. Restarting a switch for maintenance, or shutting down a group of PCs on purpose, produces the same confirmed outages, sounds and Telegram storm as a real incident.

Muting works per device, not per time window, so there is no way to say *"during Saturday's maintenance window, these outages are planned."* A proper fix would be maintenance periods (a schedule during which confirmed outages are recorded but do not alert), which is also how platforms like Zabbix handle it.

## 4. Built around a single flat /24 network

- Each device is labeled with the **last octet** of its IP. On a network with several subnets, `10.0.1.15` and `10.0.2.15` would both show as `15`.
- **Wake-on-LAN** sends the magic packet to the `.255` broadcast of the device's address, assuming a /24 mask.

Both match the network it was built for. Supporting multiple subnets would need configurable labels and a per-subnet broadcast address.

## 5. Windows-only, single-instance, local data

- The app depends on Windows DLLs (`iphlpapi`, `crypt32`, `mpr`) and on `winsound`, so it only runs on Windows.
- It is a desktop app: it only monitors while it is open on the support PC. Outages that **start and end while the app is closed are never seen**. (Outages that were already open when the app closed are resumed correctly; see [DESIGN.md](DESIGN.md#decision-6--a-csv-for-the-inventory-sqlite-for-history).)
- History lives in a local SQLite file, so there is no shared view for a second technician on another PC.

These were acceptable for one administrator watching one site. A central service with a web UI would remove all three — again, the job of a real monitoring platform.

## 6. Editing the inventory while the app is running

When a device is muted or unmuted, the app rewrites `inventario.csv` from its in-memory list. If I edit the CSV by hand while the app is open, my changes are overwritten the next time I mute something. The workaround is simple — close the app before editing the inventory — but the app does not warn about it.

## 7. Conventions that are hard-coded

- **Category codes.** Right-click actions depend on the exact category names `PC`, `IMP` and `TEL`. Any other category is display-only.
- **Local administrator name.** Local mode builds the account as `<hostname>\administrador`, the built-in Administrator account's name on Spanish-language Windows. On an English system it would be `Administrator`.
- **Thresholds.** The 3-second interval, 800 ms timeout, 100 ms latency limit, 3-failure confirmation, 8-second sound cooldown and the "more than 8 outages in 60 seconds" Telegram rule are constants in the code, not settings.

Moving these into a small configuration file would be straightforward.

## 8. Telegram alerts only cover storms

The bot reports bursts of outages, by design (see [DESIGN.md](DESIGN.md#decision-7--alerts-that-respect-the-person-receiving-them)). It does not report a single critical device going down (for example the main switch or the NAS), and it does not send a "recovered" message when the storm ends. A per-device "critical" flag in the inventory would cover the first case.

## 9. Local mode: a short exposure window remains

Local-mode PowerShell actions receive the password through an environment variable that exists only in the child process and is deleted as soon as the script reads it. During those first milliseconds, another process with enough privileges over that PowerShell process could read its environment block. It is a much smaller risk than the original command-line exposure, and acceptable for an internal support tool, but it is not zero. Details are in [`conexion_segura.py`](../conexion_segura.py).

## 10. Unsigned executable

The PyInstaller build is not code-signed, so it has no reputation with Windows Defender / SmartScreen and may be blocked on first run. In a managed environment the right answer is an exception through the security team or signing the executable with an organizational certificate.
