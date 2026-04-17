#Requires -Version 5.1
<#
.SYNOPSIS
    Install AI Token Usage Dashboard and register Windows autostart.
.PARAMETER Uninstall
    Remove the scheduled task only.
.PARAMETER NoAutostart
    Install package only; skip Task Scheduler.
.PARAMETER NoShortcut
    Skip Desktop shortcut.
.EXAMPLE
    .\install.ps1
    .\install.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [switch]$NoAutostart,
    [switch]$NoShortcut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName  = "AI-Token-Dashboard"
$ScriptDir = $PSScriptRoot
$VbsPath   = Join-Path $ScriptDir "launch-hidden.vbs"

function Write-Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "    WARN $msg" -ForegroundColor Yellow }

# --- Uninstall ---
if ($Uninstall) {
    Write-Step "Removing scheduled task '$TaskName'"
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-OK "Task removed."
    } else {
        Write-Warn "Task not found -- nothing to remove."
    }
    exit 0
}

# --- Check Python ---
Write-Step "Checking Python"
$pyExe = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
                $pyExe = (Get-Command $candidate).Source
                # Skip WindowsApps shim
                if ($pyExe -like "*WindowsApps*") { continue }
                Write-OK "Found $ver at $pyExe"
                break
            }
        }
    } catch {}
}
if (-not $pyExe) {
    Write-Error "Python 3.10+ not found. Install from https://python.org and retry."
    exit 1
}

# --- Install package via python -m pip (ensures correct interpreter) ---
Write-Step "Installing ai-token-dashboard"
& $pyExe -m pip install -e $ScriptDir --quiet
Write-OK "Package installed."

# --- Write VBS launcher (hides console window) ---
Write-Step "Writing silent launcher"
$vbsContent = @"
Set sh = CreateObject("WScript.Shell")
sh.Run """$pyExe"" -m ai_token_dashboard --no-tray", 0, False
"@
Set-Content -Path $VbsPath -Value $vbsContent -Encoding ASCII
Write-OK "Launcher: $VbsPath"

# --- Desktop shortcut ---
if (-not $NoShortcut) {
    Write-Step "Creating Desktop shortcut"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $lnk     = Join-Path $desktop "AI Token Dashboard.lnk"
    $wsh     = New-Object -ComObject WScript.Shell
    $sc      = $wsh.CreateShortcut($lnk)
    $sc.TargetPath       = $pyExe
    $sc.Arguments        = "-m ai_token_dashboard --open"
    $sc.WorkingDirectory = $env:USERPROFILE
    $sc.WindowStyle      = 7
    $sc.Description      = "AI Token Usage Dashboard"
    $sc.Save()
    Write-OK "Shortcut: $lnk"
}

# --- Task Scheduler autostart (silent via wscript) ---
if (-not $NoAutostart) {
    Write-Step "Registering logon task (no console window)"

    $action = New-ScheduledTaskAction `
        -Execute "wscript.exe" `
        -Argument "`"$VbsPath`"" `
        -WorkingDirectory $env:USERPROFILE

    $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Warn "Task exists -- updating."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName    $TaskName `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Description "AI Token Usage Dashboard silent background server" | Out-Null

    Write-OK "Task '$TaskName' registered -- starts silently at logon."
    Write-Host ""
    Write-Host "  Start now:       Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor DarkGray
    Write-Host "  Open dashboard:  Start-Process 'http://127.0.0.1:8765/'"   -ForegroundColor DarkGray
    Write-Host "  Remove:          .\install.ps1 -Uninstall"                  -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
