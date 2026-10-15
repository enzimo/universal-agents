from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel

from runtime_settings.interactive_apps import AdapterHostSettings, ExternalInteractiveAppSettings, InteractiveAppPluginConfiguration
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.interactive_apps.base import BaseInteractiveApp
from assistant_gateway.interactive_apps.builtin_registry import (
    BUILTIN_INTERACTIVE_APP_DESCRIPTORS,
    build_interactive_app_registry,
)
from assistant_gateway.interactive_apps.contracts import (
    INTERACTIVE_APP_PLUGIN_API_VERSION,
    InteractiveAppBuildContext,
    InteractiveAppCapability,
    InteractiveAppDependencyReport,
    InteractiveAppDescriptor,
    InteractiveAppDiscoveryStatus,
    InteractiveAppLifecycleState,
    InteractiveAppOnboardingMetadata,
    InteractiveAppPackageProvenance,
    InteractiveAppPluginOrigin,
)
from assistant_gateway.interactive_apps.manager import InteractiveAppManager
from assistant_gateway.interactive_apps.registry import (
    InteractiveAppRegistrationError,
    InteractiveAppRegistry,
)


class _TestInteractiveApp(BaseInteractiveApp):
    def __init__(
        self,
        *,
        channel_name: str,
        context: InteractiveAppBuildContext,
        events: list[str],
    ) -> None:
        self.channel_name = channel_name
        self.application = context.application
        self.bridge = context.bridge
        self._events = events
        self.deliveries = []

    @property
    def enabled(self) -> bool:
        return True

    async def start(self) -> None:
        self._events.append(f"{self.channel_name}.start")

    async def stop(self) -> None:
        self._events.append(f"{self.channel_name}.stop")

    async def deliver_event(self, event) -> None:
        self.deliveries.append(event)

    async def emit_text_activity_update(self, _context, _text: str) -> None:
        return None


class _TestSettings(BaseModel):
    enabled: bool = True


class _SettingsWithoutEnablement(BaseModel):
    value: str = "invalid"


class _FakeDistribution:
    def __init__(self, name: str, version: str = "1.0.0") -> None:
        self.name = name
        self.version = version


class _FakeEntryPoint:
    group = "universal_agents.chat_adapters.v1"

    def __init__(
        self,
        *,
        name: str,
        distribution: str,
        provider: object,
    ) -> None:
        self.name = name
        self.value = "example_plugin:descriptor"
        self.dist = _FakeDistribution(distribution)
        self._provider = provider
        self.load_calls = 0

    def load(self) -> object:
        self.load_calls += 1
        return self._provider


_TEST_PROVENANCE = InteractiveAppPackageProvenance(
    origin=InteractiveAppPluginOrigin.PYTHON_ENTRY_POINT,
    distribution_name="test-interactive-app",
    distribution_version="1.0.0",
    entry_point_group="universal_agents.chat_adapters.v1",
    entry_point_name="example.test",
    entry_point_value="example_plugin:descriptor",
)


def _config(tmp_path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="registry-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_registry",
        timezone="UTC",
    )
    config.interactive_apps.internal_website.enabled = False
    return config


def _descriptor(
    *,
    plugin_id: str,
    channel_name: str,
    factory: Callable[[InteractiveAppBuildContext], BaseInteractiveApp],
    dependency_report: InteractiveAppDependencyReport | None = None,
    plugin_api_version: str = INTERACTIVE_APP_PLUGIN_API_VERSION,
    configuration_model: type[BaseModel] = _TestSettings,
) -> InteractiveAppDescriptor:
    report = dependency_report or InteractiveAppDependencyReport(
        available=True,
        detail="test dependencies available",
    )
    return InteractiveAppDescriptor(
        plugin_id=plugin_id,
        channel_name=channel_name,
        display_name=channel_name.title(),
        plugin_api_version=plugin_api_version,
        implementation_version="test",
        capabilities=frozenset(
            {
                InteractiveAppCapability.INBOUND_TEXT,
                InteractiveAppCapability.OUTBOUND_TEXT,
            }
        ),
        configuration_model=configuration_model,
        dependency_probe=lambda _configuration: report,
        factory=factory,
        onboarding=InteractiveAppOnboardingMetadata(
            summary=f"Test adapter for {channel_name}.",
            setup_order=1,
        ),
        documentation_reference="tests/test_gateway_interactive_app_registry.py",
    )


def _configure_optional(
    config: GatewayInstanceConfig,
    plugin_id: str,
    *,
    enabled: bool = True,
    distribution: str = "test-interactive-app",
) -> None:
    config.interactive_apps.plugins[plugin_id] = InteractiveAppPluginConfiguration(
        distribution=distribution,
        settings=ExternalInteractiveAppSettings(enabled=enabled),
    )


