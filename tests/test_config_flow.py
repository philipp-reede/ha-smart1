from __future__ import annotations

import asyncio
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).parents[1]
DOMAIN = "smart1_ems"


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class _AbortFlow(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _Required:
    def __init__(self, key: str, default=None) -> None:
        self.key = key
        self.default = default

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Required) and self.key == other.key


class _Schema:
    def __init__(self, schema) -> None:
        self.schema = schema


class _In:
    def __init__(self, container) -> None:
        self.container = container


class _TextSelectorConfig:
    def __init__(self, **kwargs) -> None:
        self.options = kwargs


class _TextSelector:
    def __init__(self, config) -> None:
        self.config = config


class _SelectSelectorConfig:
    def __init__(self, **kwargs) -> None:
        self.options = kwargs


class _SelectSelector:
    def __init__(self, config) -> None:
        self.config = config


class _TextSelectorType:
    PASSWORD = "password"


class _SelectSelectorMode:
    DROPDOWN = "dropdown"


class _ConfigEntry:
    def __init__(
        self,
        *,
        entry_id: str,
        data: dict,
        title: str = "smart1 EMS",
        options: dict | None = None,
        unique_id: str | None = None,
        version: int = 1,
        minor_version: int = 1,
        disabled_by: str | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.data = dict(data)
        self.title = title
        self.options = dict(options or {})
        self.unique_id = unique_id
        self.version = version
        self.minor_version = minor_version
        self.disabled_by = disabled_by


class _ConfigEntriesManager:
    def __init__(self, entries: list[_ConfigEntry] | None = None) -> None:
        self.entries = list(entries or [])
        self.update_calls: list[tuple[_ConfigEntry, dict]] = []

    def async_entries(self, domain: str | None = None) -> list[_ConfigEntry]:
        return list(self.entries)

    def async_get_entry(self, entry_id: str) -> _ConfigEntry | None:
        return next(
            (entry for entry in self.entries if entry.entry_id == entry_id),
            None,
        )

    def async_update_entry(self, entry: _ConfigEntry, **changes) -> None:
        self.update_calls.append((entry, dict(changes)))
        for key, value in changes.items():
            if key in {"data", "options"}:
                value = dict(value)
            setattr(entry, key, value)


class _ConfigFlow:
    def __init_subclass__(cls, *, domain=None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        cls.DOMAIN = domain

    async def async_set_unique_id(
        self,
        unique_id: str,
        *,
        raise_on_progress: bool = True,
    ) -> None:
        self.unique_id = unique_id

    def _async_current_entries(self, include_ignore: bool = True):
        return self.hass.config_entries.async_entries(self.DOMAIN)

    def _async_abort_entries_match(self, match_dict: dict) -> None:
        for entry in self._async_current_entries():
            if all(entry.data.get(key) == value for key, value in match_dict.items()):
                raise _AbortFlow("already_configured")

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        for entry in self._async_current_entries():
            if entry.unique_id != getattr(self, "unique_id", None):
                continue
            if updates:
                data = dict(entry.data)
                data.update(updates)
                self.hass.config_entries.async_update_entry(entry, data=data)
            raise _AbortFlow("already_configured")

    def _get_reauth_entry(self) -> _ConfigEntry:
        entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        assert entry is not None
        return entry

    def _abort_if_unique_id_mismatch(
        self,
        *,
        reason: str = "unique_id_mismatch",
    ) -> None:
        if self._get_reauth_entry().unique_id != getattr(
            self,
            "unique_id",
            None,
        ):
            raise _AbortFlow(reason)

    def async_update_reload_and_abort(
        self,
        entry: _ConfigEntry,
        *,
        data_updates: dict | None = None,
        reason: str = "reauth_successful",
        **kwargs,
    ) -> dict:
        if data_updates:
            data = dict(entry.data)
            data.update(data_updates)
            self.hass.config_entries.async_update_entry(entry, data=data)
        return {
            "type": "abort",
            "reason": reason,
            "reload": True,
        }

    def async_show_form(
        self,
        *,
        step_id: str,
        data_schema,
        errors: dict,
        **kwargs,
    ) -> dict:
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
        }

    def async_create_entry(self, *, title: str, data: dict) -> dict:
        return {
            "type": "create_entry",
            "title": title,
            "data": data,
            "unique_id": getattr(self, "unique_id", None),
        }

    def async_abort(self, *, reason: str) -> dict:
        return {"type": "abort", "reason": reason}


class _OptionsFlowWithReload:
    def async_show_form(
        self,
        *,
        step_id: str,
        data_schema,
        errors: dict,
        **kwargs,
    ) -> dict:
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
        }

    def async_create_entry(self, *, data: dict, **kwargs) -> dict:
        return {"type": "create_entry", "data": data}

    def async_abort(self, *, reason: str) -> dict:
        return {"type": "abort", "reason": reason}


