#!/bin/bash

# Check if the correct number of arguments is provided (at least 2 arguments are needed)
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage: merge_folders <source_a> <source_b> [target]"
    exit 1
fi

# Get the source directories from the command line arguments
SOURCE_A=$1
SOURCE_B=$2

echo $SOURCE_A
echo $SOURCE_B

# If target directory is provided, use it. Otherwise, create default target name
if [ -n "$3" ]; then
    TARGET=$3
else
    TARGET="${SOURCE_A}_${SOURCE_B}_merged"
fi

# Create the target directory if it doesn't exist
mkdir -p "$TARGET"

# Copy all files from A to the target directory without overwriting
rsync -a --ignore-existing "$SOURCE_A/" "$TARGET/"

# Iterate over files in folder B and copy them to the target
for file in "$SOURCE_B"/*; do
    if [ -e "$TARGET/$(basename "$file")" ]; then
        # If file with the same name exists, add a suffix to avoid overwriting
        cp "$file" "$TARGET/$(basename "$file")_from_B"
    else
        # If the file doesn't exist, copy it directly
        cp "$file" "$TARGET/"
    fi
done
