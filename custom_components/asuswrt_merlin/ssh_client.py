"""SSH client for AsusWrt-Merlin integration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import paramiko

from .const import (
    ATTR_HOSTNAME,
    ATTR_IP,
    ATTR_LAST_SEEN,
    ATTR_MAC,
    CMD_ARP,
    CMD_DEVICES,
    CMD_WAN_IFNAME,
    CMD_PROC_NET_DEV,
)

_LOGGER = logging.getLogger(__name__)


class AsusWrtSSHClient:
    """SSH client for AsusWrt-Merlin router."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str | None = None,
        ssh_key: str | None = None,
    ) -> None:
        """Initialize the SSH client.

        Args:
            host: Router IP address
            port: SSH port
            username: SSH username
            password: SSH password (optional if using SSH key)
            ssh_key: Path to SSH private key file (optional if using password)
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.ssh_key = ssh_key  # This should be a file path
        self.client: paramiko.SSHClient | None = None
        self._wan_iface_cache: str | None = None

    def connect(self) -> None:
        """Connect to the router via SSH."""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            if self.ssh_key:
                # Use SSH key authentication
                key = self._load_ssh_key(self.ssh_key)
                self.client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    pkey=key,
                    timeout=10,
                )
            else:
                # Use password authentication
                self.client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    timeout=10,
                )
        except Exception as ex:
            self.client.close()
            self.client = None
            raise ConnectionError(
                f"Failed to connect to {self.host}:{self.port}"
            ) from ex

    def disconnect(self) -> None:
        """Disconnect from the router."""
        if self.client:
            self.client.close()
            self.client = None

    def _load_ssh_key(self, key_path: str) -> paramiko.PKey:
        """Load SSH private key from file, supporting multiple key types."""
        try:
            # Try different key types in order of preference
            key_types = [
                paramiko.Ed25519Key,
                paramiko.ECDSAKey,
                paramiko.RSAKey,
                paramiko.DSSKey,
            ]

            for key_type in key_types:
                try:
                    return key_type.from_private_key_file(key_path)
                except (paramiko.SSHException, paramiko.PasswordRequiredException):
                    continue

            # If all key types fail, try with password prompt disabled
            try:
                return paramiko.RSAKey.from_private_key_file(key_path, password=None)
            except paramiko.SSHException:
                pass

            raise paramiko.SSHException(f"Unable to load SSH key from {key_path}")

        except Exception as ex:
            raise ConnectionError(
                f"Failed to load SSH key from {key_path}: {ex}"
            ) from ex

    def _execute_command(self, command: str) -> str:
        """Execute a command on the router."""
        if not self.client:
            raise ConnectionError("Not connected to router")

        try:
            stdin, stdout, stderr = self.client.exec_command(command)
            output = stdout.read().decode("utf-8")
            error = stderr.read().decode("utf-8")

            if error:
                _LOGGER.warning("Command error: %s", error)

            return output
        except Exception as ex:
            raise RuntimeError(f"Failed to execute command: {command}") from ex

    def ping_ips(self, ips: list[str]) -> None:
        """Best-effort: ping a list of IPs in parallel on the router to refresh ARP.

        Uses: ping -c1 -w1 -s32 for each IP, runs them in background and waits.
        Any errors are ignored.
        """
        try:
            if not ips:
                return
            # Build a small shell to ping all IPs in parallel with 1s deadline
            # IPs are expected to be plain numeric strings (safe to inject)
            joined = " ".join(ip for ip in ips if ip)
            if not joined:
                return
            cmd = (
                "sh -c 'for ip in "
                + joined
                + '; do ping -c1 -w1 -s32 "$ip" >/dev/null 2>&1 & done; wait\''
            )
            self._execute_command(cmd)
        except Exception:
            # Non-fatal; continue regardless of ping outcome
            pass

    def get_wan_interface(self) -> str:
        """Get WAN interface name from nvram, with simple cache."""
        if self._wan_iface_cache:
            return self._wan_iface_cache

        output = self._execute_command(CMD_WAN_IFNAME).strip()
        iface = output.splitlines()[0].strip() if output else ""
        if not iface:
            # Fallback to common defaults
            iface = "eth4"
        self._wan_iface_cache = iface
        return iface

    def get_wan_counters(self) -> dict[str, int] | None:
        """Return WAN RX/TX byte counters from /proc/net/dev for the WAN iface.

        Returns a dict: {"rx_bytes": int, "tx_bytes": int} or None on failure.
        """
        iface = self.get_wan_interface()
        output = self._execute_command(CMD_PROC_NET_DEV)
        if not output:
            return None

        # /proc/net/dev format lines like: "  eth0: bytes    packets ... | bytes packets ..."
        lines = output.strip().split("\n")
        for line in lines:
            if ":" not in line:
                continue
            left, right = line.split(":", 1)
            name = left.strip()
            if name != iface:
                continue
            # After colon: receive fields then transmit fields
            parts = right.split()
            if len(parts) < 16:
                continue
            try:
                rx_bytes = int(parts[0])
                tx_bytes = int(parts[8])
                return {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}
            except ValueError:
                continue
        return None

    def get_connected_devices(self) -> list[dict[str, Any]]:
        """Get list of connected devices from the router.

        The ARP table is the primary and authoritative source of device
        discovery.  All entries with flag 0x2 (reachable) are treated as
        currently connected devices.

        DHCP leases are used as optional enrichment only: when a lease
        entry matches an ARP MAC, its hostname is preferred over the
        MAC-derived fallback.  If the lease file is absent or unreadable
        the integration continues with ARP data alone.
        """
        try:
            # --- Primary source: ARP table ---
            arp_output = self._execute_command(CMD_ARP)
            arp_devices = self._parse_arp_table(arp_output)
            _LOGGER.debug("ARP table: %d reachable entries", len(arp_devices))

            # Build a working dict keyed by upper-cased MAC for O(1) access
            mac_to_device: dict[str, dict[str, Any]] = {}
            for arp in arp_devices:
                mac = arp[ATTR_MAC].upper()
                mac_to_device[mac] = {
                    ATTR_MAC: mac,
                    ATTR_IP: arp[ATTR_IP],
                    ATTR_HOSTNAME: f"device_{mac.replace(':', '-')}",  # fallback
                    ATTR_LAST_SEEN: datetime.now(),
                    "is_connected": True,
                }

            # --- Optional enrichment: DHCP leases ---
            try:
                dhcp_output = self._execute_command(CMD_DEVICES)
                dhcp_devices = self._parse_dhcp_leases(dhcp_output)
                _LOGGER.debug("DHCP leases: %d entries", len(dhcp_devices))
                for dhcp in dhcp_devices:
                    mac = dhcp[ATTR_MAC].upper()
                    if mac in mac_to_device:
                        # Enrich the ARP-discovered entry with the DHCP hostname
                        mac_to_device[mac][ATTR_HOSTNAME] = dhcp[ATTR_HOSTNAME]
                    # DHCP-only entries (device has a lease but is not in the ARP
                    # table) are intentionally ignored: if the device is not
                    # reachable on the network right now, the coordinator's
                    # mac_last_seen cache will handle the grace-period logic.
            except Exception as dhcp_ex:
                _LOGGER.debug(
                    "DHCP lease enrichment skipped (file may not exist): %s", dhcp_ex
                )

            devices = list(mac_to_device.values())
            _LOGGER.debug(
                "Found %d connected devices", len(devices)
            )
            return devices

        except Exception as ex:
            _LOGGER.error("Failed to get connected devices: %s", ex)
            return []

    def _parse_dhcp_leases(self, output: str) -> list[dict[str, str]]:
        """Parse DHCP leases output."""
        devices = []
        lines = output.strip().split("\n")

        for line in lines:
            if not line.strip():
                continue

            # Format: timestamp mac ip hostname client_id
            parts = line.split()
            if len(parts) >= 4:
                hostname = parts[3]
                # Use MAC address with underscores if hostname is empty, "*", or just whitespace
                if not hostname or hostname == "*" or not hostname.strip():
                    hostname = f"device_{parts[1].replace(':', '-')}"

                devices.append(
                    {
                        ATTR_MAC: parts[1],
                        ATTR_IP: parts[2],
                        ATTR_HOSTNAME: hostname,
                    }
                )

        return devices

    def _parse_arp_table(self, output: str) -> list[dict[str, str]]:
        """Parse ARP table output."""
        devices = []
        lines = output.strip().split("\n")

        for line in lines:
            if not line.strip() or line.startswith("IP address"):
                continue

            # Format: IP address HW type Flags HW address Mask Device
            parts = line.split()
            if len(parts) >= 6 and parts[2] == "0x2":  # 0x2 means reachable
                devices.append(
                    {
                        ATTR_IP: parts[0],
                        ATTR_MAC: parts[3],
                    }
                )

        return devices
