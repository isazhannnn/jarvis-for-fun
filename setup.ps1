$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PIP_CACHE_DIR = Join-Path $PSScriptRoot 'downloads\pip-cache'
$env:HF_HOME = Join-Path $PSScriptRoot 'downloads\huggingface'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv --system-site-packages '.venv' }
    else { & python -m venv --system-site-packages '.venv' }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required. Install Python and try again.' }
}
& '.\.venv\Scripts\python.exe' -c "import sys,os;from pathlib import Path;base=Path(sys.base_prefix+os.sep) if sys.base_prefix.endswith(':') else Path(sys.base_prefix);packages=base/'Lib/site-packages';target=Path(sys.prefix)/'Lib/site-packages/jarvis_system_dependencies.pth';target.write_text(str(packages.resolve())+'\n',encoding='utf-8') if packages.is_dir() else None"
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment setup failed.' }
if (Test-Path -LiteralPath 'downloads\wheels\pinned-requirements.txt') {
    & '.\.venv\Scripts\python.exe' -m pip install --no-index --find-links 'downloads\wheels' -r 'downloads\wheels\pinned-requirements.txt'
    if ($LASTEXITCODE -ne 0) { throw 'Offline speech dependency installation failed.' }
}
& '.\.venv\Scripts\python.exe' -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.\.venv\Scripts\python.exe' setup_runtime.py --brain --multilingual
if ($LASTEXITCODE -ne 0) { throw 'Model download failed; rerun setup.cmd.' }
& '.\.venv\Scripts\python.exe' main.py --check
Write-Host 'Ready. Open start.cmd to launch JARVIS.'
