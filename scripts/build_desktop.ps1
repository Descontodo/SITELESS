$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

$DependencyCheck = "import fastapi, uvicorn, httpx, bs4, pandas, openpyxl, playwright, pydantic, ddgs, stripe, dotenv, PyInstaller"
& $Python -c $DependencyCheck 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install -r requirements.txt -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar dependências de build." }
}

$BuildRoot = Join-Path $ProjectRoot "work\desktop-build"
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "work"
$SpecRoot = Join-Path $BuildRoot "spec"
$PackageDir = Join-Path $DistRoot "Siteless"
$OutputDir = Join-Path $ProjectRoot "outputs"
$ZipPath = Join-Path $OutputDir "Siteless-Windows-x64.zip"

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
& $Python -m PyInstaller --noconfirm --clean --onedir --console `
    --name Siteless --distpath $DistRoot --workpath $WorkRoot --specpath $SpecRoot `
    --collect-all ddgs `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.asyncio `
    --hidden-import uvicorn.protocols.http.h11_impl `
    --hidden-import uvicorn.lifespan.on `
    desktop.py
if ($LASTEXITCODE -ne 0) { throw "Falha ao empacotar o Siteless." }

Copy-Item -LiteralPath (Join-Path $ProjectRoot "README-DESKTOP.txt") -Destination (Join-Path $PackageDir "LEIA-ME.txt") -Force
Compress-Archive -Path $PackageDir -DestinationPath $ZipPath -Force
Write-Output "Pacote criado: $ZipPath"
