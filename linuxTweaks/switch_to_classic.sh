
dconf write /org/cinnamon/panels-enabled "['1:0:bottom']"
dconf write /org/cinnamon/panels-height "['1:27']"
dconf write /org/cinnamon/panel-zone-icon-sizes '[{"left":0,"center":0,"right":0,"panelId":1}]'
dconf write /org/cinnamon/enabled-applets "['panel1:left:0:menu@cinnamon.org','panel1:left:1:show-desktop@cinnamon.org','panel1:left:2:panel-launchers@cinnamon.org','panel1:left:3:window-list@cinnamon.org','panel1:right:0:systray@cinnamon.org','panel1:right:1:xapp-status@cinnamon.org','panel1:right:2:keyboard@cinnamon.org','panel1:right:3:notifications@cinnamon.org','panel1:right:4:printers@cinnamon.org','panel1:right:5:removable-drives@cinnamon.org','panel1:right:6:user@cinnamon.org','panel1:right:7:network@cinnamon.org','panel1:right:8:sound@cinnamon.org','panel1:right:9:power@cinnamon.org','panel1:right:10:calendar@cinnamon.org']"
dconf write /org/cinnamon/theme/name "'Linux Mint'"
dconf write /org/cinnamon/desktop/interface/gtk-theme "'Mint-X'"
dconf write /org/cinnamon/desktop/interface/icon-theme "'Mint-X'"
dconf write /org/cinnamon/desktop/wm/preferences/theme "'Mint-X'"
cinnamon --replace > /dev/null 2>&1 & disown