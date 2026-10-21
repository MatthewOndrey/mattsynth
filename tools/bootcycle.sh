#!/bin/bash
# Power-cycle only the Pod's hub port every few seconds. Hold BOOT through one of
# these cycles and the ROM bootloader comes up with a fresh VBUS, which is the one
# thing it insists on before it will enumerate over USB.
for i in $(seq 1 60); do
  if lsusb | grep -q 0483:df11; then
    echo "DFU present, stopping the cycler at $(date +%T)"
    exit 0
  fi
  sudo uhubctl -l 1-1 -p 3 -a cycle -d 2 >/dev/null 2>&1
  sleep 6
done
echo "cycler finished"
