@echo off
setlocal

:: Build aes_monitor.exe with PyInstaller.
:: Output goes to dist\aes_monitor.exe alongside the required external files.

echo.
echo === AES Sync Connector — PyInstaller Build ===
echo.

:: Build AESBridge.exe (C# / .NET 4.8)
where dotnet >nul 2>&1
if %errorlevel% == 0 (
    set DOTNET=dotnet
) else if exist "%ProgramFiles%\dotnet\dotnet.exe" (
    set DOTNET="%ProgramFiles%\dotnet\dotnet.exe"
) else (
    echo ERROR: dotnet not found. Install .NET SDK or add it to your PATH.
    pause & exit /b 1
)

%DOTNET% build bridge\AESBridge.csproj -c Release --nologo -v quiet
if errorlevel 1 (
    echo ERROR: AESBridge build failed.
    pause & exit /b 1
)

:: Ensure PyInstaller is installed
python -m pip install --quiet pyinstaller pycryptodome
if errorlevel 1 (
    echo ERROR: pip install failed. Make sure Python is on your PATH.
    pause & exit /b 1
)

:: Stamp the build label the monitor prints at startup (monitor\_build_info.py, gitignored):
:: commit, branch, whether the monitor/bridge sources had uncommitted changes, and when.
set BUILD_SHA=unknown
for /f "delims=" %%i in ('git rev-parse --short HEAD 2^>nul') do set BUILD_SHA=%%i
set BUILD_BRANCH=unknown branch
for /f "delims=" %%i in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set BUILD_BRANCH=%%i
set BUILD_DIRTY=
git diff --quiet HEAD -- bridge/AESBridge.cs monitor/aes_monitor.py 2>nul || set BUILD_DIRTY=, uncommitted changes
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm'"') do set BUILD_DATE=%%i
> monitor\_build_info.py echo BUILD = "local %BUILD_SHA% (%BUILD_BRANCH%%BUILD_DIRTY%), built %BUILD_DATE%"
echo Build: local %BUILD_SHA% (%BUILD_BRANCH%%BUILD_DIRTY%), built %BUILD_DATE%

:: Build single-file exe
python -m PyInstaller ^
    --onefile ^
    --name aes_monitor ^
    --console ^
    --distpath dist ^
    --workpath build ^
    --specpath build ^
    monitor\aes_monitor.py

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed.
    pause & exit /b 1
)

:: Copy bridge binaries
copy /y bridge\bin\Release\net48\AESBridge.exe dist\AESBridge.exe
copy /y bridge\bin\Release\net48\EventScheduler_Release.exe dist\EventScheduler_Release.exe

:: Copy the config template as .example, like the release zip, so it never competes with a real config
:: (aes_config.ini or a dashboard-downloaded connector-config-<eventId>.ini) copied in beside it.
:: It already has exe = AESBridge.exe for this flat layout.
copy /y monitor\aes_config.ini.example dist\aes_config.ini.example >nul
echo Copied dist\aes_config.ini.example. Put your real config (aes_config.ini or connector-config-*.ini) next to it.

echo.
echo === Build complete — dist\ is ready to deploy ===
echo.
pause
