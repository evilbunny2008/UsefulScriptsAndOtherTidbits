#!/bin/bash

for cluster in E000 E001 E002 E003 E004 E005 E006 E007 E008 E009 E010 EF00
do
  for cmd in $(seq 0 255)
  do
    CMD=$(printf '%02X' $cmd)
    newCMD="0x${cluster}:0x${CMD}:1"
    echo "Trying ${newCMD}"
    mosquitto_pub -u 'username' -P 'password' -t "zigbee2mqtt/DeviceFriendlyName/set" -m "{\"diag\": \"${newCMD}\"}"
    sleep 1
  done
done
