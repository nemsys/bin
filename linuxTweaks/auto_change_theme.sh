#!/bin/bash

DISPLAY=:0

# First obtain a location code from: https://weather.codes/search/
# Enter your city name only in the search, and choose the weather code closest to you

# Insert your location. For example USNC0558 is a location code for Raleigh, North Carolina
location="BUXX0005"
tmpfile=/tmp/$location.out

# Obtain sunrise and sunset raw data from weather.com
wget -q "https://weather.com/weather/today/l/$location" -O "$tmpfile"

SUNR=$(grep SunriseSunset "$tmpfile" | grep -oE '((1[0-2]|0?[1-9]):([0-5][0-9]) ?([AaPp][Mm]))' | head -1)
SUNS=$(grep SunriseSunset "$tmpfile" | grep -oE '((1[0-2]|0?[1-9]):([0-5][0-9]) ?([AaPp][Mm]))' | tail -1)

# Strictly speaking, the below 3 variables are not needed - only used to show time in readable format
sr=$(date --date="$SUNR" +%R)
ss=$(date --date="$SUNS" +%R)
ct=$(date +"%R")

# These 3 variables *are* needed to calculate whether we're past sunrise or sunset
sunrise=$(date --date="$SUNR" +%s)
sunset=$(date --date="$SUNS" +%s)
now=$(date +"%s")

# Output current time, sunrise & sunset for your location (OPTIONAL)
echo "Sunrise for location $location: $sr"
echo "Sunset for location $location: $ss"
echo "Current time: $ct"

# Check if current time is past sunrise & set theme accordingly
if [ $now -gt $sunrise -a $now -lt $sunset ]
    then
        echo "Past Sunrise - setting daytime theme"
        # Desktop.
        # 'cinnamon'
        # gsettings set org.cinnamon.theme name 'cinnamon'

        # Window borders.
        # gsettings set org.cinnamon.desktop.wm.preferences theme 'Jade-1'

        # Icons.
        gsettings set org.cinnamon.desktop.interface icon-theme 'Mint-Y-Teal'

        # Controls.
        gsettings set org.cinnamon.desktop.interface gtk-theme 'Mint-Y-Teal'

        # Mouse pointer.
        # gsettings set org.cinnamon.desktop.interface cursor-theme '...'
    fi

# Now do the same for evening/night theme
if [ $now -gt $sunset ]
    then
        echo "Past Sunset - setting evening theme"
        # Themes: 'Jade-1', 'Adapta-Nokto',

        # Desktop.
        gsettings set org.cinnamon.theme name 'Mint-Y-Dark-Teal'

        # Window borders.
        gsettings set org.cinnamon.desktop.wm.preferences theme 'Jade-1'

        # Icons.
        gsettings set org.cinnamon.desktop.interface icon-theme 'Mint-Y-Dark-Teal'

        # Controls.
        gsettings set org.cinnamon.desktop.interface gtk-theme 'Mint-Y-Dark-Teal'

        # Mouse pointer.
        # gsettings set org.cinnamon.desktop.interface cursor-theme '...'
    fi
