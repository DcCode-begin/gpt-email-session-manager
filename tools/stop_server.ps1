$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "data\server.pid"
$Port = 8765
$stopped = 0

function Stop-ById($ProcessId) {
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Get-ListeningPids($LocalPort) {
    $pids = @()

    try {
        $pids += Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    } catch {
    }

    if (-not $pids) {
        $pattern = "^\s*TCP\s+\S+:$LocalPort\s+\S+\s+LISTENING\s+(\d+)\s*$"
        $matches = netstat -ano | Select-String -Pattern $pattern
        foreach ($match in $matches) {
            if ($match.Line -match $pattern) {
                $pids += [int]$Matches[1]
            }
        }
    }

    return $pids | Select-Object -Unique
}

if (Test-Path -LiteralPath $PidFile) {
    $pidValue = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($pidValue -match "^\d+$") {
        if (Stop-ById ([int]$pidValue)) {
            $stopped += 1
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

try {
    $escapedRoot = [regex]::Escape($Root)
    $processes = Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match "app\.py" -and
            $_.CommandLine -match $escapedRoot
        }

    foreach ($process in $processes) {
        try {
            $null = $process.Terminate()
            $stopped += 1
        } catch {
        }
    }
} catch {
}

try {
    foreach ($ownerPid in (Get-ListeningPids $Port)) {
        if ($ownerPid -and (Stop-ById ([int]$ownerPid))) {
            $stopped += 1
        }
    }
} catch {
}

if ($stopped -gt 0) {
    Write-Host "Email Manager server stopped."
} else {
    Write-Host "No running Email Manager server was found."
}