class _ClientError(Exception):
    pass


class _Smart1ApiError(Exception):
    def __init__(self, code: object) -> None:
        self.code = str(code).strip()
        super().__init__(f"smart1 API error {self.code}")


class _FakeApi:
    plants_by_key: dict[str, list[dict[str, str]]] = {}
    errors_by_key: dict[str, BaseException] = {}
    calls: list[str] = []

    def __init__(self, session, api_key: str, device_id: str) -> None:
        self.api_key = api_key

    async def get_plants(self) -> list[dict[str, str]]:
        type(self).calls.append(self.api_key)
        if error := type(self).errors_by_key.get(self.api_key):
            raise error
        return [dict(plant) for plant in type(self).plants_by_key.get(
            self.api_key,
            [],
        )]


class _Role:
    def __init__(self, key: str) -> None:
        self.key = key


ENERGY_ROLES = (
    _Role("grid_import"),
    _Role("grid_export"),
    _Role("battery_charge"),
)


def _energy_candidates(points, role):
    return [point for point in points if role.key in point.roles]


def _recommend_energy_roles(points):
    return {
        role.key: candidates[0].id
        for role in ENERGY_ROLES
        if (candidates := _energy_candidates(points, role))
    }


def _base_modules() -> dict[str, types.ModuleType]:
    homeassistant = _module("homeassistant")
    homeassistant.__path__ = []
    config_entries = _module(
        "homeassistant.config_entries",
        ConfigEntry=_ConfigEntry,
        ConfigFlow=_ConfigFlow,
        ConfigFlowResult=dict,
        OptionsFlowWithReload=_OptionsFlowWithReload,
    )
    homeassistant.config_entries = config_entries
    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []
    custom_components = _module("custom_components")
    custom_components.__path__ = [str(ROOT / "custom_components")]
    smart1_package = _module("custom_components.smart1_ems")
    smart1_package.__path__ = [
        str(ROOT / "custom_components" / "smart1_ems")
    ]

    selector = _module(
        "homeassistant.helpers.selector",
        SelectOptionDict=lambda **kwargs: dict(kwargs),
        SelectSelector=_SelectSelector,
        SelectSelectorConfig=_SelectSelectorConfig,
        SelectSelectorMode=_SelectSelectorMode,
        TextSelector=_TextSelector,
        TextSelectorConfig=_TextSelectorConfig,
        TextSelectorType=_TextSelectorType,
    )
    voluptuous = _module(
        "voluptuous",
        Schema=_Schema,
        Required=_Required,
        In=_In,
    )

    return {
        "aiohttp": _module("aiohttp", ClientError=_ClientError),
        "voluptuous": voluptuous,
        "homeassistant": homeassistant,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": _module(
            "homeassistant.core",
            HomeAssistant=object,
            callback=lambda function: function,
        ),
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.aiohttp_client": _module(
            "homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: object(),
        ),
        "homeassistant.helpers.selector": selector,
        "custom_components": custom_components,
        "custom_components.smart1_ems": smart1_package,
        "custom_components.smart1_ems.api": _module(
            "custom_components.smart1_ems.api",
            Smart1Api=_FakeApi,
            Smart1ApiError=_Smart1ApiError,
            is_auth_error=lambda error: (
                isinstance(error, _Smart1ApiError)
                and error.code in {"401", "403"}
            ),
        ),
        "custom_components.smart1_ems.const": _module(
            "custom_components.smart1_ems.const",
            DOMAIN=DOMAIN,
        ),
        "custom_components.smart1_ems.energy_roles": _module(
            "custom_components.smart1_ems.energy_roles",
            ENERGY_ROLES=ENERGY_ROLES,
            energy_candidates=_energy_candidates,
            recommend_energy_roles=_recommend_energy_roles,
        ),
    }


