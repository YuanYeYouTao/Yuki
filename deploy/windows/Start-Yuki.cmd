@echo off
wsl.exe --distribution Yuki-Bocchi --user root --exec bash -c "set -e; systemctl start docker yuki-bocchi; cd /home/yuki/app/gateway; docker compose --project-name yuki-bocchi-gateway up -d; exec flock -n /run/yuki-wsl-keepalive.lock sleep infinity"
