@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0reconnect_tunnel.ps1"
if errorlevel 1 pause
