@echo off
echo Installing Facebook Birthday Bot requirements...
python -m pip install -r "%~dp0requirements.txt" || goto :err
python -m playwright install chromium || goto :err
echo.
echo Done! Start the app with start_windows.bat
pause
exit /b 0
:err
echo.
echo Something went wrong. Make sure Python 3.10+ is installed and "Add python.exe to PATH" was ticked.
pause
