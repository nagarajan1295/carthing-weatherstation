#!/bin/bash
# Bedroom weather-station Pi: Bluetooth PAN access point (NAP) for the Car Thing.
# The CT firmware is hard-locked to pair to MAC DC:A6:32:62:53:01 and its rootfs resets
# each boot, so we (a) spoof this Pi's BT public address to that MAC every boot, then
# (b) bring up a br0 NAP (192.168.44.1/24). The CT connects as a PANU client with a static
# 192.168.44.2 and loads the weather station on :8090. Runs as weatherpi-btnap.service.
set -u
SPOOF="DC:A6:32:62:53:01"
log(){ echo "btnap: $*"; }

# Unblock the BT radio
rfkill unblock bluetooth 2>/dev/null || true
for r in /sys/class/rfkill/*; do
  [ "$(cat "$r/type" 2>/dev/null)" = "bluetooth" ] && echo 0 > "$r/soft" 2>/dev/null || true
done

# Wait for the controller to actually appear on the mgmt interface (serdev attach lags boot).
i=0
while ! btmgmt info 2>/dev/null | grep -q "hci0"; do
  i=$((i + 1)); [ "$i" -ge 30 ] && { log "controller not on mgmt after 30s (adapter wedged?)"; break; }
  sleep 1
done

# (a) Spoof the public address, then VERIFY it took. This BCM chip needs an HCI reset for
# the static address to become the active BD address, so retry reset+verify up to 8 times.
btmgmt power off >/dev/null 2>&1 || true
btmgmt public-addr "$SPOOF" >/dev/null 2>&1 || true
btmgmt power on  >/dev/null 2>&1 || true
sleep 1
ok=0
for n in 1 2 3 4 5 6 7 8; do
  cur=$(hciconfig hci0 2>/dev/null | grep -o 'BD Address: [0-9A-F:]*' | awk '{print $3}')
  if [ "$cur" = "$SPOOF" ]; then ok=1; break; fi
  hciconfig hci0 down 2>/dev/null || true
  hciconfig hci0 reset 2>/dev/null || true
  hciconfig hci0 up 2>/dev/null || true
  sleep 1
done
[ "$ok" = 1 ] && log "spoof OK: $SPOOF" || log "WARN: spoof NOT confirmed (got '$cur')"
hciconfig hci0 up 2>/dev/null || true

# (b) Bridge for PAN clients
if ! ip link show br0 >/dev/null 2>&1; then
  ip link add name br0 type bridge 2>/dev/null || brctl addbr br0 2>/dev/null || true
fi
ip addr add 192.168.44.1/24 dev br0 2>/dev/null || true
ip link set br0 up

# Persistent Just-Works agent (auto-accepts pairing, no PIN)
pkill -f "bt-agent -c" 2>/dev/null || true
setsid bt-agent -c NoInputNoOutput >/tmp/btagent.log 2>&1 </dev/null &

# Discoverable + pairable, no timeout
bluetoothctl <<BT || true
power on
discoverable-timeout 0
pairable on
discoverable on
BT
hciconfig hci0 piscan 2>/dev/null || true

# NAP server: incoming PAN connections enslaved to br0; retry once if it dies immediately.
start_nap(){ pkill -f "bt-network -s" 2>/dev/null || true
  setsid bt-network -s nap br0 >/tmp/btnap.log 2>&1 </dev/null & }
start_nap; sleep 2
pgrep -f "bt-network -s" >/dev/null || { sleep 2; start_nap; sleep 1; }

log "up. BT $(hciconfig hci0 | grep -o 'BD Address: [0-9A-F:]*')  br0 192.168.44.1  bt-network: $(pgrep -f 'bt-network -s' >/dev/null && echo running || echo DOWN)"
