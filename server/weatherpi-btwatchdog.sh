#!/bin/bash
# WeatherThing watchdog (runs every 60s via weatherpi-btwatchdog.timer).
#
# Keeps the CarThing<->Pi Bluetooth PAN alive and self-heals it. On this Pi the BT
# controller is kernel-serdev-attached (fe201000.serial, driver hci_uart_bcm). When the
# PAN link drops (range loss / CT reboot) the BCM4345C0 frequently WEDGES: HCI reset
# (opcode 0x0c03) times out with -110, `btmgmt power on` fails 0x05, `hciconfig up` says
# "Can't init device hci0: Connection timed out (110)". Historically that needed a full
# Pi reboot (bad — this host also runs Home Assistant + the :8080 kiosk).
#
# KEY FIX (2026-07-14): the wedge clears WITHOUT a reboot by unbind/rebinding the serdev
# driver — it reloads the BCM firmware and brings hci0 back UP RUNNING in ~7s. The rebind
# resets the adapter to its REAL mac, so we always re-run weatherpi-btnap afterwards to
# re-apply the spoof (DC:A6:32:62:53:01) + NAP. This is the automatic "re-handshake".
#
# Safety: rebind touches ONLY the BT serdev (never bluetooth.service, never a reboot), and
# only fires when hci0 is actually down/wedged. If the CT is simply off but hci0 is healthy
# we do the cheap gentle btnap re-arm instead. Rate-limited so a permanently-absent CT can't
# thrash the radio.
set -u
SPOOF="DC:A6:32:62:53:01"
CT="192.168.44.2"
API="http://127.0.0.1:8090/api/time"
SERDEV_DRV=/sys/bus/serial/drivers/hci_uart_bcm
SERDEV_DEV=serial0-0
STATE=/run/weatherpi-btwatchdog.fails
LASTFIX=/run/weatherpi-btwatchdog.lastfix
FIXN=/run/weatherpi-btwatchdog.fixn
LOG=/var/log/weatherpi-btwatchdog.log
log(){ echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }

hci_up(){ hciconfig hci0 2>/dev/null | grep -q 'UP RUNNING'; }
hci_mac(){ hciconfig hci0 2>/dev/null | grep -o 'BD Address: [0-9A-F:]*' | awk '{print $3}'; }

# The UP/RUNNING flags are cached kernel state and stay set on a wedged controller
# (seen 2026-08-06: hci0 "UP RUNNING PSCAN ISCAN" while every HCI command timed out with
# -110, so the old hci_up() test called it healthy and the watchdog only ever re-ran btnap
# — 4400+ useless checks over ~3 days). Prove the controller ANSWERS by issuing a real HCI
# command (Read Local Name, 0x0c14): it returns the name when alive, "Connection timed
# out (110)" when wedged.
hci_healthy(){
  hci_up || return 1
  timeout 8 hciconfig hci0 name 2>&1 | grep -q "Name:"
}

# Recover a wedged serdev controller in place (no reboot). Returns 0 if hci0 is UP after.
recover_controller(){
  # A wedged UART controller won't init; try a quick gentle up first (cheap if not wedged).
  timeout 6 hciconfig hci0 up 2>/dev/null
  hci_healthy && { log "hci0 came up without rebind"; return 0; }
  log "hci0 wedged -> serdev rebind ($SERDEV_DEV)"
  echo "$SERDEV_DEV" > "$SERDEV_DRV/unbind" 2>/dev/null
  sleep 4
  echo "$SERDEV_DEV" > "$SERDEV_DRV/bind" 2>/dev/null
  sleep 8
  hci_healthy && { log "hci0 recovered via serdev rebind (mac=$(hci_mac))"; return 0; }
  log "hci0 STILL down after rebind — may need a reboot"; return 1
}

# 1. API health (cheap + safe to restart)
curl -s -m 5 -o /dev/null "$API" || { log "API :8090 down -> restart weatherstation-api"; systemctl restart weatherstation-api; }

# 2. BT link — CT reachable means the PAN is up; clear the fail counter and leave.
if ping -c1 -W2 "$CT" >/dev/null 2>&1; then rm -f "$STATE" "$FIXN"; exit 0; fi
n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STATE"

# Act only on a SUSTAINED outage (>=3 consecutive checks ~3 min) ...
[ "$n" -lt 3 ] && { log "CT unreachable (#$n) — waiting before any action"; exit 0; }
# ... and at most once per 5 min (rebind+btnap is safe but drops BT briefly).
now=$(date +%s); last=$(cat "$LASTFIX" 2>/dev/null || echo 0)
[ $(( now - last )) -lt 300 ] && { log "CT unreachable (#$n) — backing off (acted <5min ago)"; exit 0; }
echo "$now" > "$LASTFIX"

# Count how many fix attempts this outage has taken, so a healthy-looking controller that
# never gets the CT back eventually gets rebound too (some failure modes answer HCI fine
# but still won't page the CT).
f=$(( $(cat "$FIXN" 2>/dev/null || echo 0) + 1 )); echo "$f" > "$FIXN"

if hci_healthy && [ $(( f % 5 )) -ne 0 ]; then
  # Controller answers HCI; the CT is just absent/out-of-range. Re-arm NAP + fix the mac if
  # the spoof was lost (cheap, no radio bounce).
  log "CT unreachable (#$n); hci0 healthy (mac=$(hci_mac)) -> re-run weatherpi-btnap [fix #$f]"
  systemctl restart weatherpi-btnap
else
  # Controller wedged/down (or 5th straight failed fix) -> in-place serdev recovery,
  # then re-apply spoof + NAP.
  log "CT unreachable (#$n); hci0 not answering (or fix #$f) -> attempting in-place recovery"
  if recover_controller; then
    systemctl restart weatherpi-btnap
    log "recovery complete -> re-ran weatherpi-btnap (spoof+NAP re-applied)"
  else
    log "recovery FAILED; NOT auto-rebooting (HA runs on this Pi) — needs manual reboot"
  fi
fi
exit 0
