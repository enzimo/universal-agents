from __future__ import annotations

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.interactive_apps import InteractiveAppManager


def test_interactive_app_manager_registers_photon(tmp_path):
    config = GatewayInstanceConfig.create(
        instance_name="test",
        instance_root=str(tmp_path),
        team_id="team_test",
        timezone="UTC",
    )
    config.interactive_apps.photon.enabled = True

    manager = InteractiveAppManager(config, application=object())

    assert "photon" in manager.apps
    assert "photon" in manager.enabled_app_names
    assert manager.apps["photon"].application is manager.gateway_application
    assert not hasattr(manager.apps["photon"], "gateway")
