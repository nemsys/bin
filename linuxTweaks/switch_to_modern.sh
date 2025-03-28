dconf reset /org/cinnamon/panels-enabled
dconf reset /org/cinnamon/panels-height
dconf reset /org/cinnamon/panel-zone-icon-sizes
dconf reset /org/cinnamon/enabled-applets
dconf reset /org/cinnamon/theme/name
dconf reset /org/cinnamon/desktop/interface/gtk-theme
dconf reset /org/cinnamon/desktop/interface/icon-theme
dconf reset /org/cinnamon/desktop/wm/preferences/theme
cinnamon --replace > /dev/null 2>&1 & disown