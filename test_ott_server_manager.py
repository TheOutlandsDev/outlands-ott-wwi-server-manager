#!/usr/bin/env python3

import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("ott-server-manager.py")
SPEC = importlib.util.spec_from_file_location("ott_server_manager", MODULE_PATH)
MANAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANAGER)


class ContinuityParserTests(unittest.TestCase):
    def test_restart_position_regression_is_recorded(self):
        checkpoint = MANAGER.new_continuity_checkpoint()
        MANAGER.apply_campaign_log_line(
            checkpoint,
            "12:00:00: Log: season spot 22  month 11  TheSeason Autumn",
            "2026-09-13T12:00:00",
        )
        MANAGER.apply_campaign_log_line(
            checkpoint,
            "12:50:24: Log: season spot 0  month 6  TheSeason Summer",
            "2026-09-14T12:50:24",
        )
        self.assertEqual(checkpoint["restart_rollovers"], 1)
        self.assertEqual(checkpoint["last_native_position"], 0)

    def test_normal_campaign_completion_is_not_a_restart_rollover(self):
        checkpoint = MANAGER.new_continuity_checkpoint()
        MANAGER.apply_campaign_log_line(
            checkpoint,
            "09:14:00: Log: season spot 41  month 6  TheSeason Summer",
            "2026-09-12T09:14:00",
        )
        MANAGER.apply_campaign_log_line(
            checkpoint,
            "09:15:33: Log: Campaign Over: Tie Breaker",
            "2026-09-12T09:15:33",
        )
        MANAGER.apply_campaign_log_line(
            checkpoint,
            "09:17:06: Log: season spot 0  month 6  TheSeason Summer",
            "2026-09-12T09:17:06",
        )
        self.assertEqual(checkpoint["campaigns_completed"], 1)
        self.assertEqual(checkpoint["restart_rollovers"], 0)

    def test_battle_and_map_fields_are_captured(self):
        checkpoint = MANAGER.new_continuity_checkpoint()
        lines = (
            "12:06:48: Round Ended.",
            "12:06:48: Map:  Brakethrough_",
            "12:08:32: Log: tile id 3",
            "12:08:32: Log: ran id 74",
            "12:08:32: Log: path Z:\\server\\MpTiles\\Tile3.json",
        )
        for line in lines:
            MANAGER.apply_campaign_log_line(checkpoint, line, "2026-09-13T12:08:32")
        self.assertEqual(checkpoint["battles_completed"], 1)
        self.assertEqual(checkpoint["last_completed_map"], "Brakethrough_")
        self.assertEqual(checkpoint["current_tile_id"], 3)
        self.assertEqual(checkpoint["current_map_run_id"], 74)
        self.assertTrue(checkpoint["current_map_path"].endswith("Tile3.json"))


class CampaignArchiveTests(unittest.TestCase):
    @staticmethod
    def make_archive(path, names):
        with tarfile.open(path, "w:gz") as archive:
            for name in names:
                data = b"{}\n"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    def test_archive_requires_campaign_and_at_least_one_tile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incomplete = root / "incomplete.tar.gz"
            complete = root / "complete.tar.gz"
            self.make_archive(incomplete, ["CampaignJson.json", "MpCampaign.txt"])
            self.make_archive(complete, ["CampaignJson.json", "MpTiles/Tile3.json"])
            self.assertFalse(MANAGER.archive_campaign_summary(incomplete)["restorable"])
            summary = MANAGER.archive_campaign_summary(complete)
            self.assertTrue(summary["restorable"])
            self.assertEqual(summary["tile_count"], 1)

    def test_restore_extracts_latest_restorable_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "server"
            backups = Path(directory) / "backups"
            root.mkdir()
            backups.mkdir()
            archive = backups / "campaign-20260913-120714-test.tar.gz"
            self.make_archive(archive, ["CampaignJson.json", "MpTiles/Tile3.json"])

            with mock.patch.object(MANAGER, "SERVER_DIR", root), mock.patch.object(
                MANAGER, "BACKUP_DIR", backups
            ), mock.patch.object(
                MANAGER, "LAST_RESTORABLE_FILE", backups / ".last-restorable.json"
            ):
                restored = MANAGER.restore_latest_campaign_if_needed()

            self.assertEqual(restored, archive)
            self.assertTrue((root / "CampaignJson.json").is_file())
            self.assertTrue((root / "MpTiles" / "Tile3.json").is_file())
            marker = MANAGER.read_json(backups / ".last-restorable.json")
            self.assertEqual(marker["tile_count"], 1)
            self.assertEqual(marker["archive_sha256"], MANAGER.file_digest(archive))


