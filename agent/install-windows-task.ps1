# Registers a Windows scheduled task that runs the Claude usage agent every
# 30 minutes in the background (pythonw, no console window). Run it from the
# agent folder after filling in agent.json:
#   powershell -ExecutionPolicy Bypass -File .\install-windows-task.ps1
# Remove it again with:
#   Unregister-ScheduledTask -TaskName "Claude usage agent" -Confirm:$false

$ErrorActionPreference = "Stop"
$taskName = "Claude usage agent"
$script = Join-Path $PSScriptRoot "claude_usage_agent.py"
$log = Join-Path $env:USERPROFILE ".claude\usage-agent.log"

$python = (Get-Command python -ErrorAction Stop).Source
$py = & $python -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"
if (-not (Test-Path $py)) { throw "pythonw.exe not found next to $python" }

$config = Get-Content (Join-Path $PSScriptRoot "agent.json") -Raw | ConvertFrom-Json
if (-not $config.token -or $config.token -like "PASTE*") { throw "Put the server's AGENT_TOKEN in agent.json first" }

$action = New-ScheduledTaskAction -Execute $py -Argument "`"$script`" --log `"$log`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "Sends Claude Code token usage to $($config.server) every 30 minutes" | Out-Null

Write-Host "Scheduled '$taskName' every 30 minutes (first run in 1 minute)."
Write-Host "Log: $log"
