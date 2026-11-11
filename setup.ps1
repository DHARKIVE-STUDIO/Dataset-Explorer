# Dataset Explorer setup and launch script for Windows PowerShell.
# Run from anywhere: .\setup.ps1   (or: powershell -ExecutionPolicy Bypass -File setup.ps1)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Allow scripts in this window only. Nothing system-wide changes.
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python was not found. Install Python 3.11+ from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "Tick 'Add Python to PATH' during setup, then open a new PowerShell window." -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"))) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "Installing requirements..." -ForegroundColor Cyan
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r backend\requirements.txt

Write-Host ""
Write-Host "Starting Dataset Explorer at http://localhost:8000" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop the server." -ForegroundColor Yellow
Write-Host ""

Set-Location -Path ".\backend"
& $venvPython -m uvicorn app:app --reload --port 8000
