$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

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

$basePython = Get-PythonCommand

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating the local runtime..."
    Invoke-Python $basePython @("-m", "venv", $VenvDir)
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw "Failed to create the local runtime."
    }
}

$python = @{ File = $VenvPython; Prefix = @() }

Write-Host "Installing Python dependencies..."
Invoke-Python $python @("-m", "pip", "install", "--retries", "2", "--timeout", "15", "-r", (Join-Path $Root "requirements.txt"))
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

Write-Host "Installing fallback Chromium. If Chrome/Edge is already installed, failure here does not block the app."
Invoke-Python $python @("-m", "playwright", "install", "chromium")
Write-Host "Dependency installation finished."
