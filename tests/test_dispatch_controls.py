"""Offline regressions for the real switch/button modules.

Run: python -m unittest discover -s tests -p test_dispatch_controls.py -v

The existing CI suite uses installed Home Assistant. Without HA, small import/
entity stubs allow standalone unit execution. Both use an in-memory coordinator;
neither validates setup, transport, firmware, or every concurrency schedule.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class EntityStub:
    def async_write_ha_state(self):
        pass


class RestoreStub:
    pass


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    return result


PACKAGE = "_alphaess_control_tests"
SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / "alphaess_modbus"
stubs = {
    "homeassistant": module("homeassistant"),
    "homeassistant.components": module("homeassistant.components"),
    "homeassistant.components.switch": module("homeassistant.components.switch", SwitchEntity=EntityStub),
    "homeassistant.components.button": module("homeassistant.components.button", ButtonEntity=EntityStub),
    "homeassistant.config_entries": module("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": module("homeassistant.core", HomeAssistant=object),
    "homeassistant.helpers": module("homeassistant.helpers"),
    "homeassistant.helpers.entity": module("homeassistant.helpers.entity", DeviceInfo=dict),
    "homeassistant.helpers.entity_platform": module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object),
    "homeassistant.helpers.restore_state": module("homeassistant.helpers.restore_state", RestoreEntity=RestoreStub),
    PACKAGE: module(PACKAGE, __path__=[str(SOURCE)]),
    PACKAGE + ".coordinator": module(PACKAGE + ".coordinator", AlphaESSCoordinator=object),
}
try:
    import homeassistant  # noqa: F401
except ModuleNotFoundError:
    imports = patch.dict(sys.modules, stubs)
    imports.start()
    switch_module = importlib.import_module(PACKAGE + ".switch")
    button_module = importlib.import_module(PACKAGE + ".button")
else:
    imports = None
    switch_module = importlib.import_module("custom_components.alphaess_modbus.switch")
    button_module = importlib.import_module("custom_components.alphaess_modbus.button")
DOMAIN = switch_module.DOMAIN
LEGACY_MODES = [key for key in switch_module._MUTEX_SWITCHES if key != "peak_shaving"]


def tearDownModule():
    if imports:
        imports.stop()


class MemoryCoordinator:
    def __init__(self):
        self.data = dict(pv1_power=0, pv2_power=0, pv3_power=0, pv4_power=0,
                         active_power_pv_meter=0, power_battery=0,
                         power_grid=1000, inverter_work_mode=1, soc_battery=50)
        self.ac_limit_w = 5000
        self.numbers = {}
        self.listeners = []
        self.writes = []
        self.active_dispatch_key = None
        self.ee_paused = False
        self.fi_paused = False
        self.reset_error = None
        self.on_reset = None

    def get_number(self, key):
        return self.numbers.get(key)

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def async_write_dispatch(self, values, *, reset_timer=True):
        self.writes.append((list(values), reset_timer))

    async def async_reset_dispatch(self):
        if self.on_reset:
            self.on_reset()
        if self.reset_error:
            raise self.reset_error
        self.writes.append(([0], True))

    def notify(self):
        for listener in list(self.listeners):
            listener()


class DispatchControlsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = MemoryCoordinator()
        self.entry = types.SimpleNamespace(entry_id="test_inverter")
        self.switches = {}
        self.hass = types.SimpleNamespace(
            loop=asyncio.get_running_loop(),
            data={DOMAIN: {"test_inverter_switches": self.switches}},
            async_create_task=lambda coro, **kw: asyncio.create_task(coro),
        )
        self.reset = button_module.AlphaESSButton(
            self.coordinator, self.entry,
            {"key": "dispatch_reset", "name": "Reset", "icon": "test"},
        )
        self.reset.hass = self.hass

    def make_switch(self, key):
        sw = switch_module.AlphaESSSwitch(
            self.coordinator, self.entry,
            {"key": key, "name": key, "icon": "test"},
        )
        sw.hass = self.hass
        sw.async_write_ha_state = Mock()
        self.switches[key] = sw
        return sw

    async def asyncTearDown(self):
        for sw in self.switches.values():
            await sw.async_will_remove_from_hass()

    async def drain(self, sw):
        if sw._pending_tasks:
            await asyncio.gather(*list(sw._pending_tasks))

    async def test_reset_stops_export_watcher_and_does_not_restart(self):
        sw = self.make_switch("force_export")
        sw._is_on = True
        self.coordinator.active_dispatch_key = sw.switch_key
        await sw._start_force_export(7200)
        self.assertTrue(self.coordinator.listeners)
        await self.reset.async_press()
        self.coordinator.notify()
        await self.drain(sw)
        self.assertFalse(sw.is_on)
        self.assertFalse(self.coordinator.listeners)
        self.assertIsNone(self.coordinator.active_dispatch_key)
        self.assertEqual([w[0][0] for w in self.coordinator.writes], [1, 0])

    async def test_reset_stops_import_watcher_and_does_not_restart(self):
        sw = self.make_switch("force_import")
        sw._is_on = True
        await sw._start_force_import(7200)
        await self.reset.async_press()
        self.coordinator.notify()
        await self.drain(sw)
        self.assertFalse(sw.is_on)
        self.assertFalse(self.coordinator.listeners)
        self.assertEqual([w[0][0] for w in self.coordinator.writes], [1, 0])

    async def test_reset_clears_every_mode_and_timer_but_keeps_preferences(self):
        handles = []
        for key in LEGACY_MODES:
            sw = self.make_switch(key)
            sw._is_on = True
            sw._timer_cancel = self.hass.loop.call_later(3600, lambda: None)
            sw._duration_cancel = self.hass.loop.call_later(3600, lambda: None)
            handles.extend([sw._timer_cancel, sw._duration_cancel])
        for key in (*switch_module._HOLD_SWITCHES, "dispatch_pv"):
            self.make_switch(key)._is_on = True
        other = types.SimpleNamespace(is_on=True)
        self.hass.data[DOMAIN]["other_inverter_switches"] = {"dispatch": other}
        self.coordinator.ee_paused = self.coordinator.fi_paused = True
        await self.reset.async_press()
        self.assertTrue(all(not self.switches[k].is_on for k in LEGACY_MODES))
        self.assertTrue(all(self.switches[k].is_on for k in (*switch_module._HOLD_SWITCHES, "dispatch_pv")))
        self.assertTrue(all(handle.cancelled() for handle in handles))
        self.assertTrue(other.is_on)
        self.assertFalse(self.coordinator.ee_paused)
        self.assertFalse(self.coordinator.fi_paused)
        self.assertEqual(self.coordinator.writes, [([0], True)])

    async def test_reset_drains_queued_write_even_if_switch_already_off(self):
        sw = self.make_switch("force_export")
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def queued_write():
            entered.set()
            try:
                await release.wait()
                await sw._write_export_dispatch(37000, 7200, reset_timer=False)
            finally:
                cancelled.set()

        task = asyncio.create_task(queued_write())
        sw._pending_tasks.add(task)
        task.add_done_callback(sw._pending_tasks.discard)
        await entered.wait()
        self.coordinator.on_reset = lambda: self.assertTrue(cancelled.is_set())
        await self.reset.async_press()
        release.set()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.coordinator.writes, [([0], True)])

    async def test_force_off_does_not_cancel_itself(self):
        sw = self.make_switch("force_export")
        task = asyncio.current_task()
        sw._pending_tasks.add(task)
        try:
            await sw.async_force_off()
            self.assertFalse(task.cancelling())
        finally:
            sw._pending_tasks.discard(task)

    async def test_reset_failure_propagates_with_controllers_stopped(self):
        sw = self.make_switch("force_export")
        sw._is_on = True
        await sw._start_force_export(7200)
        self.coordinator.reset_error = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await self.reset.async_press()
        self.assertFalse(sw.is_on)
        self.assertFalse(self.coordinator.listeners)

    async def test_initial_export_is_capped_at_selected_five_kw(self):
        sw = self.make_switch("force_export")
        sw._is_on = True
        await sw._start_force_export(7200)
        self.assertEqual(self.coordinator.writes[0][0][2], 37000)

    async def test_initial_import_includes_pv_but_is_capped(self):
        sw = self.make_switch("force_import")
        sw._is_on = True
        self.coordinator.data.update(pv1_power=8000, power_grid=-7000)
        await sw._start_force_import(7200)
        self.assertEqual(self.coordinator.writes[0][0][2], 27000)

    async def test_setpoint_change_keeps_timer_and_respects_limit(self):
        sw = self.make_switch("force_export")
        sw._is_on = True
        handle = self.hass.loop.call_later(3600, lambda: None)
        sw._duration_cancel = handle
        await sw.async_apply_param_change(reset_timer=False)
        self.assertEqual(self.coordinator.writes[-1], ([1, 0, 37000, 0, 32000, 2, int(4 / 0.392), 0, 7200, 255, 0], False))
        self.assertIs(sw._duration_cancel, handle)
        self.assertFalse(handle.cancelled())

    async def test_writer_rechecks_limit_after_it_is_lowered(self):
        sw = self.make_switch("force_export")
        seed = sw._compute_force_export_power_raw(self.coordinator.data)
        self.coordinator.ac_limit_w = 3000
        await sw._write_export_dispatch(seed, 60, reset_timer=False)
        self.assertEqual(self.coordinator.writes[-1][0][2], 35000)
        await sw._write_import_dispatch(27000, 60, reset_timer=False)
        self.assertEqual(self.coordinator.writes[-1][0][2], 29000)

    async def test_power_boundaries_do_not_wrap_or_reverse_direction(self):
        sw = self.make_switch("force_export")
        for limit in (0, 5000, 20000, 50000):
            for requested in (-2000, 0, 1000, 6000, 40000):
                with self.subTest(limit=limit, requested=requested):
                    self.coordinator.ac_limit_w = limit
                    expected = max(0, min(limit, requested, 32000))
                    await sw._write_export_dispatch(32000 + requested, 60, reset_timer=False)
                    export = self.coordinator.writes[-1][0][2]
                    await sw._write_import_dispatch(32000 - requested, 60, reset_timer=False)
                    imported = self.coordinator.writes[-1][0][2]
                    self.assertEqual(export, 32000 + expected)
                    self.assertEqual(imported, 32000 - expected)
                    self.assertTrue(0 <= export <= 65535)
                    self.assertTrue(0 <= imported <= 65535)

    async def test_missing_measurements_do_not_become_zero_load_seed(self):
        sw = self.make_switch("force_export")
        del self.coordinator.data["pv1_power"]
        self.assertIsNone(sw._compute_force_export_power_raw(self.coordinator.data))
        self.assertIsNone(sw._compute_force_import_power_raw(self.coordinator.data))


if __name__ == "__main__":
    unittest.main()
