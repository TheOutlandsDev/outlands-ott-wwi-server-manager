#!/usr/bin/env python3
"""Run the published manager's restore drill on fictional disposable archives."""
import importlib.util
import json
import tarfile
import tempfile
from pathlib import Path

SOURCE = Path(__file__).parent / 'ott-server-manager.py'
spec = importlib.util.spec_from_file_location('ott_manager_drill', SOURCE)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)

with tempfile.TemporaryDirectory(prefix='ott-synthetic-drill-') as temp:
    root = Path(temp)
    state = root / 'state'
    server = root / 'server'
    recovery = root / 'external-mirror'
    backups = state / 'campaign-backups'
    configs = state / 'update-backups'
    binaries = recovery / 'binary-rollbacks'
    for directory in (backups, configs, binaries, server / 'MpTiles', server / 'Over_The_Top_WWI_Data'):
        directory.mkdir(parents=True)
    (server / 'CampaignJson.json').write_text('{"fictional":true}\n')
    (server / 'MpTiles/tile-001.json').write_text('{"tile":1}\n')
    for name in ('ServerConfiguration.ini', 'ServerInfo.ini', 'MpCampaign.txt'):
        (server / name).write_text('fictional fixture\n')
    (server / 'Over_The_Top_WWI.exe').write_bytes(b'fictional executable fixture\n')
    (server / 'GameAssembly.dll').write_bytes(b'fictional DLL fixture\n')
    (server / 'Over_The_Top_WWI_Data/marker.txt').write_text('fictional data\n')
    manifest = root / 'appmanifest_3052620.acf'
    manifest.write_text('fictional Steam manifest\n')
    campaign = backups / 'campaign-20261109-000000-fixture.tar.gz'
    with tarfile.open(campaign, 'w:gz') as archive:
        for name in ('CampaignJson.json', 'MpTiles/tile-001.json'):
            archive.add(server / name, arcname=name)
    config = configs / 'pre-update-20261109-000000.tar.gz'
    with tarfile.open(config, 'w:gz') as archive:
        for name in ('ServerConfiguration.ini', 'ServerInfo.ini', 'MpCampaign.txt'):
            archive.add(server / name, arcname=name)
    binary = binaries / 'pre-update-build-fixture-20261109-000000.tar'
    with tarfile.open(binary, 'w') as archive:
        for name in ('Over_The_Top_WWI.exe', 'GameAssembly.dll', 'Over_The_Top_WWI_Data/marker.txt'):
            archive.add(server / name, arcname=name)
        archive.add(manifest, arcname=manager.ROLLBACK_MANIFEST_NAME)
    manager.OUTPUT_DIR = state
    manager.SERVER_DIR = server
    manager.BACKUP_DIR = backups
    manager.UPDATE_BACKUP_DIR = configs
    manager.EXTERNAL_RECOVERY_ROOT = recovery
    manager.BINARY_ROLLBACK_STATE_FILE = state / 'binary-rollback-state.json'
    manager.RECOVERY_STATE_FILE = state / 'recovery-state.json'
    manager.RESTORE_DRILL_FILE = state / 'restore-drill.json'
    manager.LAST_RESTORABLE_FILE = backups / '.last-restorable.json'
    manager.CONTINUITY_FILE = state / 'campaign-continuity.json'
    manager.external_recovery_status = lambda prepare=False: {'available': True, 'separate_device': True, 'root': str(recovery), 'warning': None}
    manager.write_json_atomic(manager.BINARY_ROLLBACK_STATE_FILE, {
        'archive': str(binary),
        'archive_sha256': manager.file_digest(binary),
        'critical_sha256': {
            'Over_The_Top_WWI.exe': manager.file_digest(server / 'Over_The_Top_WWI.exe'),
            'GameAssembly.dll': manager.file_digest(server / 'GameAssembly.dll'),
            manager.ROLLBACK_MANIFEST_NAME: manager.file_digest(manifest),
        },
    })
    result = manager.run_restore_drill()
    report = manager.read_json(manager.RESTORE_DRILL_FILE)
    assert result == 0 and report['status'] == 'passed'
    assert manager.file_digest(recovery / 'campaign-backups' / campaign.name) == manager.file_digest(campaign)
    assert manager.file_digest(recovery / 'configuration-backups' / config.name) == manager.file_digest(config)
    print(json.dumps({'status': report['status'], 'campaign_tiles': report['campaign_tiles'], 'binary_file_count': report['binary_file_count'], 'synthetic_only': True}, sort_keys=True))
