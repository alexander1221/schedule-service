<#
.SYNOPSIS
    Install the scheduling service core on Windows (6 steps).

.DESCRIPTION
    Step 1: system checks (Python 3.12-3.14, CPU, memory)
    Step 2: virtual environment .venv
    Step 3: OR-Tools, Django, psycopg, openpyxl from requirements-lock.txt (SHA256 checked)
    Step 4: dev tools (pytest, ruff, mypy) - only with -Dev
    Step 5: database migrations, roles and sample data (generated locally)
    Step 6: verify_install.py

    This file is pure ASCII on purpose: Windows PowerShell 5.1 reads .ps1 files
    in the system code page, and non-ASCII characters break the parser.

.PARAMETER PythonBin
    Python command, e.g. "py -3.13" (default) or a full path to python.exe.

.PARAMETER Dev
    Also install pytest, ruff and mypy.

.PARAMETER NoDownload
    Do not create sample data in step 5 (migrations always run).

.PARAMETER SkipVenv
    Install into the currently active environment instead of .venv.

.PARAMETER Wheels
    Offline install: take packages only from this folder of wheels.

.PARAMETER OfflineBundle
    Download wheels for this machine into the given folder and exit.

.PARAMETER DebugMode
    Verbose pip output.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -PythonBin "py -3.12" -Dev
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Wheels .\wheels
#>
param(
    [string]$PythonBin = "py -3.13",
    [switch]$Dev,
    [switch]$NoDownload,
    [switch]$SkipVenv,
    [string]$Wheels = "",
    [string]$OfflineBundle = "",
    [switch]$DebugMode
)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Set-Location $Root
$Total = 6

function Write-Log  { param($m) Write-Host "[setup] $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "[setup] OK $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "[setup] ! $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "[setup] X $m" -ForegroundColor Red }
function Stop-WithError {
    param($m)
    Write-Err $m
    Write-Err "Re-run with -DebugMode for details; common problems: docs\TROUBLESHOOTING.md"
    exit 1
}
function Confirm-Step {
    param($q)
    $a = Read-Host "$q [y/N]"
    return ($a -eq "y" -or $a -eq "Y")
}
function Get-ShortError {
    param($Err)
    $msg = "$Err"
    if ($msg.Length -gt 200) { return $msg.Substring(0, 200) + "..." }
    return $msg
}

$PipQ = @("-q")
if ($DebugMode) { $PipQ = @("-v") }

# Split "py -3.13" into executable + arguments
$PyParts = $PythonBin.Trim() -split "\s+"
$PyExe = $PyParts[0]
$PyArgs = @()
if ($PyParts.Length -gt 1) { $PyArgs = $PyParts[1..($PyParts.Length - 1)] }

# ------------------------------------------------------------------ step 1
Write-Log "Step 1/${Total}: system checks"
$cmd = Get-Command $PyExe -ErrorAction SilentlyContinue
if (-not $cmd) {
    if ($PyExe -eq "py") {
        $cmd = Get-Command "python" -ErrorAction SilentlyContinue
        if ($cmd) { $PyExe = "python"; $PyArgs = @(); Write-Warn "py launcher not found, using 'python'" }
    }
}
if (-not $cmd) {
    Stop-WithError "Python not found. Install it: winget install -e --id Python.Python.3.13"
}
$PyVer = & $PyExe @PyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1
if ($LASTEXITCODE -ne 0) {
    Stop-WithError ("Cannot run '$PythonBin': " + (Get-ShortError $PyVer) + ". Installed versions: py -0p")
}
$PyVer = "$PyVer".Trim()
Write-Log "Python: $PythonBin ($PyVer)"
$minor = [int]($PyVer.Split(".")[1])
# 3.12 is the minimum because of Django 6.x (stage 4); 3.14 is the newest
# Python with ortools 9.15.6755 wheels on PyPI.
if (-not $PyVer.StartsWith("3.") -or $minor -lt 12 -or $minor -gt 14) {
    Write-Warn "Python 3.12-3.14 is required (3.13 recommended), found $PyVer."
    Write-Warn "  winget install -e --id Python.Python.3.13   then: setup.ps1 -PythonBin 'py -3.13'"
    if (-not (Confirm-Step "Continue anyway?")) { Stop-WithError "Stopped: unsupported Python version" }
}
if ($env:CONDA_PREFIX) {
    Write-Warn "Conda environment is active ($env:CONDA_PREFIX). venv on top of Conda may pick up its DLLs."
    if (-not (Confirm-Step "Continue? (recommended: conda deactivate and re-run)")) { Stop-WithError "Stopped because of active Conda" }
}
$ncpu = [Environment]::ProcessorCount
Write-Log "CPU cores: $ncpu (CP-SAT uses all of them; 8+ recommended)"
if ($ncpu -lt 4) { Write-Warn "Few cores: the solver will be noticeably slower than README targets" }
Write-Ok "Step 1/$Total passed"

# ------------------------------------------------------------------ offline bundle
if ($OfflineBundle -ne "") {
    Write-Log "Downloading wheels for this machine into $OfflineBundle"
    New-Item -ItemType Directory -Force -Path $OfflineBundle | Out-Null
    $out = & $PyExe @PyArgs -m pip download @PipQ --require-hashes -r requirements-lock.txt -d $OfflineBundle 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | Select-Object -Last 15 | ForEach-Object { Write-Err "  $_" }
        Stop-WithError "pip download failed (check network or PIP_INDEX_URL)"
    }
    if ($Dev) {
        $out = & $PyExe @PyArgs -m pip download @PipQ --require-hashes -r requirements-dev-lock.txt -d $OfflineBundle 2>&1
        if ($LASTEXITCODE -ne 0) { Stop-WithError "pip download (dev) failed" }
    }
    $n = (Get-ChildItem -Path $OfflineBundle -Filter *.whl).Count
    if ($n -lt 5) { Stop-WithError "Suspiciously few wheels downloaded: $n" }
    Write-Ok "Wheels: $n in $OfflineBundle. On the target machine: setup.ps1 -Wheels $OfflineBundle"
    exit 0
}