class RecoveryHardeningTests(unittest.TestCase):
    def test_overall_health_distinguishes_service_failure_and_degradation(self):
        self.assertEqual(
            MANAGER.overall_health_label(True, "valid", True, {}), "HEALTHY"
        )
        self.assertEqual(
            MANAGER.overall_health_label(True, "invalid", True, {}), "DEGRADED"
        )
        self.assertEqual(
            MANAGER.overall_health_label(True, "valid", False, {}), "DEGRADED"
        )
        self.assertEqual(
            MANAGER.overall_health_label(True, "valid", True, {"bots": {}}),
            "DEGRADED",
        )
        self.assertEqual(
            MANAGER.overall_health_label(False, "valid", True, {}), "UNHEALTHY"
        )

    def test_run_marker_records_command_process_and_boot(self):
        output = io.StringIO()
        with mock.patch.object(MANAGER, "current_boot_id", return_value="boot-test"), \
                mock.patch("sys.stdout", output):
            MANAGER.emit_run_marker("ensure")
        marker = output.getvalue()
        self.assertIn("Manager run: timestamp=", marker)
        self.assertIn("command=ensure", marker)
        self.assertIn(f"pid={os.getpid()}", marker)
        self.assertIn("boot_id=boot-test", marker)

    def test_host_uptime_seconds_parses_proc_value(self):
        with mock.patch.object(MANAGER.Path, "read_text", return_value="123.45 67.89\n"):
            self.assertEqual(MANAGER.host_uptime_seconds(), 123.45)

    def test_host_uptime_seconds_returns_none_for_invalid_value(self):
        with mock.patch.object(MANAGER.Path, "read_text", return_value="invalid\n"):
            self.assertIsNone(MANAGER.host_uptime_seconds())

    def test_ensure_defers_recovery_during_boot_grace(self):
        output = io.StringIO()
        with mock.patch.object(MANAGER, "checkpoint_campaign_progress"), \
                mock.patch.object(MANAGER, "healthy", return_value=False), \
                mock.patch.object(MANAGER, "host_uptime_seconds", return_value=120), \
                mock.patch.object(MANAGER, "ensure_backup_watcher") as watcher, \
                mock.patch.object(MANAGER, "start_game") as start_game, \
                mock.patch("sys.stdout", output):
            result = MANAGER.ensure()
        self.assertEqual(result, 0)
        watcher.assert_called_once_with()
        start_game.assert_not_called()
        self.assertIn("Host boot grace active", output.getvalue())

    def test_generated_player_password_meets_length_and_character_requirements(self):
        password = MANAGER.generate_player_password()
        self.assertEqual(len(password), 18)
        self.assertTrue(any(character.isupper() for character in password))
        self.assertTrue(any(character.islower() for character in password))
        self.assertTrue(any(character.isdigit() for character in password))
        self.assertTrue(any(character in "!@#$%*+-_" for character in password))

    def test_password_rotation_snapshots_config_and_writes_private_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server_info = root / "ServerInfo.ini"
            private_dir = root / "private"
            credential = private_dir / "playtest-player-password.txt"
            server_info.write_bytes(
                b"AdminPassword = admin-secret\r\n"
                b"ServerPassword = old-player-secret\r\n"
            )
            os.chmod(server_info, 0o600)
            with mock.patch.object(
                MANAGER, "SERVER_INFO_FILE", server_info
            ), mock.patch.object(
                MANAGER, "PRIVATE_DIR", private_dir
            ), mock.patch.object(
                MANAGER, "PLAYER_PASSWORD_FILE", credential
            ), mock.patch.object(
                MANAGER, "config_snapshot"
            ) as snapshot:
                MANAGER.rotate_player_password()
            snapshot.assert_called_once_with()
            new_password = credential.read_text().strip()
            content = server_info.read_text()
            self.assertIn(f"ServerPassword = {new_password}", content)
            self.assertNotIn("old-player-secret", content)
            self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
            self.assertEqual(private_dir.stat().st_mode & 0o777, 0o700)

    def test_backup_freshness_reports_fresh_and_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "campaign.tar.gz"
            archive.write_bytes(b"archive")
            os.utime(archive, (1_000, 1_000))
            with mock.patch.object(
                MANAGER, "latest_restorable_archive", return_value=archive
            ):
                fresh = MANAGER.backup_freshness(now=1_000 + 60)
                stale = MANAGER.backup_freshness(
                    now=1_000 + MANAGER.STALE_BACKUP_SECONDS + 1
                )
            self.assertEqual(fresh["status"], "fresh")
            self.assertEqual(stale["status"], "stale")
            self.assertIn("completed map", stale["warning"])

    def test_atomic_external_copy_is_byte_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tar.gz"
            destination = root / "recovery" / "copy.tar.gz"
            source.write_bytes(b"campaign-state")
            digest = MANAGER.atomic_copy_verified(source, destination)
            self.assertEqual(digest, MANAGER.file_digest(destination))
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_config_files_include_custom_maps_and_exclude_old_codex_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = root / "CustomMaps"
            custom.mkdir()
            current = custom / "Current.json"
            old = custom / "Current.json.codex-backup-previous"
            current.write_text("{}")
            old.write_text("{}")
            info = root / "ServerInfo.ini"
            info.write_text("ServerName = test\n")
            with mock.patch.object(MANAGER, "CONFIG_INPUTS", (info, custom)):
                files = MANAGER.config_files()
            self.assertIn(current, files)
            self.assertIn(info, files)
            self.assertNotIn(old, files)

    def test_recovery_manifest_merges_prior_copy_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            state = root / "recovery-state.json"
            campaign = root / "campaign.tar.gz"
            config = root / "config.tar.gz"
            campaign.write_bytes(b"campaign")
            config.write_bytes(b"configuration")
            MANAGER.write_json_atomic(
                state,
                {
                    "root": str(external),
                    "copies": {"previous": {"path": "kept", "sha256": "abc"}},
                },
            )
            available = {
                "available": True,
                "separate_device": True,
                "root": str(external),
                "warning": None,
            }
            with mock.patch.object(
                MANAGER, "EXTERNAL_RECOVERY_ROOT", external
            ), mock.patch.object(
                MANAGER, "RECOVERY_STATE_FILE", state
            ), mock.patch.object(
                MANAGER, "external_recovery_status", return_value=available
            ):
                payload = MANAGER.mirror_recovery_material(campaign, config)
            self.assertIn("previous", payload["copies"])
            self.assertIn("campaign", payload["copies"])
            self.assertIn("configuration", payload["copies"])

    def test_binary_snapshot_restore_replaces_failed_build_and_restores_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "server"
            server.mkdir()
            (server / "current.txt").write_text("failed update")
            manifest = root / "appmanifest.acf"
            manifest.write_text("new build")
            snapshot = root / "rollback.tar"
            with tarfile.open(snapshot, "w") as archive:
                for name, data in (
                    ("Over_The_Top_WWI.exe", b"old exe"),
                    ("GameAssembly.dll", b"old dll"),
                    ("Over_The_Top_WWI_Data/data.bin", b"old data"),
                    (MANAGER.ROLLBACK_MANIFEST_NAME, b"old build"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            with mock.patch.object(MANAGER, "SERVER_DIR", server), mock.patch.object(
                MANAGER, "MANIFEST", manifest
            ):
                quarantine = MANAGER.restore_binary_rollback_snapshot(snapshot)
            self.assertEqual((server / "Over_The_Top_WWI.exe").read_bytes(), b"old exe")
            self.assertEqual(manifest.read_text(), "old build")
            self.assertEqual((quarantine / "current.txt").read_text(), "failed update")


class RestartFlowTests(unittest.TestCase):
    def test_restart_restores_after_stop_before_launch(self):
        calls = []

        with mock.patch.object(
            MANAGER, "checkpoint_campaign_progress", side_effect=lambda: calls.append("checkpoint")
        ), mock.patch.object(
            MANAGER, "backup_campaign", side_effect=lambda: calls.append("backup")
        ), mock.patch.object(
            MANAGER, "stop_game", side_effect=lambda: calls.append("stop")
        ), mock.patch.object(
            MANAGER,
            "restore_latest_campaign_if_needed",
            side_effect=lambda force_restore=False: calls.append(f"restore:{force_restore}"),
        ), mock.patch.object(
            MANAGER, "ensure_backup_watcher", side_effect=lambda: calls.append("watcher")
        ), mock.patch.object(
            MANAGER, "start_game", side_effect=lambda: calls.append("start") or 0
        ):
            self.assertEqual(MANAGER.restart(), 0)

        self.assertEqual(
            calls,
            ["checkpoint", "backup", "stop", "backup", "restore:True", "watcher", "start"],
        )


class UpdateRollbackTests(unittest.TestCase):
    def test_failed_update_recovery_restores_binary_config_campaign_then_starts(self):
        calls = []
        with mock.patch.object(
            MANAGER, "stop_game", side_effect=lambda: calls.append("stop")
        ), mock.patch.object(
            MANAGER,
            "restore_binary_rollback_snapshot",
            side_effect=lambda archive: calls.append(f"binary:{archive}"),
        ), mock.patch.object(
            MANAGER,
            "restore_snapshot",
            side_effect=lambda archive: calls.append(f"config:{archive}"),
        ), mock.patch.object(
            MANAGER,
            "restore_latest_campaign_if_needed",
            side_effect=lambda force_restore=False: calls.append(
                f"campaign:{force_restore}"
            ),
        ), mock.patch.object(
            MANAGER, "start", side_effect=lambda: calls.append("start") or 0
        ):
            result = MANAGER.recover_failed_update("config.tar", "binary.tar", "test")
        self.assertEqual(result, 1)
        self.assertEqual(
            calls,
            [
                "stop",
                "binary:binary.tar",
                "config:config.tar",
                "campaign:True",
                "start",
            ],
        )

    def test_updated_build_health_failure_enters_rollback(self):
        completed = mock.Mock(returncode=0, stdout="fully installed", stderr="")
        with mock.patch.object(MANAGER, "public_build", return_value="2"), mock.patch.object(
            MANAGER, "installed_build", side_effect=["1", "2"]
        ), mock.patch.object(MANAGER, "backup_campaign"), mock.patch.object(
            MANAGER, "config_snapshot", return_value=Path("config.tar.gz")
        ), mock.patch.object(MANAGER, "stop_game"), mock.patch.object(
            MANAGER, "create_binary_rollback_snapshot", return_value=Path("binary.tar")
        ), mock.patch.object(MANAGER, "run", return_value=completed), mock.patch.object(
            MANAGER, "restore_snapshot"
        ), mock.patch.object(
            MANAGER, "restore_latest_campaign_if_needed"
        ), mock.patch.object(MANAGER, "start", return_value=1), mock.patch.object(
            MANAGER, "recover_failed_update", return_value=1
        ) as recover:
            result = MANAGER.update()
        self.assertEqual(result, 1)
        recover.assert_called_once()
        self.assertIn("startup health check", str(recover.call_args.args[2]))


if __name__ == "__main__":
    unittest.main()
