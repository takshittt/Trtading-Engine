@echo off
setlocal
REM ============================================================
REM  Reversal Strategy - Amibroker -> MySQL bridge (PRODUCTION)
REM
REM  Data path:
REM    swing_scan.afl -> C:\swing\scan_60.csv / scan_240.csv / scan_1440.csv
REM      -> this bridge -> INSERT into ami_signal_inbox  (MySQL)
REM      -> backend polls the table -> signals -> trades
REM
REM  This machine never calls the backend over HTTP. The database is the
REM  handoff. If signals stop arriving, check ami_signal_inbox first:
REM    SELECT * FROM ami_signal_inbox ORDER BY id DESC LIMIT 20;
REM  Rows present but processed=0 means the BACKEND is not running.
REM  No rows at all means the problem is on THIS machine.
REM ============================================================

REM --- EDIT: database connection ------------------------------
REM  The quoted  set "VAR=value"  form is REQUIRED for the password: cmd.exe
REM  treats < > & | as operators in a bare  set VAR=value,  and this password
REM  contains "<". The %% is an escaped single % (the password contains "%d").
REM  Written raw it would redirect, set half the password, or both.
set "SQLHOST=129.151.44.44"
set "SQLPORT=3306"
set "SQLUSER=tradingbots"
set "SQLPASS=O5;J9%%d<$JRKsvq<X"
REM  Shared with the ladder bot. Safe: our tables are swing_signals and
REM  ami_signal_inbox; `signals` belongs to the other bot and is not touched.
set "SQLDB=Trading_bots"

set "SCANDIR=C:\swing"

echo ============================================================
echo  Reversal Strategy SQL bridge - PRODUCTION
echo    database : %SQLUSER%@%SQLHOST%:%SQLPORT%/%SQLDB%
echo    folder   : %SCANDIR%   (scan_60=1H, scan_240=4H, scan_1440=1D)
echo ============================================================
echo.

REM --- preflight 1: python on PATH ---------------------------
python --version >nul 2>&1
if errorlevel 1 (
  echo   ERROR: python is not on PATH. Install Python 3 and tick
  echo          "Add python.exe to PATH", then reopen this window.
  goto :fail
)

REM --- preflight 2: PyMySQL ----------------------------------
REM  Unlike the HTTP bridge, this one is NOT stdlib-only.
python -c "import pymysql" >nul 2>&1
if errorlevel 1 (
  echo   PyMySQL missing - installing...
  python -m pip install PyMySQL
  python -c "import pymysql" >nul 2>&1
  if errorlevel 1 (
    echo   ERROR: could not install PyMySQL. Run:  python -m pip install PyMySQL
    goto :fail
  )
)
echo   PyMySQL present - OK

REM --- preflight 3: scan folder ------------------------------
REM  The AFL fopen fails silently if the folder is missing, which looks
REM  identical to "the strategy found nothing" - so create it here.
if not exist "%SCANDIR%" (
  echo   %SCANDIR% missing - creating it.
  mkdir "%SCANDIR%"
)

echo.
echo Starting bridge. Keep this window open. Ctrl+C to stop.
echo.

REM  The script connects once at startup and exits if the login fails, so a
REM  wrong password shows up now rather than at 09:20 on a trading day.
python "%~dp0scan_bridge_sql.py" --dir "%SCANDIR%" ^
  --host %SQLHOST% --port %SQLPORT% --user %SQLUSER% ^
  --password "%SQLPASS%" --database %SQLDB%

echo.
echo Bridge stopped.
goto :end

:fail
echo.
echo Bridge NOT started - fix the above first.

:end
pause >nul
endlocal