def _load_config_flow():
    module_name = "custom_components.smart1_ems.config_flow_under_test"
    loader = SourceFileLoader(
        module_name,
        str(ROOT / "custom_components" / "smart1_ems" / "config_flow.py"),
    )
    spec = spec_from_loader(module_name, loader, is_package=False)
    assert spec is not None
    module = module_from_spec(spec)
    with patch.dict(sys.modules, _base_modules()):
        loader.exec_module(module)
    return module


async def _run_step(awaitable) -> dict:
    try:
        return await awaitable
    except _AbortFlow as err:
        return {"type": "abort", "reason": err.reason}


def _new_flow(module, entries=None, *, context=None):
    flow = module.Smart1ConfigFlow()
    flow.hass = types.SimpleNamespace(
        config_entries=_ConfigEntriesManager(entries),
    )
    flow.context = dict(context or {})
    flow.source = flow.context.get("source", "user")
    flow.unique_id = None
    return flow


class Smart1ConfigFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        _FakeApi.plants_by_key = {}
        _FakeApi.errors_by_key = {}
        _FakeApi.calls = []
        self.module = _load_config_flow()

    def test_single_plant_creates_entry_with_stable_unique_id(self) -> None:
        _FakeApi.plants_by_key["new-key"] = [
            {"DeviceId": " plant-1 ", "DeviceName": "Home"},
        ]
        flow = _new_flow(self.module)

        result = asyncio.run(
            _run_step(flow.async_step_user({"api_key": "new-key"}))
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "Home")
        self.assertEqual(
            result["data"],
            {
                "api_key": "new-key",
                "device_id": "plant-1",
                "statistics_namespace": "8aac131a20",
            },
        )
        self.assertEqual(result["unique_id"], "plant-1")

    def test_multiple_plants_show_selection_and_create_selected_entry(self) -> None:
        _FakeApi.plants_by_key["multi-key"] = [
            {"DeviceId": "plant-1", "DeviceName": "Home"},
            {"DeviceId": "plant-2", "DeviceName": "Workshop"},
        ]
        flow = _new_flow(self.module)

        first_result = asyncio.run(
            _run_step(flow.async_step_user({"api_key": "multi-key"}))
        )
        selected_result = asyncio.run(
            _run_step(flow.async_step_plant({"device_id": "plant-2"}))
        )

        self.assertEqual(first_result["type"], "form")
        self.assertEqual(first_result["step_id"], "plant")
        self.assertEqual(selected_result["type"], "create_entry")
        self.assertEqual(selected_result["title"], "Workshop")
        self.assertEqual(selected_result["data"]["device_id"], "plant-2")
        self.assertEqual(selected_result["unique_id"], "plant-2")

    def test_multiple_plants_allow_unconfigured_sibling(self) -> None:
        existing = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "old-key", "device_id": "plant-1"},
            unique_id="plant-1",
        )
        _FakeApi.plants_by_key["multi-key"] = [
            {"DeviceId": "plant-1", "DeviceName": "Home"},
            {"DeviceId": "plant-2", "DeviceName": "Workshop"},
        ]
        flow = _new_flow(self.module, [existing])

        asyncio.run(
            _run_step(flow.async_step_user({"api_key": "multi-key"}))
        )
        result = asyncio.run(
            _run_step(flow.async_step_plant({"device_id": "plant-2"}))
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["unique_id"], "plant-2")

    def test_user_connection_error_is_reported_without_secret(self) -> None:
        secret = "secret-that-must-not-leak"
        _FakeApi.errors_by_key[secret] = _ClientError(
            f"https://example.test/?apikey={secret}"
        )
        flow = _new_flow(self.module)

        result = asyncio.run(
            _run_step(flow.async_step_user({"api_key": secret}))
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertNotIn(secret, repr(result))

    def test_non_auth_api_error_is_reported_as_connection_error(self) -> None:
        _FakeApi.errors_by_key["server-error-key"] = _Smart1ApiError("500")
        flow = _new_flow(self.module)

        result = asyncio.run(
            _run_step(
                flow.async_step_user({"api_key": "server-error-key"})
            )
        )

        self.assertEqual(result["errors"], {"base": "cannot_connect"})

    def test_user_auth_errors_are_reported_as_invalid_auth(self) -> None:
        for code in ("401", "403"):
            with self.subTest(code=code):
                _FakeApi.errors_by_key = {
                    f"key-{code}": _Smart1ApiError(code),
                }
                flow = _new_flow(self.module)

                result = asyncio.run(
                    _run_step(
                        flow.async_step_user({"api_key": f"key-{code}"})
                    )
                )

                self.assertEqual(
                    result["errors"],
                    {"base": "invalid_auth"},
                )

    def test_user_no_plants_is_reported(self) -> None:
        _FakeApi.plants_by_key["empty-key"] = []
        flow = _new_flow(self.module)

        result = asyncio.run(
            _run_step(flow.async_step_user({"api_key": "empty-key"}))
        )

        self.assertEqual(result["errors"], {"base": "no_plants"})

    def test_user_form_masks_api_key(self) -> None:
        flow = _new_flow(self.module)

        result = asyncio.run(_run_step(flow.async_step_user()))

        schema = result["data_schema"].schema
        selector = next(iter(schema.values()))
        self.assertEqual(selector.config.options["type"], "password")
        self.assertEqual(
            selector.config.options["autocomplete"],
            "current-password",
        )

    def test_modern_duplicate_aborts_without_updating_api_key(self) -> None:
        existing = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "old-key", "device_id": "plant-1"},
            unique_id="plant-1",
        )
        _FakeApi.plants_by_key["new-key"] = [
            {"DeviceId": "plant-1", "DeviceName": "Home"},
        ]
        flow = _new_flow(self.module, [existing])

        result = asyncio.run(
            _run_step(flow.async_step_user({"api_key": "new-key"}))
        )

        self.assertEqual(
            result,
            {"type": "abort", "reason": "already_configured"},
        )
        self.assertEqual(existing.data["api_key"], "old-key")
        self.assertEqual(flow.hass.config_entries.update_calls, [])

    def test_legacy_duplicate_device_id_aborts(self) -> None:
        existing = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "old-key", "device_id": " plant-1 "},
            unique_id=None,
        )
        _FakeApi.plants_by_key["new-key"] = [
            {"DeviceId": "plant-1", "DeviceName": "Home"},
        ]
        flow = _new_flow(self.module, [existing])

        result = asyncio.run(
            _run_step(flow.async_step_user({"api_key": "new-key"}))
        )

        self.assertEqual(
            result,
            {"type": "abort", "reason": "already_configured"},
        )
        self.assertEqual(existing.data["api_key"], "old-key")

    def test_reauth_updates_only_api_key_for_same_plant(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-1",
            title="Home",
            data={
                "api_key": "old-key",
                "device_id": "plant-1",
                "preserved": "value",
            },
            options={"energy_roles": {"grid_import": "point-1"}},
            unique_id="plant-1",
        )
        _FakeApi.plants_by_key["new-key"] = [
            {"DeviceId": "plant-2", "DeviceName": "Workshop"},
            {"DeviceId": "plant-1", "DeviceName": "Home"},
        ]
        flow = _new_flow(
            self.module,
            [entry],
            context={"source": "reauth", "entry_id": "entry-1"},
        )

        initial = asyncio.run(
            _run_step(flow.async_step_reauth(dict(entry.data)))
        )
        result = asyncio.run(
            _run_step(
                flow.async_step_reauth_confirm({"api_key": "new-key"})
            )
        )

        self.assertEqual(initial["type"], "form")
        self.assertEqual(initial["step_id"], "reauth_confirm")
        self.assertEqual(
            result,
            {
                "type": "abort",
                "reason": "reauth_successful",
                "reload": True,
            },
        )
        self.assertEqual(
            entry.data,
            {
                "api_key": "new-key",
                "device_id": "plant-1",
                "preserved": "value",
            },
        )
        self.assertEqual(entry.title, "Home")
        self.assertEqual(
            entry.options,
            {"energy_roles": {"grid_import": "point-1"}},
        )
        self.assertEqual(entry.unique_id, "plant-1")

    def test_reauth_errors_leave_entry_unchanged(self) -> None:
        cases = (
            (_ClientError("offline"), "cannot_connect"),
            (_Smart1ApiError("401"), "invalid_auth"),
        )
        for error, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                entry = _ConfigEntry(
                    entry_id="entry-1",
                    data={"api_key": "old-key", "device_id": "plant-1"},
                    options={"preserved": True},
                    unique_id="plant-1",
                )
                _FakeApi.errors_by_key = {"new-key": error}
                flow = _new_flow(
                    self.module,
                    [entry],
                    context={"source": "reauth", "entry_id": "entry-1"},
                )
                asyncio.run(
                    _run_step(flow.async_step_reauth(dict(entry.data)))
                )

                result = asyncio.run(
                    _run_step(
                        flow.async_step_reauth_confirm(
                            {"api_key": "new-key"}
                        )
                    )
                )

                self.assertEqual(
                    result["errors"],
                    {"base": expected_error},
                )
                self.assertEqual(
                    entry.data,
                    {"api_key": "old-key", "device_id": "plant-1"},
                )
                self.assertEqual(entry.options, {"preserved": True})
                self.assertEqual(flow.hass.config_entries.update_calls, [])

    def test_reauth_for_different_account_is_rejected(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "old-key", "device_id": "plant-1"},
            unique_id="plant-1",
        )
        _FakeApi.plants_by_key["wrong-key"] = [
            {"DeviceId": "plant-2", "DeviceName": "Other"},
        ]
        flow = _new_flow(
            self.module,
            [entry],
            context={"source": "reauth", "entry_id": "entry-1"},
        )
        asyncio.run(_run_step(flow.async_step_reauth(dict(entry.data))))

        result = asyncio.run(
            _run_step(
                flow.async_step_reauth_confirm({"api_key": "wrong-key"})
            )
        )

        self.assertEqual(result["errors"], {"base": "wrong_account"})
        self.assertEqual(
            entry.data,
            {"api_key": "old-key", "device_id": "plant-1"},
        )
        self.assertEqual(flow.hass.config_entries.update_calls, [])


