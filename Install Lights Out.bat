@echo off
rem Lights Out installer: registers the elevated helper task, adds Startup and
rem Desktop shortcuts, starts the app. Self-elevates (one UAC prompt).
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
if exist "%~dp0LightsOut.exe" (
  "%~dp0LightsOut.exe" --install
) else (
  for %%P in (pythonw.exe) do set PYW=%%~$PATH:P
  if "%PYW%"=="" set PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe
  "%PYW%" "%~dp0install.py"
)