# ------------------------------------------------------------------ step 2
Write-Log "Step 2/${Total}: virtual environment"
if ($SkipVenv) {
    $Py = $PyExe
    $PyBase = $PyArgs
    Write-Warn "-SkipVenv: packages go into the current environment"
} else {
    $VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path ".venv") {
        $venvVer = "broken"
        if (Test-Path $VenvPy) {
            $venvVer = (& $VenvPy -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null)
            $venvVer = "$venvVer".Trim()
        }
        if ($venvVer -ne $PyVer) {
            Write-Warn ".venv was created for Python $venvVer, selected $PyVer"
            if (Confirm-Step "Delete .venv and create it again?") {
                Remove-Item -Recurse -Force ".venv"
            } else {
                Stop-WithError "Stopped: .venv belongs to another Python version"
            }
        } else {
            Write-Log ".venv already exists (Python $venvVer) - reusing it"
        }
    }
    if (-not (Test-Path ".venv")) {
        $out = & $PyExe @PyArgs -m venv .venv 2>&1
        if ($LASTEXITCODE -ne 0) {
            $out | Select-Object -Last 10 | ForEach-Object { Write-Err "  $_" }
            Stop-WithError "Cannot create .venv"
        }
    }
    if (-not (Test-Path $VenvPy)) { Stop-WithError ".venv has no Scripts\python.exe - delete .venv and re-run" }
    $Py = $VenvPy
    $PyBase = @()
}
& $Py @PyBase -m pip install @PipQ --upgrade "pip>=24" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warn "Could not upgrade pip - continuing with the bundled one" }
Write-Ok "Step 2/$Total passed"

# ------------------------------------------------------------------ steps 3-4
$PipSrc = @()
if ($Wheels -ne "") {
    if (-not (Test-Path $Wheels)) { Stop-WithError "Wheels folder not found: $Wheels" }
    $PipSrc = @("--no-index", "--find-links", $Wheels)
    Write-Log "Offline mode: packages only from $Wheels"
}

