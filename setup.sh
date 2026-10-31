#!/bin/bash
# Installs everything the project needs on Ubuntu 22.04 / 24.04.
# Run it from the project folder:   bash setup.sh

set -e
cd "$(dirname "$0")"

# use sudo only if we are not root already
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

echo "=== 1. Installing Mininet, Open vSwitch and tools (this can take a few minutes) ==="
$SUDO apt-get update
$SUDO apt-get install -y mininet openvswitch-switch python3 python3-venv python3-pip \
    iperf3 iputils-ping iproute2 net-tools ethtool

echo "=== 2. Starting Open vSwitch ==="
$SUDO systemctl enable --now openvswitch-switch 2>/dev/null || $SUDO service openvswitch-switch start
if ! $SUDO modprobe openvswitch 2>/dev/null; then
    echo "NOTE: the openvswitch kernel module is missing."
    echo "      Use 'sudo python3 topo.py --userspace' instead of 'sudo python3 topo.py'."
fi

echo "=== 3. Installing the SDN controller (os-ken) in a virtual environment ==="
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "=== 4. Quick check ==="
./venv/bin/osken-manager --version
python3 test_basic.py

mkdir -p logs results

echo ""
echo "========================================"
echo "  SETUP DONE"
echo "  Next: open guide.md and go to Step 5"
echo "========================================"
