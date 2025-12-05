# Home Assistant AsusWrt-Merlin Custom Integration

[![Validate with hassfest](https://github.com/DigitallyRefined/ha-asuswrt_merlin/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/DigitallyRefined/ha-asuswrt_merlin/actions/workflows/hassfest.yaml)
[![HACS Validate](https://github.com/DigitallyRefined/ha-asuswrt_merlin/actions/workflows/hacs_action.yml/badge.svg)](https://github.com/DigitallyRefined/ha-asuswrt_merlin/actions/workflows/hacs_action.yml)
[![hacs\_badge](https://img.shields.io/badge/HACS-Manual-blue.svg)](https://github.com/custom-components/hacs)

![downloads](https://img.shields.io/github/downloads/DigitallyRefined/ha-asuswrt_merlin/total.svg)
![downloads](https://img.shields.io/github/downloads/DigitallyRefined/ha-asuswrt_merlin/latest/total.svg)

A Home Assistant custom integration for AsusWrt-Merlin routers that connects via SSH fetching download/upload statistics and connected devices creating device tracker entities.

This is separate from the official Home Assistant integration, as the official integration was causing my router to become unresponsive.

## Features

* Collects download/update statistics
* Fetches connected devices
* Creates device tracker entities that are disabled by default
* Configurable SSH authentication (password or SSH key)
* Configurable "consider home" timeout & delete inactive devices after
* Automatic device discovery based on DHCP leases and ARP table

## Installation

### HACS (recommended)

If you dont' have [HACS](https://hacs.xyz) installed yet, I highly recommend it.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=DigitallyRefined\&repository=ha-asuswrt_merlin\&category=integration)\
Or copy this GitHub URL and add it as a custom integration repository.

### Manual installation

[Download the latest `asuswrt_merlin.zip` release](https://github.com/DigitallyRefined/ha-asuswrt_merlin/releases) and extract it into your `<config>/custom_component` folder.

## Configuration

After installation you need to **restart** Home Assistant before using this integration.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=asuswrt_merlin)

Or

1. Go to Settings > Devices & Services > Add Integration
2. Search for "AsusWrt-Merlin" and add it

### Required Settings

* **Router IP address**: The IP address of your AsusWrt-Merlin router
* **Username**: SSH username for the router
* **Password** or **SSH Key**: Authentication method

### Optional Settings

* **SSH Port**: Default is 22
* **Consider Home**: Time in seconds before a device is considered away (default: 180)

### SSH Key Authentication

To use SSH key authentication:

1. Generate an SSH key pair on your Home Assistant system
2. Copy the public key to your router's authorized\_keys file
3. Specify the **full path** to the private key file in the configuration

**Note**: The integration uses the SSH key file path directly - it does not store the key content. Make sure the file is readable by the Home Assistant process.

## How It Works

1. The integration connects to your AsusWrt-Merlin router via SSH
2. It fetches the DHCP leases and ARP table to identify connected devices and network statistics
3. Device tracker entities are created for each discovered device (disabled by default)
4. Only enabled entities are updated with home/away status
5. The integration polls the router every 30 seconds for updates
6. Devices that haven't been seen for more than 30 days are automatically removed

## Device Tracker Entities

Each connected device becomes a device tracker entity with:

* **Entity ID**: `device_tracker.{hostname}`
* **Name**: Device hostname
* **State**: `home` or `away`
* **Attributes**:
  * `mac`: MAC address
  * `hostname`: Device hostname
  * `ip`: IP address (when available)

## Troubleshooting

### Connection Issues

* Verify SSH is enabled on your router
* Check that the username and password/key are correct
* Ensure the router IP address is reachable from Home Assistant
* Check firewall settings

### No Devices Found

* Verify that devices are connected to the router
* Check that DHCP is enabled on the router
* Ensure the SSH user has permission to read network information

### Devices Not Updating

* Only enabled device tracker entities are updated
* Check the "consider home" setting if devices appear away too quickly
* Verify the integration is running without errors in the logs

## Requirements

* AsusWrt-Merlin firmware with SSH enabled
* Home Assistant 2025.9.0 or later
* Python paramiko library (automatically installed)
