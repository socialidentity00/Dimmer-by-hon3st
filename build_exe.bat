@echo off
rem Builds Dimmer.exe (moon icon) from Dimmer.pyw. Keep this file, Dimmer.pyw and dimmer.ico in the same folder.
cd /d "%~dp0"
set PY=python
where python >nul 2>&1 || set PY=py

%PY% -m pip install --upgrade pyinstaller pystray pillow
%PY% -m PyInstaller --onefile --noconsole --clean --name Dimmer --icon dimmer.ico --hidden-import pystray._win32 Dimmer.pyw

if not exist "dist\Dimmer.exe" (
  echo.
  echo Something went wrong - dist\Dimmer.exe was not created.
  pause
  exit /b 1
)

rem checksum file so the in-app updater can verify the download
powershell -NoProfile -Command "(Get-FileHash 'dist\Dimmer.exe' -Algorithm SHA256).Hash.ToLower() | Set-Content -NoNewline 'dist\Dimmer.exe.sha256'"

echo.
echo Done! In the "dist" folder you now have:
echo    Dimmer.exe          (your app, moon icon)
echo    Dimmer.exe.sha256   (upload this next to it on GitHub)
echo.
pause