class Smart1OptionsFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_config_flow()
        self.points = [
            types.SimpleNamespace(
                id="point-import",
                name="Grid import",
                hardware="grid",
                roles={"grid_import"},
            ),
            types.SimpleNamespace(
                id="point-export",
                name="Grid export",
                hardware="grid",
                roles={"grid_export"},
            ),
            types.SimpleNamespace(
                id="point-battery",
                name="Battery charge",
                hardware="battery",
                roles={"battery_charge"},
            ),
        ]

    def _flow(self, options: dict | None = None):
        entry = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key", "device_id": "plant-1"},
            options=options,
        )
        flow = self.module.Smart1OptionsFlow()
        flow.config_entry = entry
        flow.hass = types.SimpleNamespace(
            data={DOMAIN: {"entry-1": {"devices": self.points}}},
        )
        return flow

    def test_duplicate_energy_point_is_rejected(self) -> None:
        flow = self._flow()

        result = asyncio.run(
            flow.async_step_init({
                "grid_import": "point-import",
                "grid_export": "point-import",
                "battery_charge": "",
            })
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"],
            {"base": "duplicate_energy_point"},
        )

    def test_unloaded_entry_aborts_without_runtime_data(self) -> None:
        for hass_data in ({}, {DOMAIN: {}}):
            with self.subTest(hass_data=hass_data):
                flow = self._flow()
                flow.hass.data = hass_data

                result = asyncio.run(flow.async_step_init())

                self.assertEqual(
                    result,
                    {"type": "abort", "reason": "not_loaded"},
                )

    def test_distinct_energy_points_preserve_unrelated_options(self) -> None:
        flow = self._flow({
            "active_linear_ids": ["point-import"],
            "energy_roles": {"grid_import": "old-point"},
        })

        result = asyncio.run(
            flow.async_step_init({
                "grid_import": "point-import",
                "grid_export": "point-export",
                "battery_charge": "",
            })
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {
                "active_linear_ids": ["point-import"],
                "energy_roles": {
                    "grid_import": "point-import",
                    "grid_export": "point-export",
                },
            },
        )


