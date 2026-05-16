# install_oxidation_buildtools.ps1
#
# Installs the C/C++ build environment needed to compile llama-cpp-rs
# (the m3-embed-llamacpp 'embedded' feature) on this machine:
#   - CMake            (llama.cpp's build system)
#   - VS 2022 Build Tools + VCTools workload (the MSVC C/C++ compiler)
#
# CUDA is intentionally NOT installed here. A CPU-only llama.cpp build is
# the first milestone; GPU can be layered on afterward.
#
# RUN THIS IN AN ELEVATED (Administrator) PowerShell. Idempotent - safe to
# re-run; choco skips packages already present.
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 files in the
# system ANSI codepage, so non-ASCII chars (em-dashes etc.) break parsing.

$ErrorActionPreference = 'Stop'

# --- Guard: must be elevated --------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Not elevated. Re-run this script from an Administrator PowerShell."
    exit 1
}

# --- Guard: choco present ----------------------------------------------------
if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    Write-Error "Chocolatey not found on PATH. Install choco first, then re-run."
    exit 1
}
Write-Host "choco $(choco --version)" -ForegroundColor Cyan

# --- 1. CMake ----------------------------------------------------------------
Write-Host "`n=== Installing CMake ===" -ForegroundColor Green
choco install cmake --install-arguments '"ADD_CMAKE_TO_PATH=System"' -y --no-progress
if ($LASTEXITCODE -ne 0) { Write-Error "CMake install failed (exit $LASTEXITCODE)"; exit 1 }

# --- 2. VS 2022 Build Tools + VCTools ----------------------------------------
# VCTools = the MSVC C/C++ compiler + Windows SDK. --includeRecommended pulls
# the matching SDK and ATL/MFC bits llama.cpp's CMake config expects.
Write-Host "`n=== Installing VS 2022 Build Tools (VCTools workload) ===" -ForegroundColor Green
Write-Host "This is a large download (multiple GB) and may take 10-30 min." -ForegroundColor Yellow
choco install visualstudio2022buildtools `
    --package-parameters "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --quiet --norestart" `
    -y --no-progress
if ($LASTEXITCODE -ne 0) { Write-Error "Build Tools install failed (exit $LASTEXITCODE)"; exit 1 }

# --- 3. LLVM (libclang) ------------------------------------------------------
# llama-cpp-sys-2's build script runs bindgen, which hard-requires
# libclang.dll. MSVC + CMake alone are not enough. This installs LLVM and
# pins LIBCLANG_PATH (machine-wide) so bindgen finds the DLL deterministically.
Write-Host "`n=== Installing LLVM (provides libclang for bindgen) ===" -ForegroundColor Green
choco install llvm -y --no-progress
if ($LASTEXITCODE -ne 0) { Write-Error "LLVM install failed (exit $LASTEXITCODE)"; exit 1 }

$llvmBin = "C:\Program Files\LLVM\bin"
if (Test-Path (Join-Path $llvmBin "libclang.dll")) {
    [Environment]::SetEnvironmentVariable("LIBCLANG_PATH", $llvmBin, "Machine")
    $env:LIBCLANG_PATH = $llvmBin
    Write-Host "  LIBCLANG_PATH set (machine) -> $llvmBin" -ForegroundColor Cyan
} else {
    Write-Host "  libclang.dll not at $llvmBin - LLVM layout may differ; check before building." -ForegroundColor Yellow
}

# --- 4. Verify ---------------------------------------------------------------
Write-Host "`n=== Verifying ===" -ForegroundColor Green

# CMake - refresh PATH for this session so the check works immediately.
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
$cmake = Get-Command cmake -ErrorAction SilentlyContinue
if ($cmake) {
    Write-Host ("  cmake : OK  -> {0}" -f (cmake --version | Select-Object -First 1)) -ForegroundColor Cyan
} else {
    Write-Host "  cmake : NOT on PATH yet - a new shell will pick it up." -ForegroundColor Yellow
}

# MSVC - locate via vswhere (installed alongside Build Tools).
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (Test-Path $vswhere) {
    $vsPath = & $vswhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath
    if ($vsPath) {
        Write-Host "  MSVC  : OK  -> $vsPath" -ForegroundColor Cyan
        $clGlob = Get-ChildItem -Path "$vsPath\VC\Tools\MSVC" -Filter cl.exe -Recurse -ErrorAction SilentlyContinue |
                  Select-Object -First 1
        if ($clGlob) { Write-Host "          cl.exe -> $($clGlob.FullName)" -ForegroundColor Cyan }
    } else {
        Write-Host "  MSVC  : Build Tools installed but VCTools component not detected." -ForegroundColor Yellow
    }
} else {
    Write-Host "  MSVC  : vswhere not found - install may be incomplete." -ForegroundColor Yellow
}

# libclang - the piece that was missing for bindgen.
$libclang = Join-Path $llvmBin "libclang.dll"
if (Test-Path $libclang) {
    Write-Host "  libclang : OK  -> $libclang" -ForegroundColor Cyan
} else {
    Write-Host "  libclang : NOT found at $llvmBin - check LLVM install." -ForegroundColor Yellow
}

Write-Host "`nDone. Open a NEW terminal (or 'Developer PowerShell for VS 2022')" -ForegroundColor Green
Write-Host "so the updated PATH + MSVC env + LIBCLANG_PATH are picked up, then tell Claude to continue." -ForegroundColor Green
