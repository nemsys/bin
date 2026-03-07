#!/bin/bash
BACKUP_DIR="mint-cinnamon-backup-$(date +%Y%m%d)"
mkdir "$BACKUP_DIR"

cp -r ~/.cinnamon "$BACKUP_DIR/"
cp -r ~/.local/share/cinnamon "$BACKUP_DIR/"
cp -r ~/.config/gtk-3.0 "$BACKUP_DIR/"
cp -r ~/.config/gtk-4.0 "$BACKUP_DIR/"
cp ~/.gtkrc-2.0 "$BACKUP_DIR/" 2>/dev/null
dconf dump /org/cinnamon/ > "$BACKUP_DIR/cinnamon.dconf"

tar -czf "$BACKUP_DIR.tar.gz" "$BACKUP_DIR"