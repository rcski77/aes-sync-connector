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

:: Copy config template only if no config exists yet (don't overwrite edited values on rebuild)
if not exist dist\aes_config.ini (
    copy monitor\aes_config.ini.example dist\aes_config.ini
    echo Created dist\aes_config.ini from example — fill in your password, endpoint, and API key.
) else (
    echo Kept existing dist\aes_config.ini.
)

:: Ensure bridge exe path is set correctly for the flat deploy layout
powershell -Command "(Get-Content dist\aes_config.ini) -replace '^exe\s*=.*', 'exe = AESBridge.exe' | Set-Content dist\aes_config.ini"

echo.
echo === Build complete — dist\ is ready to deploy ===
echo.
pause
