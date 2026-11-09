# Outlands Over The Top WWI server manager

Source-only kit for managing a privately hosted **Over The Top: WWI** dedicated server. The Python manager handles start/stop, health checks, campaign checkpoints, restorable backup selection, external recovery copying, update rollback, and restore drills. The shell wrapper launches the Windows server through Proton with a virtual X display.

This repository contains no game server binary, Steam content, live configuration, player data, password, logs, campaign saves, or backup archive. It is not a tested deployment for a new host.

## Files

- `ott-server-manager.py`: lifecycle, backup, recovery, and update controller.
- `start-over-the-top-pve.sh`: Proton and Xvfb launcher invoked by the controller.
- `run-over-the-top-pve-detached.sh`, `stop-over-the-top-pve.sh`: simple operator wrappers.
- `test_ott_server_manager.py`: offline tests using temporary fictional data.

## Configure before use

Use a dedicated Linux account with Python 3.12+, tmux, Proton, Xvfb, and the game’s separately installed dedicated server. Set the paths for your host:

| Variable | Default purpose |
| --- | --- |
| `OTT_STEAM_ROOT` | `$HOME/.steam/steam` |
| `OTT_SERVER_DIR` | Dedicated server folder under the Steam root |
| `OTT_PROTON` | Proton executable under the Steam root |
| `OTT_STEAM_RUNTIME` | Steam Linux Runtime entrypoint |
| `OTT_XVFB_RUN` | `xvfb-run` found on `PATH` |
| `OTT_STATE_ROOT` | `$HOME/.local/state/outlands-ott` for private state and backups |
| `OTT_RECOVERY_ROOT` | Separate-device private recovery root; the default is deliberately unconfigured |
| `OTT_STEAMCMD` | `/usr/games/steamcmd` |

The manager reads `ServerInfo.ini` and other installed files locally. Keep them outside Git. Its private state can contain campaign history, paths, player information, and credential material. Configure a separate recovery device and verify permissions before relying on it. The manager refuses an invalid recovery root.

Run only the offline tests until you have reviewed the paths and server state:

```sh
python3 -B -m unittest -q test_ott_server_manager.py
```

Then inspect `python3 ott-server-manager.py --help`. Commands such as `update`, `start`, `restore-drill`, and password rotation change local state and need a fresh host-specific review. Backups prove only what the manager captured; game process or port health does not prove exact native campaign continuity. Confirm a completed battle/map transition and a fresh complete archive before relying on persistence.

No license is assigned to this source snapshot. Review upstream game and Steam/Proton terms separately; this repository does not redistribute their files.