class Smart1ConfigEntryMigrationTest(unittest.TestCase):
    def _load_integration(self):
        modules = _base_modules()

        class ConfigEntryAuthFailed(Exception):
            pass

        class ConfigEntryNotReady(Exception):
            pass

        modules.update({
            "homeassistant.exceptions": _module(
                "homeassistant.exceptions",
                ConfigEntryAuthFailed=ConfigEntryAuthFailed,
                ConfigEntryNotReady=ConfigEntryNotReady,
            ),
            "homeassistant.helpers.event": _module(
                "homeassistant.helpers.event",
                async_track_time_interval=Mock(),
            ),
            "custom_components.smart1_ems.coordinator": _module(
                "custom_components.smart1_ems.coordinator",
                Smart1Coordinator=object,
            ),
            "custom_components.smart1_ems.derived_history": _module(
                "custom_components.smart1_ems.derived_history",
                Smart1DerivedEnergyImporter=object,
            ),
            "custom_components.smart1_ems.discovery": _module(
                "custom_components.smart1_ems.discovery",
                Smart1Discovery=object,
            ),
            "custom_components.smart1_ems.history": _module(
                "custom_components.smart1_ems.history",
                Smart1PvHistoryImporter=object,
            ),
        })
        api_module = modules["custom_components.smart1_ems.api"]
        api_module.describe_api_error = lambda error: type(error).__name__
        api_module.sanitize_api_error_code = lambda code: str(code)
        energy_module = modules["custom_components.smart1_ems.energy_roles"]
        energy_module.ENERGY_ROLES_BY_KEY = {}

        module_name = "custom_components.smart1_ems.entrypoint_migration_test"
        loader = SourceFileLoader(
            module_name,
            str(ROOT / "custom_components" / "smart1_ems" / "__init__.py"),
        )
        spec = spec_from_loader(module_name, loader, is_package=False)
        assert spec is not None
        module = module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            loader.exec_module(module)
        return module

    def _migrate(self, entry, others=None):
        integration = self._load_integration()
        manager = _ConfigEntriesManager([entry, *(others or [])])
        hass = types.SimpleNamespace(config_entries=manager)
        result = asyncio.run(integration.async_migrate_entry(hass, entry))
        return result, manager

    def test_migration_adds_unique_id_and_minor_version(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-1",
            title="Home",
            data={
                "api_key": "preserved-key",
                "device_id": " plant-1 ",
            },
            options={"energy_roles": {"grid_import": "point-1"}},
            unique_id=None,
            version=1,
            minor_version=1,
        )

        result, manager = self._migrate(entry)

        self.assertTrue(result)
        self.assertEqual(entry.unique_id, "plant-1")
        self.assertEqual(entry.version, 1)
        self.assertEqual(entry.minor_version, 3)
        self.assertEqual(
            entry.data,
            {
                "api_key": "preserved-key",
                "device_id": "plant-1",
                "statistics_namespace": "",
            },
        )
        self.assertEqual(
            entry.options,
            {"energy_roles": {"grid_import": "point-1"}},
        )
        self.assertEqual(len(manager.update_calls), 1)

    def test_migration_is_idempotent_for_current_entry(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-1",
            data={
                "api_key": "key",
                "device_id": "plant-1",
                "statistics_namespace": "",
            },
            unique_id="plant-1",
            version=1,
            minor_version=3,
        )

        result, manager = self._migrate(entry)

        self.assertTrue(result)
        self.assertEqual(manager.update_calls, [])

    def test_migration_rejects_missing_device_id(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key"},
            unique_id=None,
            version=1,
            minor_version=1,
        )

        result, manager = self._migrate(entry)

        self.assertFalse(result)
        self.assertEqual(manager.update_calls, [])

    def test_migration_rejects_mismatched_existing_unique_id(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key", "device_id": "plant-1"},
            unique_id="plant-other",
            version=1,
            minor_version=1,
        )

        result, manager = self._migrate(entry)

        self.assertFalse(result)
        self.assertEqual(manager.update_calls, [])

    def test_migration_rejects_legacy_duplicate_without_mutation(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-2",
            data={"api_key": "key-2", "device_id": "plant-1"},
            unique_id=None,
            version=1,
            minor_version=1,
        )
        duplicate = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key-1", "device_id": "plant-1"},
            unique_id=None,
            version=1,
            minor_version=1,
        )

        result, manager = self._migrate(entry, [duplicate])

        self.assertFalse(result)
        self.assertIsNone(entry.unique_id)
        self.assertEqual(entry.minor_version, 1)
        self.assertEqual(manager.update_calls, [])

    def test_migration_allows_deterministic_legacy_duplicate_winner(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key-1", "device_id": "plant-1"},
            unique_id=None,
            version=1,
            minor_version=1,
        )
        duplicate = _ConfigEntry(
            entry_id="entry-2",
            data={"api_key": "key-2", "device_id": "plant-1"},
            unique_id=None,
            version=1,
            minor_version=1,
        )

        result, manager = self._migrate(entry, [duplicate])

        self.assertTrue(result)
        self.assertEqual(entry.unique_id, "plant-1")
        self.assertEqual(entry.minor_version, 3)
        self.assertEqual(len(manager.update_calls), 1)

    def test_migration_prefers_existing_unique_id_owner(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-2",
            data={"api_key": "key-2", "device_id": "plant-1"},
            unique_id="plant-1",
            version=1,
            minor_version=1,
        )
        legacy_duplicate = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key-1", "device_id": "plant-1"},
            unique_id=None,
            version=1,
            minor_version=1,
        )

        result, manager = self._migrate(entry, [legacy_duplicate])

        self.assertTrue(result)
        self.assertEqual(entry.unique_id, "plant-1")
        self.assertEqual(entry.minor_version, 3)
        self.assertEqual(len(manager.update_calls), 1)

    def test_migration_prefers_active_entry_over_disabled_duplicate(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-2",
            data={"api_key": "key-2", "device_id": "plant-1"},
            unique_id=None,
            version=1,
            minor_version=1,
        )
        disabled_duplicate = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key-1", "device_id": "plant-1"},
            unique_id=None,
            version=1,
            minor_version=1,
            disabled_by="user",
        )

        result, manager = self._migrate(entry, [disabled_duplicate])

        self.assertTrue(result)
        self.assertEqual(entry.unique_id, "plant-1")
        self.assertEqual(entry.minor_version, 3)
        self.assertEqual(len(manager.update_calls), 1)

    def test_migration_scopes_additional_legacy_installation(self) -> None:
        entry = _ConfigEntry(
            entry_id="entry-2",
            data={"api_key": "key-2", "device_id": "plant-2"},
            unique_id="plant-2",
            version=1,
            minor_version=2,
        )
        legacy_owner = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key-1", "device_id": "plant-1"},
            unique_id="plant-1",
            version=1,
            minor_version=2,
        )

        result, manager = self._migrate(entry, [legacy_owner])

        self.assertTrue(result)
        self.assertEqual(entry.minor_version, 3)
        self.assertEqual(
            entry.data["statistics_namespace"],
            "138a5dd174",
        )
        self.assertIs(legacy_owner.data["legacy_history_rebuild"], True)
        self.assertEqual(len(manager.update_calls), 2)

    def test_multi_install_migration_flags_legacy_owner_owner_first(self) -> None:
        integration = self._load_integration()
        owner = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key-1", "device_id": "plant-1"},
            unique_id="plant-1",
            version=1,
            minor_version=2,
        )
        sibling = _ConfigEntry(
            entry_id="entry-2",
            data={"api_key": "key-2", "device_id": "plant-2"},
            unique_id="plant-2",
            version=1,
            minor_version=2,
        )
        manager = _ConfigEntriesManager([owner, sibling])
        hass = types.SimpleNamespace(config_entries=manager)

        self.assertTrue(
            asyncio.run(integration.async_migrate_entry(hass, owner))
        )
        self.assertTrue(
            asyncio.run(integration.async_migrate_entry(hass, sibling))
        )

        self.assertEqual(owner.data["statistics_namespace"], "")
        self.assertIs(owner.data["legacy_history_rebuild"], True)
        self.assertNotIn("legacy_history_rebuild", sibling.data)
        self.assertNotEqual(sibling.data["statistics_namespace"], "")

    def test_multi_install_migration_flags_legacy_owner_owner_last(self) -> None:
        integration = self._load_integration()
        owner = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key-1", "device_id": "plant-1"},
            unique_id="plant-1",
            version=1,
            minor_version=2,
        )
        sibling = _ConfigEntry(
            entry_id="entry-2",
            data={"api_key": "key-2", "device_id": "plant-2"},
            unique_id="plant-2",
            version=1,
            minor_version=2,
        )
        manager = _ConfigEntriesManager([owner, sibling])
        hass = types.SimpleNamespace(config_entries=manager)

        self.assertTrue(
            asyncio.run(integration.async_migrate_entry(hass, sibling))
        )
        self.assertIs(owner.data["legacy_history_rebuild"], True)
        self.assertTrue(
            asyncio.run(integration.async_migrate_entry(hass, owner))
        )

        self.assertEqual(owner.data["statistics_namespace"], "")
        self.assertIs(owner.data["legacy_history_rebuild"], True)
        self.assertNotIn("legacy_history_rebuild", sibling.data)
        self.assertNotEqual(sibling.data["statistics_namespace"], "")

    def test_modern_scoped_entry_does_not_force_legacy_owner_rebuild(
        self,
    ) -> None:
        integration = self._load_integration()
        owner = _ConfigEntry(
            entry_id="entry-1",
            data={"api_key": "key-1", "device_id": "plant-1"},
            unique_id="plant-1",
            version=1,
            minor_version=2,
        )
        modern_sibling = _ConfigEntry(
            entry_id="entry-2",
            data={
                "api_key": "key-2",
                "device_id": "plant-2",
                "statistics_namespace": "138a5dd174",
            },
            unique_id="plant-2",
            version=1,
            minor_version=3,
        )
        manager = _ConfigEntriesManager([owner, modern_sibling])
        hass = types.SimpleNamespace(config_entries=manager)

        self.assertTrue(
            asyncio.run(integration.async_migrate_entry(hass, owner))
        )

        self.assertEqual(owner.data["statistics_namespace"], "")
        self.assertNotIn("legacy_history_rebuild", owner.data)
        self.assertNotIn("legacy_history_rebuild", modern_sibling.data)


if __name__ == "__main__":
    unittest.main()
