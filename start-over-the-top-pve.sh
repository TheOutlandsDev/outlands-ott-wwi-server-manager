#!/usr/bin/env bash
set -euo pipefail

steam_root="${OTT_STEAM_ROOT:-$HOME/.steam/steam}"
server_dir="${OTT_SERVER_DIR:-$steam_root/steamapps/common/Over The Top WWI - Dedicated Server}"
proton="${OTT_PROTON:-$steam_root/steamapps/common/Proton - Experimental/proton}"
runtime="${OTT_STEAM_RUNTIME:-$steam_root/steamapps/common/SteamLinuxRuntime_4/_v2-entry-point}"
compat_data="$steam_root/steamapps/compatdata/3052620"
xvfb_run="${OTT_XVFB_RUN:-$(command -v xvfb-run || true)}"
xvfb_log="$server_dir/Logs/xvfb.log"
# Proton initializes Wine's X11 driver even with Unity's -nographics flag.  Use
# a private virtual display so boot-time startup never depends on an interactive
# desktop or RDP login.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
if [[ -z "$xvfb_run" || ! -x "$xvfb_run" || -z "$(command -v Xvfb || true)" ]]; then
  echo "Private Xvfb runtime is missing or not executable." >&2
  exit 1
fi

if pgrep -f '^./Over_The_Top_WWI.exe -batchmode -nographics -server' >/dev/null; then
  echo "Over The Top: WWI dedicated server is already running." >&2
  exit 1
fi

mkdir -p "$compat_data"
cd "$server_dir"

export STEAM_COMPAT_CLIENT_INSTALL_PATH="$steam_root"
export STEAM_COMPAT_DATA_PATH="$compat_data"
export STEAM_COMPAT_APP_ID=3052620
export SteamAppId=3052620
export SteamGameId=3052620
export PROTON_USE_XALIA=0

exec "$xvfb_run" -a -e "$xvfb_log" -s '-screen 0 800x600x24' \
  "$runtime" -- "$proton" run ./Over_The_Top_WWI.exe \
  -batchmode \
  -nographics \
  -server \
  -logFile ./Logs/server.log
