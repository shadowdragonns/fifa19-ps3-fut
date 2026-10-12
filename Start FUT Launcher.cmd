@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
py -3 "%CD%\launcher_gui.py"
if errorlevel 1 pause
popd
endlocal
