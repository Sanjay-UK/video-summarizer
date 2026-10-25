# Run the web app on localhost with the Windows CPU environment (venv-win).
# Create the environment first (not tested on a real Windows machine yet):
#   py -3.11 -m venv venv-win
#   venv-win\Scripts\python -m pip install --require-hashes -r requirements\windows-cpu.lock
# RunPod is blocked for this run: an empty RUNPOD_API_KEY makes _api_key() raise before any request.
# The empty value takes precedence over .env (load_env_file uses setdefault). .env is not modified.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root "venv-win\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "venv-win not found. Create it from requirements\windows-cpu.lock first."
    exit 1
}
$env:RUNPOD_API_KEY = ""
& $Python app.py
