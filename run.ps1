# premium-factory launcher (Windows PowerShell / pwsh)
$ErrorActionPreference = "Stop"
$entry = Join-Path $PSScriptRoot "pfcli.py"
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 $entry @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python $entry @args
} else {
    Write-Error "Python 3 not found (install from python.org or 'winget install Python.Python.3')"
    exit 1
}
