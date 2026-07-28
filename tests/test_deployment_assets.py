from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_maintenance_worker_is_isolated_and_resource_limited():
    unit = (ROOT / "deploy/systemd/kuratorr-worker-maintenance.service").read_text()
    compose = (ROOT / "compose.yaml").read_text()

    assert "--queues=maintenance" in unit
    assert "--concurrency=1" in unit
    assert "TimeoutStopSec=30s" in unit
    assert "CPUQuota=50%" in unit
    assert "MemoryHigh=512M" in unit
    assert "MemoryMax=768M" in unit
    assert "worker-maintenance:" in compose
    assert '"--queues=maintenance"' in compose
    assert "cpus: 0.5" in compose
    assert "mem_limit: 768m" in compose


def test_updater_stops_services_before_migration_and_restarts_on_exit():
    updater = (ROOT / "scripts/update-from-git.sh").read_text()

    stop_position = updater.index('systemctl stop "${KURATORR_SERVICES[@]}"')
    migrate_position = updater.index('"$APP_DIR/manage.py" migrate --noinput')
    assert stop_position < migrate_position
    assert "kuratorr-worker-maintenance" in updater
    assert "trap finish_update EXIT" in updater
    assert 'systemctl start "${KURATORR_SERVICES[@]}" || true' in updater


def test_install_and_reset_manage_maintenance_worker():
    installer = (ROOT / "scripts/install-lxc.sh").read_text()
    resetter = (ROOT / "scripts/reset-database.sh").read_text()

    assert "kuratorr-worker-maintenance.service" in installer
    assert "kuratorr-worker-maintenance" in installer
    assert "kuratorr-worker-maintenance" in resetter
