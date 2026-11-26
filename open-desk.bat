@echo off
REM ===================================================================
REM  Opens fx-consensus-desk.html with the COT workbook auto-loaded.
REM  Starts a tiny local server (stdlib only, nothing installed, no
REM  internet) so the page can read cot_normal_table_2022_2026_updated.xlsx
REM  by itself instead of asking you to pick the file every time.
REM
REM  Keep this window open while you use the dashboard. Closing it
REM  stops the server. Re-run this file any time you've updated the
REM  COT workbook and want the page to pick up the new numbers.
REM ===================================================================

cd /d "%~dp0"
set PORT=8420

REM Open the browser a beat after the server actually starts listening —
REM opening it immediately can race a slow Python startup (antivirus scan,
REM cold start) and load the page before the server answers, which looks
REM like the desk "just doesn't have data" until you hit Refresh.
start "" /min cmd /c "timeout /t 1 /nobreak >nul & start "" http://localhost:%PORT%/fx-consensus-desk.html"
python -m http.server %PORT% --bind 127.0.0.1
