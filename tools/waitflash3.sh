#!/bin/bash
# One flash attempt per DFU window. The STM32 bootloader drops off the bus after a
# failed transfer, so retrying inside the same window is pointless; wait for the
# next appearance instead.
cd /home/matto/daisy-panel/firmware/daisy-pod || exit 1
BIN=build/PanelLink.bin
while true; do
  if lsusb | grep -q 0483:df11; then
    echo "DFU seen $(date +%T)  image $(stat -c%s "$BIN") bytes"
    sleep 0.8
    out=$(sudo dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D "$BIN" 2>&1)
    echo "$out" | tr '\r' '\n' | grep -E "Erase|Download done|File downloaded|Error|error" | tail -5
    if echo "$out" | grep -q "File downloaded successfully"; then
      echo "FLASH OK $(date +%T)"
      sleep 1
      sudo dfu-util -d 0483:df11 -a 0 -e -R 2>&1 | tail -2
      echo DONE
      exit 0
    fi
    echo "attempt incomplete at $(date +%T)"
    sleep 2
  fi
  sleep 0.2
done
