param(
    [switch]$NoOpen,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
$Url = "http://127.0.0.1:8765/"
$Port = 8765
$StatusUrl = "${Url}api/status"
$DataDir = Join-Path $Root "data"
$LogDir = Join-Path $DataDir "logs"
$PidFile = Join-Path $DataDir "server.pid"
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Write-Step($Message) {
    Write-Host "[EmailManager] $Message"
}

function Test-Server {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $StatusUrl -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Update-PidFromPort {
    $ownerPid = $null
    try {
        $ownerPid = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1 -ExpandProperty OwningProcess
    } catch {
    }

    if (-not $ownerPid) {
        $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
        $line = netstat -ano | Select-String -Pattern $pattern | Select-Object -First 1
        if ($line -and $line.Line -match $pattern) {
            $ownerPid = $Matches[1]
        }
    }

    if ($ownerPid) {
        Set-Content -LiteralPath $PidFile -Value $ownerPid -Encoding ASCII
    }
}

function Get-PythonCommand {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        if ($LASTEXITCODE -eq 0) {
            return @{ File = $python.Source; Prefix = @() }
        }
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        if ($LASTEXITCODE -eq 0) {
            return @{ File = $py.Source; Prefix = @("-3") }
        }
    }

    throw "Python 3.10+ was not found. Install Python and enable Add Python to PATH."
}

function Invoke-Python($Command, $Arguments) {
    & $Command.File @($Command.Prefix + $Arguments)
}

function ConvertTo-ArgumentString($Arguments) {
    return ($Arguments | ForEach-Object {
        $value = [string]$_
        if ($value -match '[\s"]') {
            '"' + ($value -replace '"', '\"') + '"'
        } else {
            $value
        }
    }) -join " "
}

function Ensure-Venv($BasePython) {
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Step "First run: creating the local runtime..."
        Invoke-Python $BasePython @("-m", "venv", $VenvDir)
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
            throw "Failed to create the local runtime. Python 3.10+ is required."
        }
    }

    return @{ File = $VenvPython; Prefix = @() }
}

function Test-Playwright($Command) {
    $code = "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('playwright') else 1)"
    Invoke-Python $Command @("-c", $code) *> $null
    return $LASTEXITCODE -eq 0
}

function Test-LocalBrowser {
    $candidates = @(
        $env:EMAIL_MANAGER_BROWSER,
        $env:CHROME_PATH,
        $env:EDGE_PATH,
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $true
        }
    }
    return $false
}

function Ensure-Dependencies($Command) {
    if ($SkipInstall) {
        return
    }

    if (-not (Test-Playwright $Command)) {
        Write-Step "First run: installing Python dependencies..."
        Invoke-Python $Command @("-m", "pip", "install", "--retries", "2", "--timeout", "15", "-r", (Join-Path $Root "requirements.txt"))
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed. Check the network and run the launcher again."
        }
    }

    if (-not (Test-LocalBrowser)) {
        Write-Step "Chrome/Edge was not found; installing fallback Chromium..."
        Invoke-Python $Command @("-m", "playwright", "install", "chromium")
        if ($LASTEXITCODE -ne 0) {
            Write-Step "Fallback Chromium installation failed. The server will still start, but GPT login may need Chrome/Edge."
        }
    }
}

function Start-EmailManager($Command) {
    New-Item -ItemType Directory -Force -Path $DataDir, $LogDir | Out-Null
    $stdout = Join-Path $LogDir "server.out.log"
    $stderr = Join-Path $LogDir "server.err.log"
    $arguments = @($Command.Prefix + @("app.py"))

    Write-Step "Starting the local server in the background..."
    "Starting at $(Get-Date -Format o)" | Set-Content -LiteralPath $stdout -Encoding UTF8
    "Starting at $(Get-Date -Format o)" | Set-Content -LiteralPath $stderr -Encoding UTF8

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Command.File
    $startInfo.Arguments = ConvertTo-ArgumentString $arguments
    $startInfo.WorkingDirectory = $Root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start the local server process."
    }

    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ASCII
}

if (Test-Server) {
    Update-PidFromPort
    Write-Step "The server is already running."
    if (-not $NoOpen) {
        Start-Process $Url
    }
    exit 0
}

$basePython = Get-PythonCommand
$python = Ensure-Venv $basePython
Ensure-Dependencies $python
Start-EmailManager $python

Write-Step "Waiting for the server to start..."
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Server) {
        Update-PidFromPort
        Write-Step "Started: $Url"
        if (-not $NoOpen) {
            Start-Process $Url
        }
        exit 0
    }
}

Write-Step "Startup timed out. Recent error log:"
$err = Join-Path $LogDir "server.err.log"
if (Test-Path -LiteralPath $err) {
    Get-Content -LiteralPath $err -Tail 30
}
exit 1
