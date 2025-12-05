"""Sensor platform for AsusWrt-Merlin integration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
    SensorDeviceClass,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AsusWrtMerlinDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Simple module-level WAN stats cache shared by all WAN sensors
_WAN_CACHE: dict[str, Any] = {
    "coordinator_ts": None,  # datetime of last coordinator update processed
    "last_rx_bytes": None,
    "last_tx_bytes": None,
    "last_sample_ts": None,  # datetime of last WAN sample
    "total_download_gb": None,
    "total_upload_gb": None,
    "download_mbps": None,
    "upload_mbps": None,
}


def _ensure_wan_stats_updated(coordinator: AsusWrtMerlinDataUpdateCoordinator) -> None:
    """Update WAN stats once per coordinator cycle using the coordinator's SSH client."""
    try:
        # Only refresh if we haven't processed this coordinator update yet
        if _WAN_CACHE["coordinator_ts"] == coordinator.last_update_time:
            return

        _WAN_CACHE.update(
            {
                "coordinator_ts": coordinator.last_update_time,
                "last_rx_bytes": None,
                "last_tx_bytes": None,
                "last_sample_ts": datetime.now(),
                "total_download_gb": coordinator.wan_total_download_gb,
                "total_upload_gb": coordinator.wan_total_upload_gb,
                "download_mbps": coordinator.wan_download_mbps,
                "upload_mbps": coordinator.wan_upload_mbps,
            }
        )
    except Exception as ex:
        _LOGGER.debug("WAN stats update failed: %s", ex)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensor platform for AsusWrt-Merlin component."""
    # Get coordinator from hass data (created in __init__.py)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        AsusWrtMerlinRouterSensor(coordinator, entry),
        AsusWrtMerlinWanTotalDownloadSensor(coordinator, entry),
        AsusWrtMerlinWanTotalUploadSensor(coordinator, entry),
        AsusWrtMerlinWanDownloadSpeedSensor(coordinator, entry),
        AsusWrtMerlinWanUploadSpeedSensor(coordinator, entry),
        AsusWrtMerlinWanDailyDownloadSensor(coordinator, entry),
        AsusWrtMerlinWanDailyUploadSensor(coordinator, entry),
        AsusWrtMerlinWanMonthlyDownloadSensor(coordinator, entry),
        AsusWrtMerlinWanMonthlyUploadSensor(coordinator, entry),
        AsusWrtMerlinWanYearlyDownloadSensor(coordinator, entry),
        AsusWrtMerlinWanYearlyUploadSensor(coordinator, entry),
    ]

    async_add_entities(entities, True)


class AsusWrtMerlinSensorBase(CoordinatorEntity, SensorEntity):
    """Base class for AsusWrt-Merlin sensors."""

    def __init__(
        self, coordinator: AsusWrtMerlinDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"AsusWrt-Merlin router {entry.data['host']}",
            "manufacturer": "ASUS",
            "model": "AsusWrt-Merlin router",
        }


class AsusWrtMerlinRouterSensor(AsusWrtMerlinSensorBase):
    """Sensor for AsusWrt-Merlin router information."""

    def __init__(
        self, coordinator: AsusWrtMerlinDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "AsusWrt-Merlin router"
        self._attr_unique_id = f"{entry.entry_id}_router_info"
        self._attr_icon = "mdi:router-wireless"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        """Return the number of connected devices as the main value."""
        if not self.coordinator.data:
            return 0

        # Count devices that are currently connected
        connected_count = 0
        for device in self.coordinator.data:
            if device.get("is_connected", False):
                connected_count += 1
            else:
                # Check if device was seen recently
                last_seen = device.get("last_seen")
                if last_seen is not None:
                    if isinstance(last_seen, str):
                        last_seen = datetime.fromisoformat(last_seen)
                    time_diff = datetime.now() - last_seen
                    if (
                        time_diff.total_seconds()
                        < self.coordinator.seconds_until_device_away
                    ):
                        connected_count += 1

        return connected_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return comprehensive state attributes."""
        attrs = {
            # Router connection info
            "router_status": "Connected"
            if self.coordinator.last_update_success
            else "Disconnected",
            "host": self._entry.data["host"],
            "update_interval_seconds": self.coordinator.update_interval.total_seconds(),
        }

        # Last update information
        if self.coordinator.last_update_time:
            attrs["last_update"] = self.coordinator.last_update_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        else:
            attrs["last_update"] = None

        # Device statistics
        if not self.coordinator.data:
            attrs.update(
                {
                    "total_devices": 0,
                    "active_devices": 0,
                    "recently_seen_devices": 0,
                    "offline_devices": 0,
                    "devices": [],
                }
            )
            return attrs

        # Count devices by status
        active_count = 0  # Currently in ARP table (actively communicating)
        recently_seen_count = (
            0  # Not in ARP but seen within seconds_until_device_away time
        )
        offline_count = 0

        for device in self.coordinator.data:
            is_connected = device.get("is_connected", False)

            if is_connected:
                # Device is actively communicating (in ARP table)
                active_count += 1
                recently_seen_count += 1
            else:
                # Check if device was seen recently but not currently active
                last_seen = device.get("last_seen")
                if last_seen is not None:
                    if isinstance(last_seen, str):
                        last_seen = datetime.fromisoformat(last_seen)
                    time_diff = datetime.now() - last_seen
                    if (
                        time_diff.total_seconds()
                        < self.coordinator.seconds_until_device_away
                    ):
                        recently_seen_count += 1
                    else:
                        offline_count += 1
                else:
                    offline_count += 1

        attrs.update(
            {
                "total_devices": len(self.coordinator.data),
                "active_devices": active_count,
                "recently_seen_devices": recently_seen_count,
                "offline_devices": offline_count,
            }
        )

        return attrs


