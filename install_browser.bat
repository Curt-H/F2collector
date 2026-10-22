@echo off
rem ============================================================
rem  Install the Playwright Firefox kernel INTO the project
rem  folder (ms-playwright\) so the whole project directory is
rem  portable - copy it to any computer and it just works.
rem
rem  Run once on a new computer. Daily use does NOT need this
rem  script: browser_fetch.py auto-detects ms-playwright\ at
rem  the project root (utils\pw_runtime.py).
rem ============================================================
setlocal
set "ROOT=%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%ms-playwright"

echo [1/2] Kernel install dir: %PLAYWRIGHT_BROWSERS_PATH%
echo [2/2] Downloading Firefox kernel (playwright 1.62.0 = firefox-1538)...
"%ROOT%python\python.exe" -m playwright install firefox
if errorlevel 1 (
    echo.
    echo Install FAILED. Check your network. If you see "self-signed
    echo certificate in certificate chain", a proxy/antivirus intercepts
    echo TLS - as a workaround run:  set NODE_TLS_REJECT_UNAUTHORIZED=0
    echo before re-running this script. If a "__dirlock" lock error
    echo repeats, kill leftover node.exe processes or delete the
    echo ms-playwright\ folder, then run this script again.
    pause
    exit /b 1
)

echo.
echo Done. Verifying headless Firefox launch...
"%ROOT%python\python.exe" -c "from playwright.sync_api import sync_playwright; pw = sync_playwright().start(); b = pw.firefox.launch(headless=True); print('HEADLESS LAUNCH OK, Firefox', b.version); b.close(); pw.stop()"
if errorlevel 1 (
    echo Verification FAILED - see message above.
) else (
    echo All good: the kernel now lives inside the project folder.
)
pause
