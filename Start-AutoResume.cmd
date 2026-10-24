@echo off
setlocal
if exist "%USERPROFILE%\miniforge3\pythonw.exe" (
  start "" "%USERPROFILE%\miniforge3\pythonw.exe" "%~dp0app.py"
  exit /b
)
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
  start "" pythonw.exe "%~dp0app.py"
  exit /b
)
echo Python with Tkinter is required. See README.md.
pause
