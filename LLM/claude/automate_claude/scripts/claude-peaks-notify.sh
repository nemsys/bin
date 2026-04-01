#!/bin/bash

# DESCRIPTION:
# This script monitors Anthropic's peak demand hours for Claude Code (13:00-19:00 UTC).
# During these times, session limits drain faster. The script sends a desktop 
# notification to Linux Mint (Cinnamon) at the start and end of these windows.
# Sofia Local Time: Summer (16:00-22:00) | Winter (15:00-21:00).

# Peak hours:     16:00-22:00 Sofia time (13:00-19:00 UTC)
# Non-peak hours: 22:00-16:00 Sofia time (19:00-13:00 UTC)

# Find your desktop session's DBUS to allow cron to send notifications
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

# Get current UTC hour
HOUR_UTC=$(date -u +%H)

# 13 UTC = 16:00 Sofia time (Peak Starts)
if [ "$HOUR_UTC" -eq 13 ]; then
    notify-send "Claude Code" "⚠️ Peak hours STARTED! Session limits will drain faster until 22:00 Sofia time." -i dialog-warning
# 19 UTC = 22:00 Sofia time (Peak Ends)
elif [ "$HOUR_UTC" -eq 19 ]; then
    notify-send "Claude Code" "✅ Peak hours ENDED! Work hard until 16:00 Sofia time." -i dialog-information
fi