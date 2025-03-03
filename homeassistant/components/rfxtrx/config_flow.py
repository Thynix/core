"""Config flow for RFXCOM RFXtrx integration."""
from __future__ import annotations

import copy
import itertools
import os
from typing import TypedDict, cast

import RFXtrx as rfxtrxmod
import serial
import serial.tools.list_ports
import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_COMMAND_OFF,
    CONF_COMMAND_ON,
    CONF_DEVICE,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_HOST,
    CONF_PORT,
    CONF_TYPE,
)
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import (
    DeviceEntry,
    DeviceRegistry,
    async_entries_for_config_entry,
    async_get_registry as async_get_device_registry,
)
from homeassistant.helpers.entity_registry import (
    async_entries_for_device,
    async_get_registry as async_get_entity_registry,
)

from . import (
    DOMAIN,
    DeviceTuple,
    get_device_id,
    get_device_tuple_from_identifiers,
    get_rfx_object,
)
from .binary_sensor import supported as binary_supported
from .const import (
    CONF_AUTOMATIC_ADD,
    CONF_DATA_BITS,
    CONF_OFF_DELAY,
    CONF_PROTOCOLS,
    CONF_REPLACE_DEVICE,
    CONF_SIGNAL_REPETITIONS,
    CONF_VENETIAN_BLIND_MODE,
    CONST_VENETIAN_BLIND_MODE_DEFAULT,
    CONST_VENETIAN_BLIND_MODE_EU,
    CONST_VENETIAN_BLIND_MODE_US,
    DEVICE_PACKET_TYPE_LIGHTING4,
)
from .cover import supported as cover_supported
from .light import supported as light_supported
from .switch import supported as switch_supported

CONF_EVENT_CODE = "event_code"
CONF_MANUAL_PATH = "Enter Manually"

RECV_MODES = sorted(itertools.chain(*rfxtrxmod.lowlevel.Status.RECMODES))


class DeviceData(TypedDict):
    """Dict data representing a device entry."""

    event_code: str | None
    device_id: DeviceTuple


def none_or_int(value, base):
    """Check if strin is one otherwise convert to int."""
    if value is None:
        return None
    return int(value, base)


