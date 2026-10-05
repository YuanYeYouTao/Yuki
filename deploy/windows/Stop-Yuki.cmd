@echo off
wsl.exe --distribution Yuki-Bocchi --user root --exec bash -c "cd /home/yuki/app/gateway && docker compose --project-name yuki-bocchi-gateway stop && systemctl stop yuki-bocchi"
if errorlevel 1 goto done
wsl.exe --terminate Yuki-Bocchi
:done
pause
