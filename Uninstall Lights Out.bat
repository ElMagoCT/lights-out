@echo off
rem Removes the helper task and the shortcuts. Self-elevates (one UAC prompt).
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
taskkill /im LightsOut.exe /f >nul 2>&1
taskkill /im LightsOutHelper.exe /f >nul 2>&1
if exist "%~dp0LightsOut.exe" (
  "%~dp0LightsOut.exe" --uninstall
) else (
  for %%P in (pythonw.exe) do set PYW=%%~$PATH:P
  if "%PYW%"=="" set PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe
  "%PYW%" "%~dp0install.py" --uninstall
)
