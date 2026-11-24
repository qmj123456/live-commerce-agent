param([int]$Port = 9333, [string]$Url = 'about:blank', [string]$Chrome = '')
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = 'python' }
$launcherArgs = @((Join-Path $PSScriptRoot 'start_chrome.py'), '--port', $Port, '--url', $Url)
if ($Chrome) { $launcherArgs += @('--chrome', $Chrome) }
& $pythonPath @launcherArgs
if ($LASTEXITCODE -ne 0) { throw 'Chrome 启动失败，详见上方错误。' }
