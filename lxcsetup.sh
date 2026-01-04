#!/bin/bash

# LXD Init Setup Script
# This script installs LXD, initializes it, and sets up a macvlan network for public IPs.
# Run as root or with sudo. Assumes Ubuntu/Debian host.
# Customize: STORAGE_BACKEND (dir/zfs/lvm), MACVLAN_LINK (eth0/enp1s0 etc.), SUBNET (192.168.1.0/24)

set -e  # Exit on error

STORAGE_BACKEND="dir"  # Change to "zfs" or "lvm" if preferred
MACVLAN_LINK="eth0"    # Your host network interface
SUBNET="192.168.1.0/24"  # Your subnet, e.g., for public IPs adjust
GATEWAY="192.168.1.1"  # Gateway IP

echo "🚀 Starting LXD Setup..."

# Step 1: Install snapd and LXD
apt update
apt install -y snapd
snap install lxd

# Step 2: Add current user to lxd group (replace $USER if needed)
usermod -aG lxd $USER
newgrp lxd  # Refresh group

# Step 3: Initialize LXD
lxd init --auto  # Interactive? No, auto for defaults
# Or for custom: uncomment below
# lxd init <<EOF
#yes
#no
#$STORAGE_BACKEND
#default
#no
#no
#no
#no
#EOF

# Step 4: Create macvlan network (for static public IPs, no NAT)
lxc network create macvlan_pub \
  --type=macvlan \
  --config=ipv4.address=none \
  --config=ipv4.nat=false \
  parent=$MACVLAN_LINK

# Optional: Set subnet if needed (but for macvlan, usually none as IPs static)
# lxc network set macvlan_pub ipv4.address=$SUBNET

echo "✅ LXD initialized!"
echo "🔗 Macvlan network 'macvlan_pub' created on $MACVLAN_LINK"
echo "ℹ️  Log out and log back in for group changes, or run 'newgrp lxd'"
echo "💡 Test: lxc info"
echo "💡 For VPS: Use 'lxc launch images:ubuntu/22.04 myvps' then attach network"

# Optional: Enable LXD service
systemctl enable --now snap.lxd.daemon