class AsusWrtMerlinWanTotalDownloadSensor(AsusWrtMerlinSensorBase):
    """Sensor for total WAN download in GB."""

    def __init__(
        self, coordinator: AsusWrtMerlinDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN total downloaded"
        self._attr_unique_id = f"{entry.entry_id}_wan_total_download_gb"
        self._attr_icon = "mdi:download"
        self._attr_native_unit_of_measurement = "GB"
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        _ensure_wan_stats_updated(self.coordinator)
        value = _WAN_CACHE.get("total_download_gb")
        if value is None:
            return None
        return round(value, 3)


class AsusWrtMerlinWanTotalUploadSensor(AsusWrtMerlinSensorBase):
    """Sensor for total WAN upload in GB."""

    def __init__(
        self, coordinator: AsusWrtMerlinDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN total uploaded"
        self._attr_unique_id = f"{entry.entry_id}_wan_total_upload_gb"
        self._attr_icon = "mdi:upload"
        self._attr_native_unit_of_measurement = "GB"
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        _ensure_wan_stats_updated(self.coordinator)
        value = _WAN_CACHE.get("total_upload_gb")
        if value is None:
            return None
        return round(value, 3)


class AsusWrtMerlinWanDownloadSpeedSensor(AsusWrtMerlinSensorBase):
    """Sensor for current WAN download speed in Mbps."""

    def __init__(
        self, coordinator: AsusWrtMerlinDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN download speed"
        self._attr_unique_id = f"{entry.entry_id}_wan_download_mbps"
        self._attr_icon = "mdi:download-network"
        self._attr_native_unit_of_measurement = "Mbit/s"

    @property
    def native_value(self) -> float | None:
        _ensure_wan_stats_updated(self.coordinator)
        value = _WAN_CACHE.get("download_mbps")
        if value is None:
            return None
        return round(value, 3)


class AsusWrtMerlinWanUploadSpeedSensor(AsusWrtMerlinSensorBase):
    """Sensor for current WAN upload speed in Mbps."""

    def __init__(
        self, coordinator: AsusWrtMerlinDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN upload speed"
        self._attr_unique_id = f"{entry.entry_id}_wan_upload_mbps"
        self._attr_icon = "mdi:upload-network"
        self._attr_native_unit_of_measurement = "Mbit/s"

    @property
    def native_value(self) -> float | None:
        _ensure_wan_stats_updated(self.coordinator)
        value = _WAN_CACHE.get("upload_mbps")
        if value is None:
            return None
        return round(value, 3)


class _AccumulatingWanCounterSensor(AsusWrtMerlinSensorBase, RestoreEntity):
    """Base for daily/monthly accumulating WAN counters that persist restarts."""

    _period: str  # "daily" or "monthly"
    _direction: str  # "download" or "upload"

    def __init__(
        self, coordinator: AsusWrtMerlinDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._value_gb: float | None = None
        # Initialize to current period to prevent reset during early updates
        # This will be overwritten by async_added_to_hass if there's a saved state
        self._last_period_marker: str | None = None  # Set after self._period exists
        self._last_reset: datetime | None = None
        self._attr_native_unit_of_measurement = "GB"
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_state_class = SensorStateClass.TOTAL
        # Now that _period is available (from subclass), initialize the marker
        self._last_period_marker = self._current_period_marker()
        self._last_reset = self._get_period_start_datetime()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._value_gb = float(last_state.state)
            except (TypeError, ValueError):
                self._value_gb = 0.0
        else:
            self._value_gb = 0.0

        # Restore last period marker and last reset
        if last_state and last_state.attributes:
            self._last_period_marker = last_state.attributes.get("period_marker")
            # If period_marker wasn't saved, initialize to current period to prevent reset
            if self._last_period_marker is None:
                self._last_period_marker = self._current_period_marker()

            # Try to restore last_reset from attributes
            last_reset_str = last_state.attributes.get("last_reset")
            if last_reset_str:
                try:
                    self._last_reset = datetime.fromisoformat(last_reset_str)
                except (TypeError, ValueError):
                    self._last_reset = self._get_period_start_datetime()
            else:
                # If not available, calculate based on current period
                self._last_reset = self._get_period_start_datetime()
        else:
            # Initialize for current period
            self._last_period_marker = self._current_period_marker()
            self._last_reset = self._get_period_start_datetime()

        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def native_value(self) -> float | None:
        return round(self._value_gb, 3) if self._value_gb is not None else None

    @property
    def last_reset(self) -> datetime | None:
        """Return the time when the counter was last reset."""
        return self._last_reset

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {
            "period": self._period,
            "direction": self._direction,
            "period_marker": self._current_period_marker(),
        }
        if self._last_reset:
            attrs["last_reset"] = self._last_reset.isoformat()
        return attrs

    def _current_period_marker(self) -> str:
        now = datetime.now()
        if self._period == "daily":
            return now.strftime("%Y-%m-%d")
        if self._period == "monthly":
            return now.strftime("%Y-%m")
        # yearly
        return now.strftime("%Y")

    def _get_period_start_datetime(self) -> datetime:
        """Get the datetime when the current period started."""
        now = datetime.now()
        if self._period == "daily":
            # Midnight of current day
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._period == "monthly":
            # Midnight of first day of current month
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # yearly - Midnight of January 1st of current year
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    def _maybe_reset_for_new_period(self) -> None:
        marker = self._current_period_marker()
        if self._last_period_marker != marker:
            self._value_gb = 0.0
            self._last_period_marker = marker
            self._last_reset = self._get_period_start_datetime()

    def _handle_coordinator_update(self) -> None:
        try:
            # Reset if new day/month started
            self._maybe_reset_for_new_period()

            # Add latest delta
            if self._direction == "download":
                delta_bytes = self.coordinator.wan_last_rx_delta_bytes or 0
            else:
                delta_bytes = self.coordinator.wan_last_tx_delta_bytes or 0

            self._value_gb = (self._value_gb or 0.0) + (delta_bytes / (1024**3))
        finally:
            self.async_write_ha_state()


class AsusWrtMerlinWanDailyDownloadSensor(_AccumulatingWanCounterSensor):
    _period = "daily"
    _direction = "download"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN daily downloaded"
        self._attr_unique_id = f"{entry.entry_id}_wan_daily_download_gb"
        self._attr_icon = "mdi:download"


class AsusWrtMerlinWanDailyUploadSensor(_AccumulatingWanCounterSensor):
    _period = "daily"
    _direction = "upload"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN daily uploaded"
        self._attr_unique_id = f"{entry.entry_id}_wan_daily_upload_gb"
        self._attr_icon = "mdi:upload"


class AsusWrtMerlinWanMonthlyDownloadSensor(_AccumulatingWanCounterSensor):
    _period = "monthly"
    _direction = "download"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN monthly downloaded"
        self._attr_unique_id = f"{entry.entry_id}_wan_monthly_download_gb"
        self._attr_icon = "mdi:download"


class AsusWrtMerlinWanMonthlyUploadSensor(_AccumulatingWanCounterSensor):
    _period = "monthly"
    _direction = "upload"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN monthly uploaded"
        self._attr_unique_id = f"{entry.entry_id}_wan_monthly_upload_gb"
        self._attr_icon = "mdi:upload"


class AsusWrtMerlinWanYearlyDownloadSensor(_AccumulatingWanCounterSensor):
    _period = "yearly"
    _direction = "download"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN yearly downloaded"
        self._attr_unique_id = f"{entry.entry_id}_wan_yearly_download_gb"
        self._attr_icon = "mdi:download"


class AsusWrtMerlinWanYearlyUploadSensor(_AccumulatingWanCounterSensor):
    _period = "yearly"
    _direction = "upload"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "WAN yearly uploaded"
        self._attr_unique_id = f"{entry.entry_id}_wan_yearly_upload_gb"
        self._attr_icon = "mdi:upload"
