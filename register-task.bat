@echo off
schtasks /Delete /TN "AI-Token-Dashboard" /F 2>nul
schtasks /Create /TN "AI-Token-Dashboard" /TR "wscript.exe \"D:\GitHub Projects\ai-token-usage-dashboard\launch-hidden.vbs\"" /SC ONLOGON /RL LIMITED /F
if %errorlevel%==0 (
    echo Task registered OK.
) else (
    echo ERROR registering task.
)
