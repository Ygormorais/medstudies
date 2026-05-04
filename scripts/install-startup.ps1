# MedStudies — Register as Windows startup task
# Run once as Administrator: .\scripts\install-startup.ps1

$ProjectDir = (Get-Item $PSScriptRoot).Parent.FullName
$TaskName = "MedStudies"
$Script = Join-Path $ProjectDir "scripts\start.ps1"

# Remove existing task if any
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Description "MedStudies study dashboard — starts at login" | Out-Null

Write-Host "✓ MedStudies registered as startup task." -ForegroundColor Green
Write-Host "  It will start automatically at next login." -ForegroundColor Cyan
Write-Host "  To start now: .\scripts\start.ps1" -ForegroundColor Cyan