def test_builtin_registry_declares_stable_order_configuration_and_capabilities() -> (
    None
):
    registry = build_interactive_app_registry()

    assert [descriptor.plugin_id for descriptor in registry.descriptors] == [
        "builtin.discord",
        "builtin.telegram",
        "builtin.whatsapp",
        "builtin.signal",
        "builtin.photon",
        "builtin.internal_website",
    ]
    assert registry.descriptors == BUILTIN_INTERACTIVE_APP_DESCRIPTORS
    for descriptor in registry.descriptors:
        assert descriptor.configuration_schema()["type"] == "object"
        assert descriptor.onboarding.summary
        assert InteractiveAppCapability.INBOUND_TEXT in descriptor.capabilities
        assert InteractiveAppCapability.OUTBOUND_TEXT in descriptor.capabilities
        assert InteractiveAppCapability.PROACTIVE_DELIVERY in descriptor.capabilities
        documentation = Path(__file__).resolve().parents[1] / descriptor.documentation_reference.split("#", 1)[0]
        assert documentation.is_file()

    telegram = registry.descriptor_for_plugin("builtin.telegram")
    assert telegram is not None
    assert InteractiveAppCapability.THREADS_TOPICS in telegram.capabilities
    assert InteractiveAppCapability.NATIVE_COMMANDS in telegram.capabilities
    assert InteractiveAppCapability.POLLING_LIFECYCLE in telegram.capabilities
    assert telegram.secret_reference_names == ("TELEGRAM_BOT_TOKEN",)

    website = registry.descriptor_for_plugin("builtin.internal_website")
    assert website is not None
    assert InteractiveAppCapability.ATTACHMENTS in website.capabilities
    assert InteractiveAppCapability.WEBHOOK_LIFECYCLE in website.capabilities
    assert website.secret_reference_names == ("WEB_CHAT_AUTH_TOKEN",)
    for descriptor in registry.descriptors:
        assert InteractiveAppCapability.NATIVE_COMMANDS in descriptor.capabilities


