$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath 'config.json')) {
    Copy-Item -LiteralPath 'config.example.json' -Destination 'config.json'
}
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败' }
}
& ./.venv/Scripts/python.exe -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
& ./.venv/Scripts/python.exe -m docqa serve
