@echo off
setlocal
cd /d "%~dp0"
call bun install --frozen-lockfile
if errorlevel 1 exit /b %errorlevel%
call bun run tauri build --no-bundle
exit /b %errorlevel%