Write-Log "Step 3/${Total}: dependencies - OR-Tools, Django, psycopg, openpyxl (requirements-lock.txt, SHA256 checked)"
$out = & $Py @PyBase -m pip install @PipQ @PipSrc --require-hashes -r requirements-lock.txt 2>&1
if ($LASTEXITCODE -ne 0) {
    $out | Select-Object -Last 20 | ForEach-Object { Write-Err "  $_" }
    Stop-WithError "Dependency install failed. Without PyPI access set PIP_INDEX_URL to the internal mirror or use -Wheels"
}
$orv = & $Py @PyBase -c "import ortools; print(ortools.__version__)" 2>&1
$orv = "$orv".Trim()
if ($orv -ne "9.15.6755") { Stop-WithError ("ortools version after install: " + (Get-ShortError $orv) + ", expected 9.15.6755") }
Write-Ok "Step 3/$Total passed (ortools $orv)"

Write-Log "Step 4/${Total}: developer tools"
if ($Dev) {
    $out = & $Py @PyBase -m pip install @PipQ @PipSrc --require-hashes -r requirements-dev-lock.txt 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | Select-Object -Last 20 | ForEach-Object { Write-Err "  $_" }
        Stop-WithError "Cannot install pytest/ruff/mypy"
    }
    Write-Ok "Step 4/$Total passed (pytest, ruff, mypy)"
} else {
    Write-Log "  skipped (only needed for development: setup.ps1 -Dev)"
}

# ------------------------------------------------------------------ step 5
Write-Log "Step 5/${Total}: database and sample data (the project has no external downloads)"
$env:PYTHONIOENCODING = "utf-8"
$out = & $Py @PyBase manage.py migrate --noinput -v 0 2>&1
if ($LASTEXITCODE -ne 0) {
    $out | Select-Object -Last 15 | ForEach-Object { Write-Err "  $_" }
    Stop-WithError "Migrations failed. For PostgreSQL check DB_HOST / DB_USER / DB_PASSWORD"
}
$out = & $Py @PyBase manage.py init_roles 2>&1
if ($LASTEXITCODE -ne 0) { Stop-WithError "Cannot create roles" }
if ($NoDownload) {
    Write-Log "  sample data skipped (-NoDownload)"
} else {
    if (Test-Path "data\problem.json") {
        Write-Log "  data\problem.json already exists - not overwriting"
    } else {
        $out = & $Py @PyBase schedule_cli.py generate -o data\problem.json --planted data\planted.json 2>&1
        if ($LASTEXITCODE -ne 0) {
            $out | Select-Object -Last 15 | ForEach-Object { Write-Err "  $_" }
            Stop-WithError "Sample data generation failed"
        }
        $size = (Get-Item "data\problem.json").Length
        if ($size -lt 1024) { Stop-WithError "data\problem.json is suspiciously small ($size bytes)" }
    }
    $out = & $Py @PyBase manage.py seed_synthetic --if-empty 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | Select-Object -Last 15 | ForEach-Object { Write-Err "  $_" }
        Stop-WithError "Cannot load the synthetic semester into the database"
    }
}
Write-Ok "Step 5/$Total passed"

# ------------------------------------------------------------------ step 6
Write-Log "Step 6/${Total}: installation check"
$env:PYTHONIOENCODING = "utf-8"
& $Py @PyBase verify_install.py
if ($LASTEXITCODE -ne 0) { Stop-WithError "verify_install.py reported problems (see output above)" }
& $Py @PyBase -m pip freeze 2>$null | Out-File -Encoding ascii requirements-frozen.txt
Write-Log "Actual environment saved to requirements-frozen.txt"
Write-Ok "Step 6/$Total passed"

Write-Host ""
Write-Ok "Done. Next:"
Write-Host "    .venv\Scripts\activate"
Write-Host "    python manage.py createsuperuser"
Write-Host "    python manage.py runserver        (then open http://127.0.0.1:8000/admin/)"
Write-Host "    python manage.py solve_semester 1 --time 120 --variants 3"
