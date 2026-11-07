#requires -Version 5.1
# ===========================================================================
#  SongForPrism launcher (PowerShell) - pick the Python interpreter that has maafw.
#  Detection order (probe each with "import maa.library", use the first that works):
#    1) every "python" on current PATH (e.g. conda base, if user installed maafw there)
#    2) fallback: the WorkBuddy-bundled interpreter found during task #11
#  If none works, show the install hint (official PyPI index REQUIRED).
#  This script does NOT self-elevate and does NOT trigger UAC.
#  All arguments are forwarded to agent\main.py (e.g. --check). Exit code is kept.
#  NOTE: Chinese hints live in UTF-8 files msg_install.txt / msg_admin.txt and are
#        shown via Get-Content -Encoding UTF8 (avoids console code-page breakage).
# ===========================================================================

$ErrorActionPreference = 'Continue'
chcp 65001 | Out-Null
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

Set-Location 'D:\DMM\scsp_MAA'

$py = $null
$candidates = @()
try { where.exe python 2>$null | ForEach-Object { $candidates += $_ } } catch {}
$candidates += 'C:\Users\Anorak\.workbuddy\binaries\python\versions\3.13.12\python.exe'

foreach ($p in $candidates) {
    if ($py) { break }
    if (-not (Test-Path $p)) { continue }
    & $p -c 'import maa.library' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $py = $p }
}

if (-not $py) {
    Write-Host ''
    Write-Host (Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'msg_install.txt'))
    Write-Host ''
    if (-not $env:SCSP_NO_PAUSE) { Read-Host 'Press Enter to exit' }
    exit 3
}

Write-Host "[INFO] Using Python: $py"
& $py -c "from maa.library import Library; print('[INFO] maafw version:', Library.version())"
Write-Host ''

# ---- Admin check (prompt only; NEVER self-elevate / NEVER trigger UAC) ----
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ''
    Write-Host (Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'msg_admin.txt'))
}

# ---- Run agent\main.py, forward ALL args, keep exit code ----
& $py 'agent\main.py' @args
$rc = $LASTEXITCODE
Write-Host ''
Write-Host "[DONE] exit code: $rc"
if (-not $env:SCSP_NO_PAUSE) { Read-Host 'Press Enter to exit' }
exit $rc
