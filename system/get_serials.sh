#!/bin/bash

echo "Gathering serial numbers of components..."

# Get motherboard serial number
echo -e "\nMOTHERBOARD SERIAL NUMBER:"
sudo dmidecode -t baseboard | grep "Serial Number"

# Get CPU ID
echo -e "\nCPU ID:"
sudo dmidecode -t processor | grep "ID"

# Get RAM data
echo -e "\nRAM DATA:"
sudo dmidecode -t memory
# sudo dmidecode -t 17

# Get disk serial numbers
echo -e "\nDISK DATA:"
lsblk -o NAME,SERIAL

# Get battery data
echo -e "\nBATTERY DATA:"
# sudo dmidecode -t battery | grep "Serial Number"
sudo cat /sys/class/power_supply/BAT0/uevent

# Get chassis serial number
echo -e "\nCHASSIS SERIAL NUMBER:"
sudo dmidecode -t chassis | grep "Serial Number"

# Get BIOS serial number
echo -e "\nBIOS DATA:"
sudo dmidecode -t bios

# Get network card MAC addresses (can identify internal Wi-Fi & Ethernet adapters)
echo -e "\nNETWORK ADAPTER MAC ADDRESSES:"
ip link show | grep -E "ether" | awk '{print $2}'

echo -e "\nSerial numbers gathered."
