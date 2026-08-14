@echo off
REM Double-click this to open the OCR review app.
REM uv reads the dependency header inside ocr_review_app.py and installs
REM psycopg + PyMuPDF into a throwaway environment automatically -- nothing
REM is installed into your system Python.
cd /d "%~dp0"
echo Starting OCR review app...
echo A browser tab will open at http://127.0.0.1:8778
echo Close this window (or press Ctrl-C) to stop it.
echo.
uv run ocr_review_app.py
if errorlevel 1 (
  echo.
  echo *** It failed to start. The usual cause is NEON_DATABASE_URL not being
  echo *** set in this session. It is a Windows User environment variable, so
  echo *** a terminal opened before it was set will not see it -- open a new
  echo *** window and try again.
  pause
)
