"""Device tracker platform for AsusWrt-Merlin integration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_HOSTNAME,
    ATTR_IP,
    ATTR_LAST_SEEN,
    ATTR_MAC,
    DOMAIN,
)
from .coordinator import AsusWrtMerlinDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up device tracker for AsusWrt-Merlin component."""
    # Get coordinator from hass data (created in __init__.py)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Initialize known devices with current data and persisted last_seen cache
    data_devices = coordinator.data or []
    data_macs = {device[ATTR_MAC] for device in data_devices} if data_devices else set()
    cached_macs = (
        set(coordinator.mac_last_seen.keys())
        if getattr(coordinator, "mac_last_seen", None)
        else set()
    )
    coordinator.known_devices = set(data_macs)

    entities = []
    # Create entities for devices present in the current data
    for device in data_devices:
        entities.append(AsusWrtMerlinDeviceTracker(coordinator, device))

    # Also create entities for devices only known via persisted last_seen cache
    offline_only_macs = cached_macs - data_macs
    for mac in offline_only_macs:
        last_seen = coordinator.mac_last_seen.get(mac)
        hostname = None
        try:
            hostname = coordinator.mac_hostname.get(mac)
        except Exception:
            hostname = None
        # Synthesize a minimal device record so the tracker can render as offline
        synth_device = {
            ATTR_MAC: mac,
            ATTR_HOSTNAME: hostname or mac,  # fallback label
            ATTR_LAST_SEEN: last_seen,
            "is_connected": False,
        }
        entities.append(AsusWrtMerlinDeviceTracker(coordinator, synth_device))

    # Do not force an immediate refresh on add; rely on restored state/coordinator
    # to avoid temporary 'unavailable' or state flapping during reloads.
    async_add_entities(entities, False)

    # Set up callback for new devices
    async def handle_new_devices(new_devices: list[dict[str, Any]]) -> None:
        """Handle new devices that appear on the router."""
        new_entities = []
        for device in new_devices:
            new_entities.append(AsusWrtMerlinDeviceTracker(coordinator, device))

        if new_entities:
            _LOGGER.info("Adding %d new device entities", len(new_entities))
            # Avoid update_before_add to prevent brief state flips for existing entities
            async_add_entities(new_entities, False)

    coordinator.set_new_devices_callback(handle_new_devices)


class AsusWrtMerlinDataUpdateCoordinator(AsusWrtMerlinDataUpdateCoordinator):
    """Backwards-compatible alias imported by device_tracker and sensor modules."""

    pass


class AsusWrtMerlinDeviceTracker(ScannerEntity, RestoreEntity):
    """Representation of a tracked device."""

    def __init__(
        self, coordinator: AsusWrtMerlinDataUpdateCoordinator, device: dict[str, Any]
    ) -> None:
        """Initialize the device tracker."""
        self.coordinator = coordinator
        self._device = device
        # Include hostname and MAC in the initial name (for entity_id generation),
        # but avoid duplicating the MAC if it's already present in the hostname.
        hostname = (device.get(ATTR_HOSTNAME) or "").strip()
        mac = device[ATTR_MAC]
        if hostname:
            host_norm = hostname.lower().replace(":", "").replace("-", "")
            mac_norm = mac.lower().replace(":", "").replace("-", "")
            if mac_norm in host_norm:
                self._attr_name = hostname
            else:
                self._attr_name = f"{hostname} ({mac})"
        else:
            self._attr_name = mac
        self._attr_unique_id = device[ATTR_MAC]
        # New devices are created as disabled by default
        self._attr_entity_registry_enabled_default = False

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return if the entity should be enabled when first added to the entity registry."""
        return False

    @property
    def entity_registry_disabled_by(self) -> str | None:
        """Return if the entity should be disabled when first added to the entity registry."""
        return "integration"

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected to the network."""
        if not self.coordinator.data:
            return False

        # Find the device in the current data
        for device in self.coordinator.data:
            if device[ATTR_MAC] == self._device[ATTR_MAC]:
                # Check if device is currently connected (in ARP table)
                if device.get("is_connected", False):
                    return True

                # Check if device was seen recently (only if last_seen exists)
                last_seen = device.get(ATTR_LAST_SEEN)
                if last_seen is not None:
                    if isinstance(last_seen, str):
                        last_seen = datetime.fromisoformat(last_seen)
                    time_diff = datetime.now() - last_seen
                    if time_diff.total_seconds() < self.coordinator.seconds_until_device_away:
                        return True

                # If last_seen is None, device is not connected
                return False

        return False

    @property
    def state(self) -> str:
        """Return the state of the device."""
        if self.is_connected:
            return "home"
        return "not_home"

    @property
    def available(self) -> bool:
        """Keep entity available across brief coordinator failures.

        We do not want transient SSH errors or empty payloads to flip the
        device tracker to 'unavailable' momentarily.
        """
        return True

    @property
    def source_type(self) -> str:
        """Return the source type of the device."""
        return "router"

    @property
    def ip_address(self) -> str | None:
        """Return the IP address of the device."""
        if not self.coordinator.data:
            return None

        for device in self.coordinator.data:
            if device[ATTR_MAC] == self._device[ATTR_MAC]:
                return device.get(ATTR_IP)

        return None

    @property
    def mac_address(self) -> str:
        """Return the MAC address of the device."""
        return self._device[ATTR_MAC]

    @property
    def hostname(self) -> str:
        """Return the hostname of the device."""
        return self._device[ATTR_HOSTNAME]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        attrs = {
            ATTR_MAC: self.mac_address,
        }

        if self.ip_address:
            attrs[ATTR_IP] = self.ip_address

        # Expose last_seen as ISO 8601 string when known
        if self.coordinator.data:
            for device in self.coordinator.data:
                if device[ATTR_MAC] == self._device[ATTR_MAC]:
                    last_seen = device.get(ATTR_LAST_SEEN)
                    if isinstance(last_seen, datetime):
                        attrs[ATTR_LAST_SEEN] = last_seen.isoformat()
                    elif isinstance(last_seen, str):
                        # Assume already in ISO format
                        attrs[ATTR_LAST_SEEN] = last_seen
                    break

        return attrs

    @property
    def should_poll(self) -> bool:
        """No need to poll. Coordinator notifies entity of updates."""
        return False

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information - link to main router device."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.entry.entry_id)},
            "name": f"AsusWrt-Merlin router {self.coordinator.entry.data['host']}",
            "manufacturer": "ASUS",
            "model": "AsusWrt-Merlin router",
        }

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        # Restore last known state to avoid brief unknown/unavailable on startup
        try:
            last_state = await self.async_get_last_state()
            if last_state and last_state.state in ("home", "not_home"):
                self._attr_state = last_state.state  # type: ignore[attr-defined]
        except Exception:
            pass
        _LOGGER.debug(
            "Device tracker entity %s added to hass with enabled_default=%s",
            self.name,
            self.entity_registry_enabled_default,
        )
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_update(self) -> None:
        """Update the entity.

        Only used by the generic entity update service.
        """
        await self.coordinator.async_request_refresh()
