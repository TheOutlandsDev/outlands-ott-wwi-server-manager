#!/usr/bin/env python3
"""Health-aware manager, updater, and campaign backup client for OTT: WWI."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import string
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STEAM_ROOT = Path(os.environ.get("OTT_STEAM_ROOT", str(Path.home() / ".steam/steam")))
SERVER_DIR = Path(os.environ.get(
    "OTT_SERVER_DIR", str(STEAM_ROOT / "steamapps/common/Over The Top WWI - Dedicated Server")
))
SERVER_INFO_FILE = SERVER_DIR / "ServerInfo.ini"
LOG_DIR = SERVER_DIR / "Logs"
MANIFEST = STEAM_ROOT / "steamapps/appmanifest_3052620.acf"
COMPAT_DATA = STEAM_ROOT / "steamapps/compatdata/3052620"
WINE_SERVER = STEAM_ROOT / "steamapps/common/Proton - Experimental/files/bin/wineserver"
STEAMCMD = Path(os.environ.get("OTT_STEAMCMD", "/usr/games/steamcmd"))
OUTPUT_DIR = Path(os.environ.get(
    "OTT_STATE_ROOT", str(Path.home() / ".local/state/outlands-ott")
))
LAUNCHER = SCRIPT_DIR / "start-over-the-top-pve.sh"
BACKUP_DIR = OUTPUT_DIR / "campaign-backups"
UPDATE_BACKUP_DIR = OUTPUT_DIR / "update-backups"
CONTINUITY_FILE = OUTPUT_DIR / "campaign-continuity.json"
LAST_RESTORABLE_FILE = BACKUP_DIR / ".last-restorable.json"
RECOVERY_STATE_FILE = OUTPUT_DIR / "recovery-state.json"
RESTORE_DRILL_FILE = OUTPUT_DIR / "restore-drill.json"
ALERT_STATE_FILE = OUTPUT_DIR / "campaign-alert-state.json"
BINARY_ROLLBACK_STATE_FILE = OUTPUT_DIR / "binary-rollback-state.json"
PRIVATE_DIR = OUTPUT_DIR / "private"
PLAYER_PASSWORD_FILE = PRIVATE_DIR / "playtest-player-password.txt"
EXTERNAL_RECOVERY_ROOT = Path(os.environ.get(
    "OTT_RECOVERY_ROOT", str(OUTPUT_DIR / "external-recovery-unconfigured")
))
LOCK_FILE = OUTPUT_DIR / ".ott-manager.lock"

SERVER_SESSION = "over-the-top-pve"
BACKUP_SESSION = "over-the-top-backup"
PROCESS_PATTERN = "Over_The_Top_WWI.exe -batchmode -nographics -server"
GAME_PORT = 7386
QUERY_PORT = 7387
STARTUP_TIMEOUT = 180
BOOT_GRACE_SECONDS = 8 * 60
WATCH_INTERVAL = 300
MAX_CONTINUITY_HISTORY = 256
MAX_SEEN_CONTINUITY_EVENTS = 4096
STALE_BACKUP_SECONDS = 24 * 60 * 60
STALE_WARNING_INTERVAL = 24 * 60 * 60
MAX_BINARY_ROLLBACKS = 2
ROLLBACK_MANIFEST_NAME = "__steam_appmanifest_3052620.acf"
EXPECTED_RULES = {
    "game_mode": "2",
    "campaign": "1",
    "attacker_bots": "100",
    "defender_bots": "100",
    "hardcore": "1",
    "first_person": "1",
    "map_minutes": "30",
    "attacker_respawn_seconds": "30",
    "defender_respawn_seconds": "30",
    "wave_spawning": "1",
    "reset_if_empty": "0",
}

SAVE_INPUTS = (
    SERVER_DIR / "CampSaves",
    SERVER_DIR / "CampaignJson.json",
    SERVER_DIR / "MpTiles",
    SERVER_DIR / "MpCampaign.txt",
)
CONFIG_INPUTS = (
    SERVER_DIR / "ServerConfiguration.ini",
    SERVER_DIR / "ServerInfo.ini",
    SERVER_DIR / "MpCampaign.txt",
    SERVER_DIR / "MapRotation.txt",
    SERVER_DIR / "HvBController.json",
    SERVER_DIR / "Banlist.txt",
    SERVER_DIR / "BannedWords.txt",
    SERVER_DIR / "TempBanlist.txt",
    SERVER_DIR / "Whitelist.txt",
    SERVER_DIR / "CustomMaps",
)


def run(command, *, check=False, timeout=None, env=None):
    return subprocess.run(
        command, check=check, text=True, capture_output=True, timeout=timeout, env=env
    )


def current_boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return "unknown"


def host_uptime_seconds():
    try:
        return float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def emit_run_marker(command):
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(
        f"Manager run: timestamp={timestamp} command={command} "
        f"pid={os.getpid()} boot_id={current_boot_id()}",
        flush=True,
    )


@contextmanager
def operation_lock():
    with LOCK_FILE.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another server operation is running; skipping this invocation.")
            yield False
            return
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        yield True


def tmux_exists(name):
    return run(["tmux", "has-session", "-t", name]).returncode == 0


def process_exists():
    return run(["pgrep", "-f", PROCESS_PATTERN]).returncode == 0


def port_bound(port, protocol):
    required_state = "0A" if protocol == "tcp" else None
    for suffix in (protocol, protocol + "6"):
        try:
            lines = Path("/proc/net", suffix).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            local_port = int(fields[1].rsplit(":", 1)[1], 16)
            if local_port == port and (required_state is None or fields[3] == required_state):
                return True
    return False


def ports():
    return {
        "tcp_game": port_bound(GAME_PORT, "tcp"),
        "udp_game": port_bound(GAME_PORT, "udp"),
        "udp_query": port_bound(QUERY_PORT, "udp"),
    }


def healthy():
    return all(ports().values())


def read_json(path, default=None):
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def write_json_atomic(path, payload, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_bytes_atomic(path, payload, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def format_age(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def backup_freshness(now=None):
    archive = latest_restorable_archive()
    if archive is None:
        return {
            "status": "missing",
            "archive": None,
            "age_seconds": None,
            "warning": "No restorable campaign backup exists.",
        }
    current_time = time.time() if now is None else now
    try:
        age_seconds = max(0, current_time - archive.stat().st_mtime)
    except OSError:
        age_seconds = None
    status = "stale" if age_seconds is None or age_seconds > STALE_BACKUP_SECONDS else "fresh"
    warning = None
    if status == "stale":
        warning = (
            f"Newest restorable campaign backup is {format_age(age_seconds)} old; "
            "confirm a completed map produces a new restorable archive."
        )
    return {
        "status": status,
        "archive": str(archive),
        "age_seconds": age_seconds,
        "warning": warning,
    }


def external_recovery_status(prepare=False):
    try:
        root = EXTERNAL_RECOVERY_ROOT
        if prepare:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(root, 0o700)
        if not root.is_dir():
            raise OSError("target directory does not exist")
        local_device = OUTPUT_DIR.stat().st_dev
        external_device = root.stat().st_dev
        if local_device == external_device:
            return {
                "available": False,
                "separate_device": False,
                "root": str(root),
                "warning": "Recovery target is on the same filesystem as the live manager.",
            }
        if not os.access(root, os.W_OK):
            raise OSError("target directory is not writable")
        if prepare:
            probe = root / ".write-test"
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            probe.unlink()
        return {
            "available": True,
            "separate_device": True,
            "root": str(root),
            "warning": None,
        }
    except OSError as error:
        return {
            "available": False,
            "separate_device": False,
            "root": str(EXTERNAL_RECOVERY_ROOT),
            "warning": f"External recovery target unavailable: {error}",
        }


def atomic_copy_verified(source, destination):
    source = Path(source)
    destination = Path(destination)
    expected = file_digest(source)
    if destination.is_file():
        try:
            if destination.stat().st_size == source.stat().st_size:
                if file_digest(destination) == expected:
                    os.chmod(destination, 0o600)
                    return expected
        except OSError:
            pass
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(temporary, 0o600)
        if file_digest(temporary) != expected:
            raise RuntimeError(f"Recovery copy verification failed: {source}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return expected


def mirror_recovery_material(campaign_archive=None, config_archive=None):
    status = external_recovery_status(prepare=True)
    if not status["available"]:
        print(f"WARNING: {status['warning']}", file=sys.stderr)
        return None
    root = EXTERNAL_RECOVERY_ROOT
    existing = read_json(RECOVERY_STATE_FILE, default={})
    copied = {}
    if isinstance(existing, dict) and existing.get("root") == str(root):
        copied.update(existing.get("copies", {}))
    campaign_archive = campaign_archive or latest_restorable_archive()
    if campaign_archive is not None:
        destination = root / "campaign-backups" / Path(campaign_archive).name
        copied["campaign"] = {
            "path": str(destination),
            "sha256": atomic_copy_verified(campaign_archive, destination),
        }
    if config_archive is not None:
        destination = root / "configuration-backups" / Path(config_archive).name
        copied["configuration"] = {
            "path": str(destination),
            "sha256": atomic_copy_verified(config_archive, destination),
        }
    recovery_tools = (
        CONTINUITY_FILE,
        LAST_RESTORABLE_FILE,
        RESTORE_DRILL_FILE,
        BINARY_ROLLBACK_STATE_FILE,
        Path(__file__).resolve(),
        SCRIPT_DIR / "test_ott_server_manager.py",
        SCRIPT_DIR / "README.md",
        LAUNCHER,
    )
    for source in recovery_tools:
        if source.is_file():
            destination = root / "recovery-tools" / source.name
            copied[source.name] = {
                "path": str(destination),
                "sha256": atomic_copy_verified(source, destination),
            }
    payload = {
        "version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": str(root),
        "separate_device": True,
        "copies": copied,
    }
    write_json_atomic(RECOVERY_STATE_FILE, payload)
    write_json_atomic(root / "recovery-state.json", payload)
    return payload


def emit_backup_freshness_warning(now=None):
    freshness = backup_freshness(now=now)
    state = read_json(ALERT_STATE_FILE, default={})
    previous_status = state.get("status") if isinstance(state, dict) else None
    current_time = time.time() if now is None else now
    last_warning = float(state.get("last_warning_epoch", 0)) if isinstance(state, dict) else 0
    should_warn = freshness["status"] != "fresh" and (
        previous_status != freshness["status"]
        or current_time - last_warning >= STALE_WARNING_INTERVAL
    )
    if should_warn:
        print(f"WARNING: {freshness['warning']}", file=sys.stderr, flush=True)
        last_warning = current_time
    elif freshness["status"] == "fresh" and previous_status not in {None, "fresh"}:
        print("Campaign backup freshness recovered.", flush=True)
    payload = {
        "status": freshness["status"],
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_warning_epoch": last_warning,
        "archive": freshness["archive"],
        "age_seconds": freshness["age_seconds"],
        "warning": freshness["warning"],
    }
    write_json_atomic(ALERT_STATE_FILE, payload)
    return freshness


def new_continuity_checkpoint():
    return {
        "version": 1,
        "updated_at": None,
        "battles_completed": 0,
        "campaigns_completed": 0,
        "restart_rollovers": 0,
        "native_transitions_observed": 0,
        "last_completed_map": None,
        "current_map_name": None,
        "current_tile_id": None,
        "current_map_run_id": None,
        "current_map_path": None,
        "last_native_position": None,
        "last_native_month": None,
        "last_native_season": None,
        "campaign_reset_pending": False,
        "last_event_at": None,
        "log_cursors": {},
        "seen_event_ids": [],
        "history": [],
    }


def add_continuity_history(checkpoint, event):
    checkpoint.setdefault("history", []).append(event)
    checkpoint["history"] = checkpoint["history"][-MAX_CONTINUITY_HISTORY:]


def apply_campaign_log_line(checkpoint, line, event_at=None):
    """Apply one server log line to the durable, approximate campaign ledger."""
    line = line.replace("\x00", "").strip()
    if not line:
        return False
    changed = False

    match = re.search(r"\bRound Ended\.", line, re.IGNORECASE)
    if match:
        checkpoint["battles_completed"] = int(checkpoint.get("battles_completed", 0)) + 1
        add_continuity_history(checkpoint, {
            "type": "battle_ended",
            "at": event_at,
            "battle": checkpoint["battles_completed"],
        })
        changed = True

    match = re.search(r"\bMap:\s+(.+?)\s*$", line)
    if match:
        checkpoint["last_completed_map"] = match.group(1).strip()
        changed = True

    match = re.search(r"The map has ended\.\s+Starting\s+(.+?)\.?\s*$", line, re.IGNORECASE)
    if match:
        checkpoint["current_map_name"] = match.group(1).strip().rstrip(".")
        changed = True

    match = re.search(r"\btile id\s+(\d+)", line, re.IGNORECASE)
    if match:
        checkpoint["current_tile_id"] = int(match.group(1))
        changed = True

    match = re.search(r"\bran id\s+(\d+)", line, re.IGNORECASE)
    if match:
        checkpoint["current_map_run_id"] = int(match.group(1))
        changed = True

    match = re.search(r"\bpath\s+(.+?)\s*$", line, re.IGNORECASE)
    if match and ("MpTiles" in match.group(1) or "CustomMaps" in match.group(1)):
        checkpoint["current_map_path"] = match.group(1).strip()
        changed = True

    match = re.search(r"Campaign Over:\s*(.+?)\s*$", line, re.IGNORECASE)
    if match:
        checkpoint["campaigns_completed"] = int(checkpoint.get("campaigns_completed", 0)) + 1
        checkpoint["campaign_reset_pending"] = True
        add_continuity_history(checkpoint, {
            "type": "campaign_completed",
            "at": event_at,
            "result": match.group(1).strip(),
            "campaign": checkpoint["campaigns_completed"],
        })
        changed = True

    match = re.search(
        r"season spot\s+(\d+)\s+month\s+(\d+)\s+TheSeason\s+(\S+)",
        line,
        re.IGNORECASE,
    )
    if match:
        new_position = int(match.group(1))
        previous_position = checkpoint.get("last_native_position")
        if previous_position is not None and new_position < int(previous_position):
            if checkpoint.get("campaign_reset_pending"):
                checkpoint["campaign_reset_pending"] = False
            else:
                checkpoint["restart_rollovers"] = int(
                    checkpoint.get("restart_rollovers", 0)
                ) + 1
                add_continuity_history(checkpoint, {
                    "type": "restart_rollover",
                    "at": event_at,
                    "from_position": int(previous_position),
                    "to_position": new_position,
                    "rollover": checkpoint["restart_rollovers"],
                })
        checkpoint["last_native_position"] = new_position
        checkpoint["last_native_month"] = int(match.group(2))
        checkpoint["last_native_season"] = match.group(3)
        checkpoint["native_transitions_observed"] = int(
            checkpoint.get("native_transitions_observed", 0)
        ) + 1
        add_continuity_history(checkpoint, {
            "type": "native_transition",
            "at": event_at,
            "position": new_position,
            "month": checkpoint["last_native_month"],
            "season": checkpoint["last_native_season"],
            "tile": checkpoint.get("current_tile_id"),
        })
        changed = True

    if changed:
        checkpoint["last_event_at"] = event_at
    return changed


def checkpoint_campaign_progress():
    checkpoint = read_json(CONTINUITY_FILE, default=None)
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("version") != 1
        or "seen_event_ids" not in checkpoint
    ):
        checkpoint = new_continuity_checkpoint()
    checkpoint.setdefault("log_cursors", {})
    checkpoint.setdefault("seen_event_ids", [])
    seen_event_ids = set(checkpoint["seen_event_ids"])
    log_paths = sorted(LOG_DIR.glob("20??-??-??.txt")) if LOG_DIR.exists() else []
    changed = not CONTINUITY_FILE.exists()
    for log_path in log_paths:
        try:
            stat = log_path.stat()
        except OSError:
            continue
        key = str(log_path)
        cursor = checkpoint["log_cursors"].get(key, {})
        offset = int(cursor.get("offset", 0))
        if cursor.get("inode") != stat.st_ino or stat.st_size < offset:
            offset = 0
        try:
            with log_path.open("rb") as source:
                source.seek(offset)
                content = source.read()
                new_offset = source.tell()
        except OSError:
            continue
        date_prefix = log_path.stem
        for raw_line in content.decode("utf-8", errors="replace").splitlines():
            time_match = re.match(r"(\d{2}:\d{2}:\d{2}):", raw_line)
            event_at = f"{date_prefix}T{time_match.group(1)}" if time_match else date_prefix
            event_id = hashlib.sha256(
                f"{date_prefix}\0{raw_line}".encode("utf-8", errors="replace")
            ).hexdigest()[:20]
            if event_id in seen_event_ids:
                continue
            if apply_campaign_log_line(checkpoint, raw_line, event_at):
                checkpoint["seen_event_ids"].append(event_id)
                seen_event_ids.add(event_id)
                changed = True
        new_cursor = {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "offset": new_offset,
        }
        if cursor != new_cursor:
            checkpoint["log_cursors"][key] = new_cursor
            changed = True
    if changed:
        checkpoint["seen_event_ids"] = checkpoint["seen_event_ids"][
            -MAX_SEEN_CONTINUITY_EVENTS:
        ]
        checkpoint["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json_atomic(CONTINUITY_FILE, checkpoint)
    return checkpoint


def save_files():
    result = []
    for source in SAVE_INPUTS:
        if source.is_file():
            result.append(source)
        elif source.is_dir():
            result.extend(path for path in source.rglob("*") if path.is_file())
    return sorted(result)


def config_files():
    result = []
    for source in CONFIG_INPUTS:
        if source.is_file():
            result.append(source)
        elif source.is_dir():
            result.extend(
                path for path in source.rglob("*")
                if path.is_file() and ".codex-" not in path.name
            )
    return sorted(set(result))


def campaign_state_status():
    campaign = SERVER_DIR / "CampaignJson.json"
    tiles = sorted((SERVER_DIR / "MpTiles").glob("*.json"))
    if not campaign.exists() and not tiles:
        return "missing"
    if not campaign.is_file() or not tiles:
        return "invalid"
    for path in (campaign, *tiles):
        try:
            with path.open(encoding="utf-8") as source:
                json.load(source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return "invalid"
    return "valid"


def archive_campaign_summary(archive_path):
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            names = {
                member.name for member in archive.getmembers()
                if member.isfile()
            }
    except (OSError, tarfile.TarError):
        return {
            "restorable": False,
            "tile_count": 0,
            "save_count": 0,
            "file_count": 0,
        }
    tiles = sorted(
        name for name in names
        if name.startswith("MpTiles/") and name.endswith(".json")
    )
    saves = sorted(name for name in names if name.startswith("CampSaves/"))
    return {
        "restorable": "CampaignJson.json" in names and bool(tiles),
        "tile_count": len(tiles),
        "save_count": len(saves),
        "file_count": len(names),
    }


def latest_restorable_archive():
    if not BACKUP_DIR.exists():
        return None
    for archive_path in sorted(BACKUP_DIR.glob("campaign-*.tar.gz"), reverse=True):
        if archive_campaign_summary(archive_path)["restorable"]:
            return archive_path
    return None


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def mark_last_restorable(archive_path, campaign_digest=None):
    summary = archive_campaign_summary(archive_path)
    if not summary["restorable"]:
        raise RuntimeError(f"Campaign archive is not restorable: {archive_path}")
    payload = {
        "archive": str(archive_path),
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "archive_sha256": file_digest(archive_path),
        "campaign_digest": campaign_digest,
        **summary,
    }
    write_json_atomic(LAST_RESTORABLE_FILE, payload)
    return payload


def digest(files):
    result = hashlib.sha256()
    for path in files:
        result.update(str(path.relative_to(SERVER_DIR)).encode())
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                result.update(block)
    return result.hexdigest()


def backup_campaign(force=False):
    checkpoint_campaign_progress()
    state_status = campaign_state_status()
    if state_status != "valid":
        latest = latest_restorable_archive()
        if latest is not None:
            marker = read_json(LAST_RESTORABLE_FILE, default={})
            if (
                marker.get("archive") != str(latest)
                or not marker.get("archive_sha256")
            ):
                mark_last_restorable(latest)
            print(
                f"Campaign map state is {state_status}; skipped an incomplete snapshot and "
                f"preserved the last restorable backup: {latest}"
            )
            try:
                mirror_recovery_material(campaign_archive=latest)
            except Exception as error:
                print(f"WARNING: External campaign mirror failed: {error}", file=sys.stderr)
        else:
            print(
                f"Campaign map state is {state_status}; skipped an incomplete snapshot. "
                "No restorable campaign backup exists yet."
            )
        return None
    files = save_files()
    if not files:
        print("No campaign files exist yet.")
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(BACKUP_DIR, 0o700)
    value = digest(files)
    state = BACKUP_DIR / ".backup-state.json"
    try:
        previous = json.loads(state.read_text()).get("sha256")
    except (OSError, json.JSONDecodeError):
        previous = None
    if not force and previous == value:
        print("Campaign files unchanged; no new backup needed.")
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = BACKUP_DIR / f"campaign-{stamp}-{value[:10]}.tar.gz"
    descriptor, temporary_name = tempfile.mkstemp(dir=BACKUP_DIR, suffix=".tar.gz")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for path in files:
                archive.add(path, arcname=path.relative_to(SERVER_DIR), recursive=False)
            if CONTINUITY_FILE.is_file():
                archive.add(
                    CONTINUITY_FILE,
                    arcname="continuity/campaign-continuity.json",
                    recursive=False,
                )
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    mark_last_restorable(destination, campaign_digest=value)
    write_json_atomic(state, {"sha256": value})
    for old in sorted(BACKUP_DIR.glob("campaign-*.tar.gz"), reverse=True)[96:]:
        old.unlink()
    print(f"Campaign backup created: {destination}")
    try:
        mirror_recovery_material(campaign_archive=destination)
    except Exception as error:
        print(f"WARNING: External campaign mirror failed: {error}", file=sys.stderr)
    return destination


def restore_latest_campaign_if_needed(force_restore=False):
    state = campaign_state_status()
    if state == "valid" and not force_restore:
        return None
    archive_path = latest_restorable_archive()
    root = SERVER_DIR.resolve()
    if archive_path is not None:
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = archive.getmembers()
                names = {member.name for member in members if member.isfile()}
                if "CampaignJson.json" not in names or not any(
                    name.startswith("MpTiles/") and name.endswith(".json") for name in names
                ):
                    raise RuntimeError("Archive lost its required campaign or tile files")
                selected = []
                for member in members:
                    parts = Path(member.name).parts
                    if not parts or parts[0] not in {"CampSaves", "CampaignJson.json", "MpTiles"}:
                        continue
                    destination = (SERVER_DIR / member.name).resolve()
                    if root not in destination.parents:
                        raise RuntimeError(f"Unsafe archive member: {member.name}")
                    selected.append(member)
                archive.extractall(SERVER_DIR, members=selected, filter="data")
            if campaign_state_status() == "valid":
                mark_last_restorable(archive_path)
                print(f"Campaign state restored from: {archive_path}")
                return archive_path
        except (OSError, tarfile.TarError, RuntimeError) as error:
            raise RuntimeError(
                f"Latest restorable campaign archive could not be restored: {archive_path}: {error}"
            ) from error
    if state == "missing":
        print("No complete saved campaign exists yet; starting without a restore.")
        return None
    raise RuntimeError("Campaign state is invalid and no complete backup can restore it")


def config_snapshot():
    UPDATE_BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(UPDATE_BACKUP_DIR, 0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = UPDATE_BACKUP_DIR / f"pre-update-{stamp}.tar.gz"
    with tarfile.open(destination, "w:gz") as archive:
        for path in config_files():
            archive.add(path, arcname=path.relative_to(SERVER_DIR), recursive=False)
    os.chmod(destination, 0o600)
    for old in sorted(UPDATE_BACKUP_DIR.glob("pre-update-*.tar.gz"), reverse=True)[8:]:
        old.unlink()
    print(f"Configuration snapshot created: {destination}")
    try:
        mirror_recovery_material(config_archive=destination)
    except Exception as error:
        print(f"WARNING: External configuration mirror failed: {error}", file=sys.stderr)
    return destination


def extract_archive_safely(snapshot, destination, mode="r:gz", skip_names=()):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(snapshot, mode) as archive:
        selected = []
        for member in archive.getmembers():
            if member.name in skip_names:
                continue
            candidate = (destination / member.name).resolve()
            if candidate != root and root not in candidate.parents:
                raise RuntimeError(f"Unsafe archive member: {member.name}")
            selected.append(member)
        archive.extractall(destination, members=selected, filter="data")
    return selected


def restore_snapshot(snapshot):
    extract_archive_safely(snapshot, SERVER_DIR)
    print("Private server configuration restored.")


def generate_player_password(length=18):
    if not 12 <= length <= 20:
        raise ValueError("Player passwords must contain between 12 and 20 characters")
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%*+-_"),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%*+-_"
    required.extend(secrets.choice(alphabet) for _ in range(length - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def rotate_player_password():
    current = SERVER_INFO_FILE.read_bytes()
    password = generate_player_password()
    replacement = b"ServerPassword = " + password.encode("ascii")
    updated, count = re.subn(
        rb"(?m)^ServerPassword[ \t]*=[^\r\n]*", replacement, current
    )
    if count != 1:
        raise RuntimeError(
            f"Expected one ServerPassword setting in {SERVER_INFO_FILE}; found {count}"
        )
    config_snapshot()
    original_mode = SERVER_INFO_FILE.stat().st_mode & 0o777
    write_bytes_atomic(SERVER_INFO_FILE, updated, mode=original_mode)
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PRIVATE_DIR, 0o700)
    write_bytes_atomic(
        PLAYER_PASSWORD_FILE,
        (password + "\n").encode("ascii"),
        mode=0o600,
    )
    print(
        "Player password rotated. The new password is stored only in the private "
        f"credential file: {PLAYER_PASSWORD_FILE}"
    )
    return 0


def binary_snapshot_summary(snapshot):
    try:
        with tarfile.open(snapshot, "r:") as archive:
            names = {member.name for member in archive.getmembers() if member.isfile()}
    except (OSError, tarfile.TarError):
        return {"restorable": False, "file_count": 0}
    required = {
        "Over_The_Top_WWI.exe",
        "GameAssembly.dll",
        ROLLBACK_MANIFEST_NAME,
    }
    has_data = any(name.startswith("Over_The_Top_WWI_Data/") for name in names)
    return {
        "restorable": required.issubset(names) and has_data,
        "file_count": len(names),
    }


def create_binary_rollback_snapshot(build=None):
    status = external_recovery_status(prepare=True)
    if not status["available"]:
        raise RuntimeError(status["warning"])
    build = build or installed_build() or "unknown"
    destination_dir = EXTERNAL_RECOVERY_ROOT / "binary-rollbacks"
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination_dir, 0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = destination_dir / f"pre-update-build-{build}-{stamp}.tar"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination_dir, prefix=".pre-update-build-", suffix=".tar.tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w") as archive:
            for path in sorted(SERVER_DIR.iterdir()):
                archive.add(path, arcname=path.name, recursive=True)
            if MANIFEST.is_file():
                archive.add(MANIFEST, arcname=ROLLBACK_MANIFEST_NAME, recursive=False)
        os.chmod(temporary, 0o600)
        summary = binary_snapshot_summary(temporary)
        if not summary["restorable"]:
            raise RuntimeError("Full binary rollback snapshot failed validation")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    payload = {
        "version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "build": str(build),
        "archive": str(destination),
        "archive_sha256": file_digest(destination),
        "critical_sha256": {
            "Over_The_Top_WWI.exe": file_digest(SERVER_DIR / "Over_The_Top_WWI.exe"),
            "GameAssembly.dll": file_digest(SERVER_DIR / "GameAssembly.dll"),
            ROLLBACK_MANIFEST_NAME: file_digest(MANIFEST),
        },
        **binary_snapshot_summary(destination),
    }
    write_json_atomic(BINARY_ROLLBACK_STATE_FILE, payload)
    write_json_atomic(destination_dir / ".latest.json", payload)
    for old in sorted(destination_dir.glob("pre-update-build-*.tar"), reverse=True)[
        MAX_BINARY_ROLLBACKS:
    ]:
        old.unlink()
    print(f"Full binary rollback snapshot created: {destination}")
    return destination


def restore_binary_rollback_snapshot(snapshot):
    snapshot = Path(snapshot)
    summary = binary_snapshot_summary(snapshot)
    if not summary["restorable"]:
        raise RuntimeError(f"Binary rollback snapshot is not restorable: {snapshot}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    failed_directory = SERVER_DIR.with_name(f"{SERVER_DIR.name}.failed-update-{stamp}")
    if failed_directory.exists():
        raise RuntimeError(f"Rollback quarantine already exists: {failed_directory}")
    SERVER_DIR.rename(failed_directory)
    SERVER_DIR.mkdir(parents=True)
    try:
        manifest_content = None
        with tarfile.open(snapshot, "r:") as archive:
            selected = []
            root = SERVER_DIR.resolve()
            for member in archive.getmembers():
                if member.name == ROLLBACK_MANIFEST_NAME:
                    extracted = archive.extractfile(member)
                    manifest_content = extracted.read() if extracted is not None else None
                    continue
                candidate = (SERVER_DIR / member.name).resolve()
                if candidate != root and root not in candidate.parents:
                    raise RuntimeError(f"Unsafe binary snapshot member: {member.name}")
                selected.append(member)
            archive.extractall(SERVER_DIR, members=selected, filter="data")
        if manifest_content is None:
            raise RuntimeError("Binary rollback snapshot has no Steam manifest")
        write_bytes_atomic(MANIFEST, manifest_content, mode=0o600)
    except Exception:
        shutil.rmtree(SERVER_DIR, ignore_errors=True)
        failed_directory.rename(SERVER_DIR)
        raise
    print(f"Previous server build restored; failed update quarantined at: {failed_directory}")
    return failed_directory


def recover_failed_update(config_archive, binary_archive, reason):
    print(f"Update recovery started: {reason}", file=sys.stderr)
    stop_game()
    try:
        restore_binary_rollback_snapshot(binary_archive)
        restore_snapshot(config_archive)
        restore_latest_campaign_if_needed(force_restore=True)
        result = start()
    except Exception as error:
        print(f"CRITICAL: Update rollback failed: {error}", file=sys.stderr)
        return 2
    if result == 0:
        print("Previous build recovered and is healthy; the update remains unapplied.")
        return 1
    print("CRITICAL: Previous build was restored but did not become healthy.", file=sys.stderr)
    return 2


def ensure_backup_watcher():
    if not tmux_exists(BACKUP_SESSION):
        run([
            "tmux", "new-session", "-d", "-s", BACKUP_SESSION,
            sys.executable, str(Path(__file__).resolve()), "watch",
        ], check=True)
        print("Campaign backup watcher started.")


def stop_game():
    if process_exists():
        run(["pkill", "-TERM", "-f", PROCESS_PATTERN])
        print("Stopped the game and Proton wrapper.")
    if tmux_exists(SERVER_SESSION):
        run(["tmux", "kill-session", "-t", SERVER_SESSION])
    for _ in range(15):
        if not process_exists():
            break
        time.sleep(1)
    if WINE_SERVER.is_file():
        environment = os.environ.copy()
        environment["WINEPREFIX"] = str(COMPAT_DATA / "pfx")
        run([str(WINE_SERVER), "-k"], env=environment, timeout=15)
    if process_exists():
        run(["pkill", "-KILL", "-f", PROCESS_PATTERN])
        time.sleep(2)


def wait_for_start():
    deadline = time.monotonic() + STARTUP_TIMEOUT
    next_message = time.monotonic() + 15
    while time.monotonic() < deadline:
        if healthy():
            print("Server healthy: TCP/UDP 7386 and UDP 7387 are listening.")
            return True
        if not tmux_exists(SERVER_SESSION) and not process_exists():
            print("Server exited before opening its ports.", file=sys.stderr)
            return False
        if time.monotonic() >= next_message:
            print("Waiting for game ports to open...", flush=True)
            next_message += 15
        time.sleep(3)
    print("Server startup timed out; cleaning up the failed launch.", file=sys.stderr)
    return False


def start_game():
    if healthy():
        print("Server is already healthy.")
        return 0
    if process_exists() or tmux_exists(SERVER_SESSION):
        print("Removing stalled server processes.")
        stop_game()
    run(["tmux", "new-session", "-d", "-s", SERVER_SESSION, str(LAUNCHER)], check=True)
    print("Server launch started.")
    if wait_for_start():
        return 0
    stop_game()
    return 1


def start():
    checkpoint_campaign_progress()
    restore_latest_campaign_if_needed()
    ensure_backup_watcher()
    return start_game()


def stop():
    backup_campaign()
    stop_game()
    backup_campaign()
    if tmux_exists(BACKUP_SESSION):
        run(["tmux", "kill-session", "-t", BACKUP_SESSION])
    print("Server and backup watcher stopped.")
    return 0


def restart():
    checkpoint_campaign_progress()
    backup_campaign()
    stop_game()
    backup_campaign()
    restore_latest_campaign_if_needed(force_restore=True)
    ensure_backup_watcher()
    return start_game()


def ensure():
    checkpoint_campaign_progress()
    if healthy():
        ensure_backup_watcher()
        print("Health check passed.")
        return 0
    uptime = host_uptime_seconds()
    if uptime is not None and uptime < BOOT_GRACE_SECONDS:
        ensure_backup_watcher()
        remaining = max(0, int(BOOT_GRACE_SECONDS - uptime))
        print(
            f"Host boot grace active; deferring game recovery for {remaining}s."
        )
        return 0
    print("Health check failed; recovering automatically.")
    restore_latest_campaign_if_needed()
    ensure_backup_watcher()
    return start_game()


def installed_build():
    try:
        content = MANIFEST.read_text(errors="replace")
    except OSError:
        return None
    match = re.search(r'"buildid"\s+"(\d+)"', content)
    return match.group(1) if match else None


def public_build():
    result = run([
        str(STEAMCMD), "+login", "anonymous", "+app_info_update", "1",
        "+app_info_print", "3052620", "+quit",
    ], timeout=180)
    content = result.stdout + result.stderr
    match = re.search(r'"public"\s*\{.*?"buildid"\s+"(\d+)"', content, re.DOTALL)
    if result.returncode != 0 or not match:
        raise RuntimeError("SteamCMD could not read the public build ID")
    return match.group(1)


def update():
    try:
        public = public_build()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Update check failed safely: {error}", file=sys.stderr)
        return ensure()
    current = installed_build()
    print(f"Installed build: {current or 'unknown'}; public build: {public}")
    if current == public:
        print("Dedicated server is already up to date.")
        return ensure()
    backup_campaign(force=True)
    snapshot = config_snapshot()
    stop_game()
    try:
        binary_snapshot = create_binary_rollback_snapshot(current)
    except (OSError, RuntimeError, tarfile.TarError) as error:
        print(f"Update aborted safely before changing files: {error}", file=sys.stderr)
        restore_snapshot(snapshot)
        start()
        return 1
    try:
        result = run([
            str(STEAMCMD), "+force_install_dir", str(SERVER_DIR), "+login", "anonymous",
            "+app_update", "3052620", "validate", "+quit",
        ], timeout=1800)
        content = result.stdout + result.stderr
        if result.returncode != 0 or "fully installed" not in content:
            raise RuntimeError("SteamCMD did not report a successful update")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return recover_failed_update(snapshot, binary_snapshot, error)
    restore_snapshot(snapshot)
    print(f"Update completed; installed build: {installed_build() or 'unknown'}")
    try:
        restore_latest_campaign_if_needed(force_restore=True)
        result = start()
    except Exception as error:
        return recover_failed_update(snapshot, binary_snapshot, error)
    if result != 0:
        return recover_failed_update(
            snapshot,
            binary_snapshot,
            "updated build did not pass the startup health check",
        )
    print("Updated build passed the TCP/UDP startup health check.")
    return 0


def read_assignments(path):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "//", "#")) or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def server_rules_summary():
    values = read_assignments(SERVER_DIR / "ServerConfiguration.ini")
    return {
        "game_mode": values.get("CurrentGameMode", "unknown"),
        "campaign": values.get("MpCampaign", "unknown"),
        "attacker_bots": values.get("AttackerBots", "unknown"),
        "defender_bots": values.get("DefenderBots", "unknown"),
        "hardcore": values.get("HardcoreMode", "unknown"),
        "first_person": values.get("ForceFirstPerson", "unknown"),
        "map_minutes": values.get("MaxMapTime", "unknown"),
        "attacker_respawn_seconds": values.get("AttackerRespawnTime", "unknown"),
        "defender_respawn_seconds": values.get("DefenderRespawnTime", "unknown"),
        "wave_spawning": values.get("WaveSpawning", "unknown"),
        "reset_if_empty": values.get("ResetIfNoPlayers", "unknown"),
    }


def rules_profile_issues(rules=None):
    rules = rules or server_rules_summary()
    return {
        key: {"expected": expected, "actual": rules.get(key, "unknown")}
        for key, expected in EXPECTED_RULES.items()
        if rules.get(key) != expected
    }


def latest_config_snapshot():
    if not UPDATE_BACKUP_DIR.exists():
        return None
    return next(iter(sorted(UPDATE_BACKUP_DIR.glob("pre-update-*.tar.gz"), reverse=True)), None)


def latest_binary_snapshot():
    state = read_json(BINARY_ROLLBACK_STATE_FILE, default={})
    if isinstance(state, dict) and state.get("archive"):
        candidate = Path(state["archive"])
        if candidate.is_file() and binary_snapshot_summary(candidate)["restorable"]:
            return candidate
    directory = EXTERNAL_RECOVERY_ROOT / "binary-rollbacks"
    if not directory.exists():
        return None
    for candidate in sorted(directory.glob("pre-update-build-*.tar"), reverse=True):
        if binary_snapshot_summary(candidate)["restorable"]:
            return candidate
    return None


def run_restore_drill():
    started = time.monotonic()
    payload = {
        "version": 1,
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "failed",
    }
    try:
        campaign_archive = latest_restorable_archive()
        if campaign_archive is None:
            raise RuntimeError("No restorable campaign archive exists")
        campaign_summary = archive_campaign_summary(campaign_archive)
        campaign_sha256 = file_digest(campaign_archive)
        config_archive = latest_config_snapshot()
        if config_archive is None:
            raise RuntimeError("No configuration archive exists")
        config_sha256 = file_digest(config_archive)
        binary_archive = latest_binary_snapshot()
        if binary_archive is None:
            raise RuntimeError("No full binary rollback archive exists")
        binary_state = read_json(BINARY_ROLLBACK_STATE_FILE, default={})
        binary_sha256 = file_digest(binary_archive)
        if (
            isinstance(binary_state, dict)
            and binary_state.get("archive") == str(binary_archive)
            and binary_state.get("archive_sha256") != binary_sha256
        ):
            raise RuntimeError("Binary rollback archive digest no longer matches its marker")
        critical_sha256 = binary_state.get("critical_sha256", {})
        if not all(
            critical_sha256.get(name)
            for name in (
                "Over_The_Top_WWI.exe",
                "GameAssembly.dll",
                ROLLBACK_MANIFEST_NAME,
            )
        ):
            raise RuntimeError("Binary rollback marker lacks critical file digests")
        mirror_recovery_material(
            campaign_archive=campaign_archive,
            config_archive=config_archive,
        )
        external_campaign = (
            EXTERNAL_RECOVERY_ROOT / "campaign-backups" / campaign_archive.name
        )
        external_config = (
            EXTERNAL_RECOVERY_ROOT / "configuration-backups" / config_archive.name
        )
        if file_digest(external_campaign) != campaign_sha256:
            raise RuntimeError("External campaign copy does not match its source")
        if file_digest(external_config) != config_sha256:
            raise RuntimeError("External configuration copy does not match its source")
        with tempfile.TemporaryDirectory(prefix="ott-restore-drill-") as directory:
            root = Path(directory)
            campaign_root = root / "campaign"
            extract_archive_safely(campaign_archive, campaign_root)
            campaign_json = campaign_root / "CampaignJson.json"
            tiles = sorted((campaign_root / "MpTiles").glob("*.json"))
            if not campaign_json.is_file() or not tiles:
                raise RuntimeError("Campaign drill extraction lacks required campaign files")
            for path in (campaign_json, *tiles):
                with path.open(encoding="utf-8") as source:
                    json.load(source)
            config_root = root / "configuration"
            extract_archive_safely(config_archive, config_root)
            required_config = (
                config_root / "ServerConfiguration.ini",
                config_root / "ServerInfo.ini",
                config_root / "MpCampaign.txt",
            )
            if not all(path.is_file() and path.stat().st_size > 0 for path in required_config):
                raise RuntimeError("Configuration drill extraction lacks required files")
            binary_root = root / "binary"
            extract_archive_safely(binary_archive, binary_root, mode="r:")
            for relative in ("Over_The_Top_WWI.exe", "GameAssembly.dll"):
                restored_file = binary_root / relative
                if (
                    not restored_file.is_file()
                    or file_digest(restored_file) != critical_sha256[relative]
                ):
                    raise RuntimeError(f"Binary drill mismatch for {relative}")
            restored_manifest = binary_root / ROLLBACK_MANIFEST_NAME
            if (
                not restored_manifest.is_file()
                or file_digest(restored_manifest)
                != critical_sha256[ROLLBACK_MANIFEST_NAME]
            ):
                raise RuntimeError("Binary drill mismatch for the Steam app manifest")
        payload.update({
            "status": "passed",
            "campaign_archive": str(campaign_archive),
            "campaign_sha256": campaign_sha256,
            "campaign_tiles": campaign_summary["tile_count"],
            "configuration_archive": str(config_archive),
            "configuration_sha256": config_sha256,
            "external_campaign": str(external_campaign),
            "external_configuration": str(external_config),
            "binary_archive": str(binary_archive),
            "binary_sha256": binary_sha256,
            "binary_file_count": binary_snapshot_summary(binary_archive)["file_count"],
        })
    except Exception as error:
        payload["error"] = str(error)
    payload["duration_seconds"] = round(time.monotonic() - started, 3)
    write_json_atomic(RESTORE_DRILL_FILE, payload)
    if payload["status"] == "passed":
        print(
            "Restore drill passed: campaign, configuration, and full binary archives "
            "extracted and verified against their source evidence."
        )
        return 0
    print(f"Restore drill failed: {payload['error']}", file=sys.stderr)
    return 1


def overall_health_label(service_healthy, campaign_status, watcher_running, rules_issues):
    if not service_healthy:
        return "UNHEALTHY"
    if campaign_status != "valid" or not watcher_running or rules_issues:
        return "DEGRADED"
    return "HEALTHY"


def status():
    state = ports()
    is_healthy = all(state.values())
    continuity = checkpoint_campaign_progress()
    campaign_status = campaign_state_status()
    restorable = latest_restorable_archive()
    freshness = backup_freshness()
    external = external_recovery_status()
    recovery_state = read_json(RECOVERY_STATE_FILE, default={})
    drill = read_json(RESTORE_DRILL_FILE, default={})
    rules = server_rules_summary()
    rules_issues = rules_profile_issues(rules)
    watcher_running = tmux_exists(BACKUP_SESSION)
    overall_health = overall_health_label(
        is_healthy, campaign_status, watcher_running, rules_issues
    )
    print(f"Overall health: {overall_health}")
    print(f"Game process: {'present' if process_exists() else 'stopped'}")
    print(f"Server tmux session: {'running' if tmux_exists(SERVER_SESSION) else 'stopped'}")
    print(f"Backup watcher: {'running' if watcher_running else 'stopped'}")
    print(f"TCP 7386: {'listening' if state['tcp_game'] else 'closed'}")
    print(f"UDP 7386: {'listening' if state['udp_game'] else 'closed'}")
    print(f"UDP 7387: {'listening' if state['udp_query'] else 'closed'}")
    print(f"Installed Steam build: {installed_build() or 'unknown'}")
    files = save_files()
    print(f"Campaign state files: {len(files)}")
    print(f"Live campaign map state: {campaign_status}")
    if campaign_status != "valid":
        print(
            "WARNING: Live campaign map state is incomplete; preserving the last "
            "restorable archive until a completed map produces valid state."
        )
    archives = sorted(BACKUP_DIR.glob("campaign-*.tar.gz"), reverse=True) if BACKUP_DIR.exists() else []
    print(f"Campaign backups: {len(archives)}")
    if archives:
        print(f"Newest backup: {archives[0]}")
    print(f"Last restorable backup: {restorable or 'none'}")
    print(
        "Restorable backup freshness: "
        f"{freshness['status']} ({format_age(freshness['age_seconds'])} old)"
    )
    if freshness["warning"]:
        print(f"WARNING: {freshness['warning']}")
    print(
        "Separate-disk recovery: "
        f"{'ready' if external['available'] else 'unavailable'} at {external['root']}"
    )
    if external["warning"]:
        print(f"WARNING: {external['warning']}")
    if isinstance(recovery_state, dict) and recovery_state.get("updated_at"):
        print(f"Last separate-disk sync: {recovery_state['updated_at']}")
    if isinstance(drill, dict) and drill.get("run_at"):
        print(
            f"Last restore drill: {drill.get('status', 'unknown')} at {drill['run_at']}"
        )
    else:
        print("Last restore drill: never")
    print(
        "Rules profile: "
        f"mode={rules['game_mode']}; campaign={rules['campaign']}; "
        f"bots={rules['attacker_bots']}v{rules['defender_bots']}; "
        f"hardcore={rules['hardcore']}; forced-first-person={rules['first_person']}"
    )
    print(
        "Pacing profile: "
        f"map={rules['map_minutes']}m; spawn waves={rules['wave_spawning']}; "
        f"respawn={rules['attacker_respawn_seconds']}s/"
        f"{rules['defender_respawn_seconds']}s; reset-if-empty={rules['reset_if_empty']}"
    )
    print(f"Rules validation: {'PASS' if not rules_issues else 'FAIL'}")
    for key, issue in rules_issues.items():
        print(
            f"WARNING: Rule drift for {key}: expected {issue['expected']}, "
            f"found {issue['actual']}"
        )
    print(
        "Campaign continuity: "
        f"{continuity.get('battles_completed', 0)} battles observed; "
        f"{continuity.get('campaigns_completed', 0)} native campaigns completed; "
        f"{continuity.get('restart_rollovers', 0)} restart rollovers recorded"
    )
    if continuity.get("current_tile_id") is not None:
        print(f"Last observed campaign tile: {continuity['current_tile_id']}")
    if continuity.get("last_completed_map"):
        print(f"Last completed map: {continuity['last_completed_map']}")
    if continuity.get("current_map_name"):
        print(f"Last observed current map: {continuity['current_map_name']}")
    if continuity.get("last_event_at"):
        print(f"Last campaign event: {continuity['last_event_at']}")
    if continuity.get("last_native_position") is not None:
        print(
            "Last native campaign position: "
            f"{continuity['last_native_position']} "
            f"({continuity.get('last_native_season')}, month "
            f"{continuity.get('last_native_month')})"
        )
    return 0 if is_healthy else 1


def watch():
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGHUP, request_stop)
    print(f"Watching campaign saves every {WATCH_INTERVAL} seconds.", flush=True)
    try:
        while not stopping:
            try:
                backup_campaign()
                emit_backup_freshness_warning()
            except Exception as error:
                print(f"Backup failed: {error}", file=sys.stderr, flush=True)
            for _ in range(WATCH_INTERVAL):
                if stopping:
                    break
                time.sleep(1)
    finally:
        if stopping:
            try:
                backup_campaign()
            except Exception as error:
                print(f"Final backup failed: {error}", file=sys.stderr, flush=True)
    return 0


def list_backups():
    archives = sorted(BACKUP_DIR.glob("campaign-*.tar.gz"), reverse=True) if BACKUP_DIR.exists() else []
    if not archives:
        print("No campaign backups exist yet.")
    for archive in archives:
        label = "restorable" if archive_campaign_summary(archive)["restorable"] else "incomplete"
        print(f"[{label}] {archive}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "start", "stop", "restart", "ensure", "status", "update", "backup", "watch",
        "checkpoint", "list-backups", "sync-recovery", "restore-drill",
        "snapshot-binaries", "snapshot-config", "rotate-player-password"
    ))
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    emit_run_marker(arguments.command)
    if arguments.command == "status":
        return status()
    if arguments.command == "backup":
        backup_campaign(force=arguments.force)
        return 0
    if arguments.command == "watch":
        return watch()
    if arguments.command == "checkpoint":
        checkpoint = checkpoint_campaign_progress()
        print(json.dumps(checkpoint, indent=2, sort_keys=True))
        return 0
    if arguments.command == "list-backups":
        return list_backups()
    if arguments.command == "sync-recovery":
        config_archive = latest_config_snapshot()
        payload = mirror_recovery_material(config_archive=config_archive)
        return 0 if payload is not None else 1
    if arguments.command == "restore-drill":
        return run_restore_drill()
    if arguments.command == "snapshot-binaries":
        if process_exists() or tmux_exists(SERVER_SESSION):
            print("Stop the game before creating a consistent binary snapshot.", file=sys.stderr)
            return 1
        create_binary_rollback_snapshot()
        return 0
    if arguments.command == "snapshot-config":
        config_snapshot()
        return 0
    with operation_lock() as acquired:
        if not acquired:
            return 0
        return {
            "start": start,
            "stop": stop,
            "restart": restart,
            "ensure": ensure,
            "update": update,
            "rotate-player-password": rotate_player_password,
        }[arguments.command]()


if __name__ == "__main__":
    raise SystemExit(main())