def test_external_execution_configuration_fails_closed_on_invalid_topology(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    raw = config.model_dump(mode="python")
    telegram = raw["interactive_apps"]["plugins"]["builtin.telegram"]
    telegram["settings"]["enabled"] = True
    telegram["execution"] = "adapter_host"

    with pytest.raises(ValueError, match="adapter_host.enabled"):
        GatewayInstanceConfig.model_validate(raw)

    raw["interactive_apps"]["adapter_host"]["enabled"] = True
    validated = GatewayInstanceConfig.model_validate(raw)
    assert validated.interactive_apps.telegram.enabled is True

    raw["interactive_apps"]["plugins"]["builtin.internal_website"]["settings"][
        "enabled"
    ] = True
    raw["interactive_apps"]["plugins"]["builtin.internal_website"][
        "execution"
    ] = "adapter_host"
    with pytest.raises(ValueError, match="must use execution=in_process"):
        GatewayInstanceConfig.model_validate(raw)


def test_adapter_host_lease_must_cover_two_heartbeat_intervals() -> None:
    with pytest.raises(ValueError, match="at least twice"):
        AdapterHostSettings(
            lease_seconds=10,
            limits={"heartbeat_interval_seconds": 6},
        )


def test_registry_rejects_unallowlisted_optional_duplicate_and_incompatible_plugins() -> (
    None
):
    def factory(context: InteractiveAppBuildContext) -> BaseInteractiveApp:
        return _TestInteractiveApp(
            channel_name="test",
            context=context,
            events=[],
        )

    descriptor = _descriptor(
        plugin_id="example.test",
        channel_name="test",
        factory=factory,
    )
    registry = InteractiveAppRegistry()

    with pytest.raises(InteractiveAppRegistrationError, match="not allowlisted"):
        registry.register_optional(descriptor, provenance=_TEST_PROVENANCE)

    registry = InteractiveAppRegistry(
        allowed_optional_plugins={"example.test": "test-interactive-app"}
    )
    with pytest.raises(InteractiveAppRegistrationError, match="does not match"):
        registry.register_optional(
            descriptor,
            provenance=replace(
                _TEST_PROVENANCE,
                distribution_name="wrong-distribution",
            ),
        )
    registry.register_optional(
        descriptor,
        provenance=replace(_TEST_PROVENANCE, entry_point_name="example.test"),
    )
    with pytest.raises(InteractiveAppRegistrationError, match="already registered"):
        registry.register_builtin(descriptor)

    duplicate_channel = _descriptor(
        plugin_id="example.other",
        channel_name="test",
        factory=factory,
    )
    with pytest.raises(InteractiveAppRegistrationError, match="already owned"):
        registry.register_builtin(duplicate_channel)

    incompatible = _descriptor(
        plugin_id="example.future",
        channel_name="future",
        factory=factory,
        plugin_api_version="999",
    )
    with pytest.raises(InteractiveAppRegistrationError, match="supports '1'"):
        registry.register_builtin(incompatible)


def test_descriptor_configuration_model_requires_boolean_enablement() -> None:
    with pytest.raises(TypeError, match="enabled boolean field"):
        _descriptor(
            plugin_id="example.invalid-settings",
            channel_name="invalid-settings",
            factory=lambda context: _TestInteractiveApp(
                channel_name="invalid-settings",
                context=context,
                events=[],
            ),
            configuration_model=_SettingsWithoutEnablement,
        )


def test_entry_point_discovery_loads_only_enabled_allowlisted_package(tmp_path) -> None:
    descriptor = _descriptor(
        plugin_id="example.echo",
        channel_name="echo",
        factory=lambda context: _TestInteractiveApp(
            channel_name="echo",
            context=context,
            events=[],
        ),
    )
    entry_point = _FakeEntryPoint(
        name="example.echo",
        distribution="Example_Package",
        provider=lambda: descriptor,
    )
    unrelated = _FakeEntryPoint(
        name="unapproved.plugin",
        distribution="unapproved-package",
        provider=lambda: descriptor,
    )
    config = _config(tmp_path)
    _configure_optional(
        config,
        "example.echo",
        distribution="example-package",
    )

    registry = build_interactive_app_registry(
        config,
        entry_point_provider=lambda **_kwargs: (entry_point, unrelated),
    )

    assert registry.descriptor_for_plugin("example.echo") is descriptor
    assert entry_point.load_calls == 1
    assert unrelated.load_calls == 0
    provenance = registry.provenance_for_plugin("example.echo")
    assert provenance is not None
    assert provenance.distribution_name == "example-package"
    assert provenance.distribution_version == "1.0.0"
    assert registry.discovery_diagnostics[0].status == (
        InteractiveAppDiscoveryStatus.REGISTERED
    )


def test_disabled_builtin_adapters_are_not_imported_in_clean_runtime() -> None:
    script = """
import json
import sys
import tempfile
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.interactive_apps import InteractiveAppManager

with tempfile.TemporaryDirectory() as root:
    config = GatewayInstanceConfig.create(
        instance_name="lazy-import",
        instance_root=root,
        team_id="team_lazy",
        timezone="UTC",
    )
    config.interactive_apps.internal_website.enabled = False
    InteractiveAppManager(config, application=object())

modules = [
    "assistant_gateway.interactive_apps.discord",
    "assistant_gateway.interactive_apps.telegram",
    "assistant_gateway.interactive_apps.whatsapp",
    "assistant_gateway.interactive_apps.signal",
    "assistant_gateway.interactive_apps.photon",
    "assistant_gateway.interactive_apps.website",
]
print(json.dumps({module: module in sys.modules for module in modules}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert set(json.loads(result.stdout).values()) == {False}


def test_registry_plugin_cannot_replace_builtin_website() -> None:
    descriptor = _descriptor(
        plugin_id="test.website_override",
        channel_name="website",
        factory=lambda context: _TestInteractiveApp(
            channel_name="website",
            context=context,
            events=[],
        ),
    )
    registry = InteractiveAppRegistry(
        allowed_optional_plugins={"test.website_override": "test-interactive-app"}
    )
    for builtin in BUILTIN_INTERACTIVE_APP_DESCRIPTORS:
        registry.register_builtin(builtin)

    with pytest.raises(InteractiveAppRegistrationError, match="already owned"):
        registry.register_optional(
            descriptor,
            provenance=replace(
                _TEST_PROVENANCE,
                entry_point_name="test.website_override",
            ),
        )


@pytest.mark.asyncio
async def test_manager_constructs_and_tracks_allowlisted_test_plugin_without_code_changes(
    tmp_path,
) -> None:
    events: list[str] = []
    descriptor = _descriptor(
        plugin_id="test.echo",
        channel_name="echo",
        factory=lambda context: _TestInteractiveApp(
            channel_name="echo",
            context=context,
            events=events,
        ),
    )
    registry = InteractiveAppRegistry(
        allowed_optional_plugins={"test.echo": "test-interactive-app"}
    )
    registry.register_optional(
        descriptor,
        provenance=replace(_TEST_PROVENANCE, entry_point_name="test.echo"),
    )
    config = _config(tmp_path)
    _configure_optional(config, "test.echo")

    manager = InteractiveAppManager(
        config,
        application=object(),
        registry=registry,
    )

    assert manager.enabled_app_names == ["echo"]
    assert manager.apps["echo"].application is manager.gateway_application
    health = manager.plugin_health_for_channel("echo")
    assert health is not None
    assert health.state == InteractiveAppLifecycleState.REGISTERED
    assert health.selected is True
    assert health.dependency_report is not None
    assert health.dependency_report.available is True

    await manager.start()
    assert manager.running_app_names == ["echo"]
    assert (
        manager.plugin_health_for_channel("echo").state
        == InteractiveAppLifecycleState.RUNNING
    )

    await manager.stop()
    assert events == ["echo.start", "echo.stop"]
    assert (
        manager.plugin_health_for_channel("echo").state
        == InteractiveAppLifecycleState.STOPPED
    )


@pytest.mark.asyncio
async def test_dependency_failure_is_diagnostic_and_does_not_stop_other_plugins(
    tmp_path,
    caplog,
) -> None:
    events: list[str] = []
    missing_factory_calls: list[str] = []
    missing = _descriptor(
        plugin_id="test.missing",
        channel_name="missing",
        factory=lambda context: (
            missing_factory_calls.append("called")
            or _TestInteractiveApp(
                channel_name="missing",
                context=context,
                events=events,
            )
        ),
        dependency_report=InteractiveAppDependencyReport(
            available=False,
            checked=("missing-sdk",),
            missing=("missing-sdk",),
            installation_hint="Install the missing-sdk extra.",
            detail="Missing Python package(s): missing-sdk.",
        ),
    )
    healthy = _descriptor(
        plugin_id="test.healthy",
        channel_name="healthy",
        factory=lambda context: _TestInteractiveApp(
            channel_name="healthy",
            context=context,
            events=events,
        ),
    )
    registry = InteractiveAppRegistry(
        allowed_optional_plugins={
            "test.missing": "test-interactive-app",
            "test.healthy": "test-interactive-app",
        }
    )
    registry.register_optional(
        missing,
        provenance=replace(_TEST_PROVENANCE, entry_point_name="test.missing"),
    )
    registry.register_optional(
        healthy,
        provenance=replace(_TEST_PROVENANCE, entry_point_name="test.healthy"),
    )
    config = _config(tmp_path)
    _configure_optional(config, "test.missing")
    _configure_optional(config, "test.healthy")
    manager = InteractiveAppManager(
        config,
        application=object(),
        registry=registry,
    )

    assert missing_factory_calls == []
    assert (
        manager.plugin_health_for_channel("missing").state
        == InteractiveAppLifecycleState.DEPENDENCY_UNAVAILABLE
    )

    with caplog.at_level("WARNING"):
        await manager.start()

    assert missing_factory_calls == []
    assert manager.running_app_names == ["healthy"]
    assert events == ["healthy.start"]
    missing_health = manager.plugin_health_for_channel("missing")
    assert missing_health.state == InteractiveAppLifecycleState.FAILED
    assert missing_health.dependency_report.missing == ("missing-sdk",)
    assert "Install the missing-sdk extra" in missing_health.error
    assert "plugin_id=test.missing" in caplog.text
    assert "plugin_id=test.healthy" not in caplog.text

    await manager.stop()
    assert events == ["healthy.start", "healthy.stop"]


@pytest.mark.asyncio
async def test_explicit_dependency_failure_remains_strict(tmp_path) -> None:
    descriptor = _descriptor(
        plugin_id="test.missing",
        channel_name="missing",
        factory=lambda context: _TestInteractiveApp(
            channel_name="missing",
            context=context,
            events=[],
        ),
        dependency_report=InteractiveAppDependencyReport(
            available=False,
            missing=("missing-sdk",),
            installation_hint="Install missing-sdk.",
        ),
    )
    registry = InteractiveAppRegistry(
        allowed_optional_plugins={"test.missing": "test-interactive-app"}
    )
    registry.register_optional(
        descriptor,
        provenance=replace(_TEST_PROVENANCE, entry_point_name="test.missing"),
    )
    config = _config(tmp_path)
    _configure_optional(config, "test.missing")
    manager = InteractiveAppManager(
        config,
        application=object(),
        registry=registry,
        include_apps=["missing"],
    )

    with pytest.raises(RuntimeError, match="Install missing-sdk"):
        await manager.start()

    assert manager.running_app_names == []
    assert (
        manager.plugin_health_for_channel("missing").state
        == InteractiveAppLifecycleState.FAILED
    )
