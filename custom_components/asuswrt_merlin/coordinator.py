"""Shared data coordinator for AsusWrt-Merlin integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    ATTR_MAC,
    ATTR_LAST_SEEN,
    ATTR_IP,
    ATTR_HOSTNAME,
    CONF_DAYS_UNTIL_DEVICE_REMOVAL,
    CONF_SECONDS_UNTIL_DEVICE_AWAY,
    DEFAULT_DAYS_UNTIL_DEVICE_REMOVAL,
    DEFAULT_SECONDS_UNTIL_DEVICE_AWAY,
    DOMAIN,
)
from .ssh_client import AsusWrtSSHClient

_LOGGER = logging.getLogger(__name__)


class AsusWrtMerlinDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch devices and WAN stats in one SSH session."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.hass = hass
        self.ssh_client = AsusWrtSSHClient(
            host=entry.data["host"],
            port=entry.data.get("port", 22),
            username=entry.data["username"],
            password=entry.data.get("password"),
            ssh_key=entry.data.get("ssh_key"),
        )
        # Read from options first (for reconfiguration), fallback to data
        self.seconds_until_device_away = entry.options.get(
            CONF_SECONDS_UNTIL_DEVICE_AWAY,
            entry.data.get(
                CONF_SECONDS_UNTIL_DEVICE_AWAY, DEFAULT_SECONDS_UNTIL_DEVICE_AWAY
            ),
        )

        self.last_update_time: datetime | None = None
        self.known_devices: set[str] = set()
        self.new_devices_callback = None
        self.mac_last_seen: dict[str, datetime] = {}
        self.mac_hostname: dict[str, str] = {}
        # Configurable device removal threshold (defaults to 30 days)
        # Read from options first (for reconfiguration), fallback to data
        days_until_device_removal = entry.options.get(
            CONF_DAYS_UNTIL_DEVICE_REMOVAL,
            entry.data.get(
                CONF_DAYS_UNTIL_DEVICE_REMOVAL, DEFAULT_DAYS_UNTIL_DEVICE_REMOVAL
            ),
        )
        self._prune_threshold: timedelta = timedelta(days=days_until_device_removal)
        self._store: Store = Store(
            hass,
            version=1,
            key=f"{DOMAIN}_{entry.entry_id}_last_seen",
        )

        # WAN traffic tracking
        self._last_wan_rx_bytes: int | None = None
        self._last_wan_tx_bytes: int | None = None
        self._last_wan_sample_time: datetime | None = None
        self.wan_total_download_gb: float | None = None
        self.wan_total_upload_gb: float | None = None
        self.wan_download_mbps: float | None = None
        self.wan_upload_mbps: float | None = None
        self.wan_last_rx_delta_bytes: int | None = None
        self.wan_last_tx_delta_bytes: int | None = None

        # Throttle client refreshes to avoid running update_clients too often
        self._last_clients_ping: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    def _iter_our_device_tracker_entries(self, registry):
        """Yield entity registry entries for this config entry's device_trackers on our platform."""
        try:
            entries = er.async_entries_for_config_entry(registry, self.entry.entry_id)
        except Exception:
            entries = []
        for entity_entry in entries:
            try:
                if entity_entry.domain != "device_tracker":
                    continue
                if entity_entry.platform != DOMAIN:
                    continue
                yield entity_entry
            except Exception:
                continue

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Update devices and WAN stats via SSH (single session)."""
        try:
            _LOGGER.debug("Starting data update")
            devices, wan_stats = await self.hass.async_add_executor_job(
                self._get_data_from_router
            )
            self.last_update_time = datetime.now()

            if not isinstance(devices, list):
                _LOGGER.warning("Expected list of devices, got %s", type(devices))
                devices = []

            if devices:
                mac_to_device = {
                    device.get(ATTR_MAC): device
                    for device in devices
                    if isinstance(device, dict) and device.get(ATTR_MAC)
                }
                current_device_macs = set(mac_to_device.keys())
                new_devices = current_device_macs - self.known_devices
                if new_devices:
                    # Only store newly discovered devices if they are currently connected
                    connected_new_devices = {
                        mac
                        for mac in new_devices
                        if mac_to_device.get(mac, {}).get("is_connected", False)
                    }
                    if connected_new_devices:
                        self.known_devices.update(connected_new_devices)
                        if self.new_devices_callback:
                            new_device_data = [
                                mac_to_device[mac]
                                for mac in connected_new_devices
                                if isinstance(mac_to_device.get(mac), dict)
                            ]
                            await self.new_devices_callback(new_device_data)

                # Update last seen timestamps for pruning
                now = datetime.now()
                for device in devices:
                    try:
                        if not isinstance(device, dict):
                            continue
                        mac = device.get(ATTR_MAC)
                        if not mac:
                            continue
                        # Track hostname when available
                        host = device.get(ATTR_HOSTNAME)
                        if isinstance(host, str) and host.strip():
                            self.mac_hostname[mac] = host
                        # If currently connected, consider seen now
                        if device.get("is_connected", False):
                            self.mac_last_seen[mac] = now
                            continue
                        # Else, use last_seen if available
                        last_seen = device.get(ATTR_LAST_SEEN)
                        if last_seen is not None:
                            if isinstance(last_seen, str):
                                try:
                                    last_seen = datetime.fromisoformat(last_seen)
                                except Exception:
                                    # Skip unparsable timestamps
                                    continue
                            if isinstance(last_seen, datetime):
                                self.mac_last_seen[mac] = last_seen
                    except Exception:
                        # Never let a single bad device break the cycle
                        continue

            if wan_stats:
                self._update_wan_metrics(wan_stats)

            _LOGGER.debug("Data update completed successfully")
            # Prune stale device_tracker entities asynchronously
            await self._async_prune_stale_entities()
            # Persist last seen map
            await self._async_save_persisted_last_seen()
            # Ensure disconnected devices carry a last_seen equal to their last seen time
            # so that trackers can apply the grace period (seconds_until_device_away)
            try:
                if devices:
                    for device in devices:
                        try:
                            if not isinstance(device, dict):
                                continue
                            mac = device.get(ATTR_MAC)
                            if not mac:
                                continue
                            if device.get("is_connected", False):
                                # Connected devices already updated above
                                continue
                            has_last = device.get(ATTR_LAST_SEEN) is not None
                            if not has_last:
                                last_seen = self.mac_last_seen.get(mac)
                                if last_seen is not None:
                                    device[ATTR_LAST_SEEN] = last_seen
                        except Exception:
                            continue
            except Exception:
                # Non-fatal enrichment failure should not break updates
                pass
            return devices
        except Exception as ex:
            _LOGGER.error("Error in _async_update_data: %s", ex, exc_info=True)
            raise UpdateFailed(f"Error communicating with router: {ex}") from ex

    def _get_data_from_router(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
        """Fetch all data using a single SSH connection."""
        try:
            self.ssh_client.connect()
            devices = self.ssh_client.get_connected_devices()
            wan_stats = self.ssh_client.get_wan_counters()

            # Determine if we should run pings on this cycle
            should_ping = False
            try:
                now = datetime.now()
                if self._last_clients_ping is None or (
                    now - self._last_clients_ping
                ) >= timedelta(minutes=5):
                    should_ping = True
            except Exception:
                should_ping = False

            # If due, ping only devices currently marked as connected
            if should_ping and isinstance(devices, list) and devices:
                try:
                    # Build set of enabled device_tracker MACs for this entry
                    enabled_macs: set[str] = set()
                    try:
                        registry = er.async_get(self.hass)
                        for entity_entry in self._iter_our_device_tracker_entries(
                            registry
                        ):
                            # Only include entities that are not disabled in the registry
                            if (
                                getattr(entity_entry, "disabled_by", None) is None
                                and entity_entry.unique_id
                            ):
                                enabled_macs.add(entity_entry.unique_id)
                    except Exception:
                        enabled_macs = set()

                    ips = [
                        d.get(ATTR_IP)
                        for d in devices
                        if isinstance(d, dict)
                        and d.get("is_connected", False)
                        and d.get(ATTR_IP)
                        and d.get(ATTR_MAC) in enabled_macs
                    ]
                    if ips:
                        _LOGGER.debug(
                            "Pinging %d connected device IPs: %s",
                            len(ips),
                            ", ".join(ips),
                        )
                        self.ssh_client.ping_ips(ips)
                        self._last_clients_ping = datetime.now()
                except Exception:
                    pass

            return devices, wan_stats
        except Exception as ex:
            _LOGGER.error("SSH fetch failed: %s", ex, exc_info=True)
            return [], None
        finally:
            self.ssh_client.disconnect()

    def set_new_devices_callback(self, callback) -> None:
        """Set callback for new device notifications."""
        self.new_devices_callback = callback

    async def async_load_persisted_last_seen(self) -> None:
        """Load persisted last-seen timestamps and hostnames from storage."""
        try:
            data = await self._store.async_load()
            if not data or not isinstance(data, dict):
                return
            for mac, stored in data.items():
                if not isinstance(stored, dict):
                    continue
                ts = stored.get("last_seen")
                host = stored.get("hostname")
                if isinstance(ts, str):
                    try:
                        self.mac_last_seen[mac] = datetime.fromisoformat(ts)
                    except Exception:
                        pass
                if isinstance(host, str) and host.strip():
                    self.mac_hostname[mac] = host
        except Exception as ex:
            _LOGGER.debug("Failed to load persisted last_seen: %s", ex)

    async def _async_save_persisted_last_seen(self) -> None:
        """Persist last-seen timestamps to storage, along with hostnames."""
        try:
            serializable: dict[str, dict[str, str]] = {}
            for mac, ts in self.mac_last_seen.items():
                if not isinstance(ts, datetime):
                    continue
                entry: dict[str, str] = {"last_seen": ts.isoformat()}
                host = self.mac_hostname.get(mac)
                if isinstance(host, str) and host.strip():
                    entry["hostname"] = host
                serializable[mac] = entry
            await self._store.async_save(serializable)
        except Exception as ex:
            _LOGGER.debug("Failed to save persisted last_seen: %s", ex)

    async def _async_prune_stale_entities(self) -> None:
        """Remove old device_tracker entities not seen for over the prune threshold."""
        try:
            registry = er.async_get(self.hass)
            cutoff = datetime.now() - self._prune_threshold
            # Iterate over all entities and filter to our platform/domain/entry
            for entity_entry in self._iter_our_device_tracker_entries(registry):
                try:
                    mac = entity_entry.unique_id
                    if not mac:
                        continue
                    last_seen = self.mac_last_seen.get(mac)
                    if last_seen is None or last_seen < cutoff:
                        _LOGGER.info(
                            "Pruning stale device_tracker entity %s (MAC %s, last seen %s)",
                            entity_entry.entity_id,
                            mac,
                            last_seen,
                        )
                        registry.async_remove(entity_entry.entity_id)
                        self.known_devices.discard(mac)
                        self.mac_last_seen.pop(mac, None)
                        self.mac_hostname.pop(mac, None)
                except Exception:
                    # Continue pruning other entities even if one fails
                    continue
        except Exception as ex:
            _LOGGER.debug("Pruning stale entities failed: %s", ex)

    def _update_wan_metrics(self, counters: dict[str, int]) -> None:
        """Compute WAN totals in GB and speeds in Mbps from byte counters."""
        now = datetime.now()
        rx_bytes = counters.get("rx_bytes")
        tx_bytes = counters.get("tx_bytes")
        if rx_bytes is None or tx_bytes is None:
            return

        # Totals in GB (base-2 as GB per earlier choice)
        self.wan_total_download_gb = rx_bytes / (1024**3)
        self.wan_total_upload_gb = tx_bytes / (1024**3)

        # Speeds
        rx_delta: int | None = None
        tx_delta: int | None = None

        if (
            self._last_wan_rx_bytes is not None
            and self._last_wan_tx_bytes is not None
            and self._last_wan_sample_time is not None
        ):
            elapsed = (now - self._last_wan_sample_time).total_seconds()
            if elapsed > 0:
                rx_delta = max(0, rx_bytes - self._last_wan_rx_bytes)
                tx_delta = max(0, tx_bytes - self._last_wan_tx_bytes)
                self.wan_download_mbps = (rx_delta * 8) / 1_000_000.0 / elapsed
                self.wan_upload_mbps = (tx_delta * 8) / 1_000_000.0 / elapsed
        else:
            rx_delta = None
            tx_delta = None

        self._last_wan_rx_bytes = rx_bytes
        self._last_wan_tx_bytes = tx_bytes
        self._last_wan_sample_time = now

        # Expose deltas for sensors to accumulate
        self.wan_last_rx_delta_bytes = rx_delta if rx_delta is not None else 0
        self.wan_last_tx_delta_bytes = tx_delta if tx_delta is not None else 0