class OptionsFlow(config_entries.OptionsFlow):
    """Handle Rfxtrx options."""

    _device_registry: DeviceRegistry
    _device_entries: list[DeviceEntry]

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize rfxtrx options flow."""
        self._config_entry = config_entry
        self._global_options = None
        self._selected_device = None
        self._selected_device_entry_id: str | None = None
        self._selected_device_event_code: str | None = None
        self._selected_device_object: rfxtrxmod.RFXtrxEvent | None = None

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        return await self.async_step_prompt_options()

    async def async_step_prompt_options(self, user_input=None):
        """Prompt for options."""
        errors = {}

        if user_input is not None:
            self._global_options = {
                CONF_AUTOMATIC_ADD: user_input[CONF_AUTOMATIC_ADD],
                CONF_PROTOCOLS: user_input[CONF_PROTOCOLS] or None,
            }
            if CONF_DEVICE in user_input:
                entry_id = user_input[CONF_DEVICE]
                device_data = self._get_device_data(entry_id)
                self._selected_device_entry_id = entry_id
                event_code = device_data[CONF_EVENT_CODE]
                self._selected_device_event_code = event_code
                self._selected_device = self._config_entry.data[CONF_DEVICES][
                    event_code
                ]
                self._selected_device_object = get_rfx_object(event_code)
                return await self.async_step_set_device_options()
            if CONF_EVENT_CODE in user_input:
                self._selected_device_event_code = user_input[CONF_EVENT_CODE]
                self._selected_device = {}
                selected_device_object = get_rfx_object(
                    self._selected_device_event_code
                )
                if selected_device_object is None:
                    errors[CONF_EVENT_CODE] = "invalid_event_code"
                elif not self._can_add_device(selected_device_object):
                    errors[CONF_EVENT_CODE] = "already_configured_device"
                else:
                    self._selected_device_object = selected_device_object
                    return await self.async_step_set_device_options()

            if not errors:
                self.update_config_data(global_options=self._global_options)

                return self.async_create_entry(title="", data={})

        device_registry = await async_get_device_registry(self.hass)
        device_entries = async_entries_for_config_entry(
            device_registry, self._config_entry.entry_id
        )
        self._device_registry = device_registry
        self._device_entries = device_entries

        configure_devices = {
            entry.id: entry.name_by_user if entry.name_by_user else entry.name
            for entry in device_entries
            if self._get_device_event_code(entry.id) is not None
        }

        options = {
            vol.Optional(
                CONF_AUTOMATIC_ADD,
                default=self._config_entry.data[CONF_AUTOMATIC_ADD],
            ): bool,
            vol.Optional(
                CONF_PROTOCOLS,
                default=self._config_entry.data.get(CONF_PROTOCOLS) or [],
            ): cv.multi_select(RECV_MODES),
            vol.Optional(CONF_EVENT_CODE): str,
            vol.Optional(CONF_DEVICE): vol.In(configure_devices),
        }

        return self.async_show_form(
            step_id="prompt_options", data_schema=vol.Schema(options), errors=errors
        )

    async def async_step_set_device_options(self, user_input=None):
        """Manage device options."""
        errors = {}

        if user_input is not None:
            assert self._selected_device_object
            assert self._selected_device_event_code
            device_id = get_device_id(
                self._selected_device_object.device,
                data_bits=user_input.get(CONF_DATA_BITS),
            )

            if CONF_REPLACE_DEVICE in user_input:
                await self._async_replace_device(user_input[CONF_REPLACE_DEVICE])

                devices = {self._selected_device_event_code: None}
                self.update_config_data(
                    global_options=self._global_options, devices=devices
                )

                return self.async_create_entry(title="", data={})

            try:
                command_on = none_or_int(user_input.get(CONF_COMMAND_ON), 16)
            except ValueError:
                errors[CONF_COMMAND_ON] = "invalid_input_2262_on"

            try:
                command_off = none_or_int(user_input.get(CONF_COMMAND_OFF), 16)
            except ValueError:
                errors[CONF_COMMAND_OFF] = "invalid_input_2262_off"

            try:
                off_delay = none_or_int(user_input.get(CONF_OFF_DELAY), 10)
            except ValueError:
                errors[CONF_OFF_DELAY] = "invalid_input_off_delay"

            if not errors:
                devices = {}
                device = {
                    CONF_DEVICE_ID: device_id,
                    CONF_SIGNAL_REPETITIONS: user_input.get(CONF_SIGNAL_REPETITIONS, 1),
                }

                devices[self._selected_device_event_code] = device

                if off_delay:
                    device[CONF_OFF_DELAY] = off_delay
                if user_input.get(CONF_DATA_BITS):
                    device[CONF_DATA_BITS] = user_input[CONF_DATA_BITS]
                if command_on:
                    device[CONF_COMMAND_ON] = command_on
                if command_off:
                    device[CONF_COMMAND_OFF] = command_off
                if user_input.get(CONF_VENETIAN_BLIND_MODE):
                    device[CONF_VENETIAN_BLIND_MODE] = user_input[
                        CONF_VENETIAN_BLIND_MODE
                    ]

                self.update_config_data(
                    global_options=self._global_options, devices=devices
                )

                return self.async_create_entry(title="", data={})

        device_data = self._selected_device

        data_schema = {}

        if binary_supported(self._selected_device_object):
            if device_data.get(CONF_OFF_DELAY):
                off_delay_schema = {
                    vol.Optional(
                        CONF_OFF_DELAY,
                        description={"suggested_value": device_data[CONF_OFF_DELAY]},
                    ): str,
                }
            else:
                off_delay_schema = {
                    vol.Optional(CONF_OFF_DELAY): str,
                }
            data_schema.update(off_delay_schema)

        if (
            binary_supported(self._selected_device_object)
            or cover_supported(self._selected_device_object)
            or light_supported(self._selected_device_object)
            or switch_supported(self._selected_device_object)
        ):
            data_schema.update(
                {
                    vol.Optional(
                        CONF_SIGNAL_REPETITIONS,
                        default=device_data.get(CONF_SIGNAL_REPETITIONS, 1),
                    ): int,
                }
            )

        if (
            self._selected_device_object.device.packettype
            == DEVICE_PACKET_TYPE_LIGHTING4
        ):
            data_schema.update(
                {
                    vol.Optional(
                        CONF_DATA_BITS, default=device_data.get(CONF_DATA_BITS, 0)
                    ): int,
                    vol.Optional(
                        CONF_COMMAND_ON,
                        default=hex(device_data.get(CONF_COMMAND_ON, 0)),
                    ): str,
                    vol.Optional(
                        CONF_COMMAND_OFF,
                        default=hex(device_data.get(CONF_COMMAND_OFF, 0)),
                    ): str,
                }
            )

        if isinstance(self._selected_device_object.device, rfxtrxmod.RfyDevice):
            data_schema.update(
                {
                    vol.Optional(
                        CONF_VENETIAN_BLIND_MODE,
                        default=device_data.get(
                            CONF_VENETIAN_BLIND_MODE, CONST_VENETIAN_BLIND_MODE_DEFAULT
                        ),
                    ): vol.In(
                        [
                            CONST_VENETIAN_BLIND_MODE_DEFAULT,
                            CONST_VENETIAN_BLIND_MODE_US,
                            CONST_VENETIAN_BLIND_MODE_EU,
                        ]
                    ),
                }
            )
        devices = {
            entry.id: entry.name_by_user if entry.name_by_user else entry.name
            for entry in self._device_entries
            if self._can_replace_device(entry.id)
        }

        if devices:
            data_schema.update(
                {
                    vol.Optional(CONF_REPLACE_DEVICE): vol.In(devices),
                }
            )

        return self.async_show_form(
            step_id="set_device_options",
            data_schema=vol.Schema(data_schema),
            errors=errors,
        )

    async def _async_replace_device(self, replace_device):
        """Migrate properties of a device into another."""
        device_registry = self._device_registry
        old_device = self._selected_device_entry_id
        old_entry = device_registry.async_get(old_device)
        device_registry.async_update_device(
            replace_device,
            area_id=old_entry.area_id,
            name_by_user=old_entry.name_by_user,
        )

        old_device_data = self._get_device_data(old_device)
        new_device_data = self._get_device_data(replace_device)

        old_device_id = "_".join(x for x in old_device_data[CONF_DEVICE_ID])
        new_device_id = "_".join(x for x in new_device_data[CONF_DEVICE_ID])

        entity_registry = await async_get_entity_registry(self.hass)
        entity_entries = async_entries_for_device(
            entity_registry, old_device, include_disabled_entities=True
        )
        entity_migration_map = {}
        for entry in entity_entries:
            unique_id = entry.unique_id
            new_unique_id = unique_id.replace(old_device_id, new_device_id)

            new_entity_id = entity_registry.async_get_entity_id(
                entry.domain, entry.platform, new_unique_id
            )

            if new_entity_id is not None:
                entity_migration_map[new_entity_id] = entry

        for entry in entity_migration_map.values():
            entity_registry.async_remove(entry.entity_id)

        for entity_id, entry in entity_migration_map.items():
            entity_registry.async_update_entity(
                entity_id,
                new_entity_id=entry.entity_id,
                name=entry.name,
                icon=entry.icon,
            )

        device_registry.async_remove_device(old_device)

    def _can_add_device(self, new_rfx_obj):
        """Check if device does not already exist."""
        new_device_id = get_device_id(new_rfx_obj.device)
        for packet_id, entity_info in self._config_entry.data[CONF_DEVICES].items():
            rfx_obj = get_rfx_object(packet_id)
            device_id = get_device_id(rfx_obj.device, entity_info.get(CONF_DATA_BITS))
            if new_device_id == device_id:
                return False

        return True

    def _can_replace_device(self, entry_id):
        """Check if device can be replaced with selected device."""
        device_data = self._get_device_data(entry_id)

        if (event_code := device_data[CONF_EVENT_CODE]) is not None:
            rfx_obj = get_rfx_object(event_code)
            if (
                rfx_obj.device.packettype
                == self._selected_device_object.device.packettype
                and rfx_obj.device.subtype
                == self._selected_device_object.device.subtype
                and self._selected_device_event_code != event_code
            ):
                return True

        return False

    def _get_device_event_code(self, entry_id):
        data = self._get_device_data(entry_id)

        return data[CONF_EVENT_CODE]

    def _get_device_data(self, entry_id) -> DeviceData:
        """Get event code based on device identifier."""
        event_code: str | None = None
        entry = self._device_registry.async_get(entry_id)
        assert entry
        device_id = get_device_tuple_from_identifiers(entry.identifiers)
        assert device_id
        for packet_id, entity_info in self._config_entry.data[CONF_DEVICES].items():
            if tuple(entity_info.get(CONF_DEVICE_ID)) == device_id:
                event_code = cast(str, packet_id)
                break
        return DeviceData(event_code=event_code, device_id=device_id)

    @callback
    def update_config_data(self, global_options=None, devices=None):
        """Update data in ConfigEntry."""
        entry_data = self._config_entry.data.copy()
        entry_data[CONF_DEVICES] = copy.deepcopy(self._config_entry.data[CONF_DEVICES])
        if global_options:
            entry_data.update(global_options)
        if devices:
            for event_code, options in devices.items():
                if options is None:
                    entry_data[CONF_DEVICES].pop(event_code)
                else:
                    entry_data[CONF_DEVICES][event_code] = options
        self.hass.config_entries.async_update_entry(self._config_entry, data=entry_data)
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry.entry_id)
        )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RFXCOM RFXtrx."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Step when user initializes a integration."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors = {}
        if user_input is not None:
            if user_input[CONF_TYPE] == "Serial":
                return await self.async_step_setup_serial()

            return await self.async_step_setup_network()

        list_of_types = ["Serial", "Network"]

        schema = vol.Schema({vol.Required(CONF_TYPE): vol.In(list_of_types)})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_setup_network(self, user_input=None):
        """Step when setting up network configuration."""
        errors = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            try:
                data = await self.async_validate_rfx(host=host, port=port)
            except CannotConnect:
                errors["base"] = "cannot_connect"

            if not errors:
                return self.async_create_entry(title="RFXTRX", data=data)

        schema = vol.Schema(
            {vol.Required(CONF_HOST): str, vol.Required(CONF_PORT): int}
        )
        return self.async_show_form(
            step_id="setup_network",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_setup_serial(self, user_input=None):
        """Step when setting up serial configuration."""
        errors = {}

        if user_input is not None:
            user_selection = user_input[CONF_DEVICE]
            if user_selection == CONF_MANUAL_PATH:
                return await self.async_step_setup_serial_manual_path()

            dev_path = await self.hass.async_add_executor_job(
                get_serial_by_id, user_selection
            )

            try:
                data = await self.async_validate_rfx(device=dev_path)
            except CannotConnect:
                errors["base"] = "cannot_connect"

            if not errors:
                return self.async_create_entry(title="RFXTRX", data=data)

        ports = await self.hass.async_add_executor_job(serial.tools.list_ports.comports)
        list_of_ports = {}
        for port in ports:
            list_of_ports[
                port.device
            ] = f"{port}, s/n: {port.serial_number or 'n/a'}" + (
                f" - {port.manufacturer}" if port.manufacturer else ""
            )
        list_of_ports[CONF_MANUAL_PATH] = CONF_MANUAL_PATH

        schema = vol.Schema({vol.Required(CONF_DEVICE): vol.In(list_of_ports)})
        return self.async_show_form(
            step_id="setup_serial",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_setup_serial_manual_path(self, user_input=None):
        """Select path manually."""
        errors = {}

        if user_input is not None:
            device = user_input[CONF_DEVICE]
            try:
                data = await self.async_validate_rfx(device=device)
            except CannotConnect:
                errors["base"] = "cannot_connect"

            if not errors:
                return self.async_create_entry(title="RFXTRX", data=data)

        schema = vol.Schema({vol.Required(CONF_DEVICE): str})
        return self.async_show_form(
            step_id="setup_serial_manual_path",
            data_schema=schema,
            errors=errors,
        )

    async def async_validate_rfx(self, host=None, port=None, device=None):
        """Create data for rfxtrx entry."""
        success = await self.hass.async_add_executor_job(
            _test_transport, host, port, device
        )
        if not success:
            raise CannotConnect

        data = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_DEVICE: device,
            CONF_AUTOMATIC_ADD: False,
            CONF_DEVICES: {},
        }
        return data

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlow(config_entry)


def _test_transport(host, port, device):
    """Construct a rfx object based on config."""
    if port is not None:
        try:
            conn = rfxtrxmod.PyNetworkTransport((host, port))
        except OSError:
            return False

        conn.close()
    else:
        try:
            conn = rfxtrxmod.PySerialTransport(device)
        except serial.serialutil.SerialException:
            return False

        if conn.serial is None:
            return False

        conn.close()

    return True


def get_serial_by_id(dev_path: str) -> str:
    """Return a /dev/serial/by-id match for given device if available."""
    by_id = "/dev/serial/by-id"
    if not os.path.isdir(by_id):
        return dev_path

    for path in (entry.path for entry in os.scandir(by_id) if entry.is_symlink()):
        if os.path.realpath(path) == dev_path:
            return path
    return dev_path


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""
