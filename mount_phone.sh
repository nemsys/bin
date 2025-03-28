#!/bin/bash
MTP_PATH=$(ls -d /run/user/$UID/gvfs/mtp:* 2>/dev/null)
if [ -n "$MTP_PATH" ]; then
    ln -sfn "$MTP_PATH" ~/OP6
    echo "Phone mounted at ~/OP6"
else
    echo "No MTP device found."
fi
