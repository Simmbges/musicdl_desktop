$ErrorActionPreference = 'Stop'
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..\musicdl-master')).Path
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction Stop).Source }

$buildArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', 'Shiyin', '--distpath', (Join-Path $PSScriptRoot 'dist'),
    '--workpath', (Join-Path $PSScriptRoot 'build'),
    '--specpath', $PSScriptRoot,
    '--paths', $source,
    '--collect-all', 'musicdl',
    '--add-data', ((Join-Path $source 'musicdl') + ';musicdl-master\musicdl'),
    (Join-Path $PSScriptRoot 'app.py')
)
& $python @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
Copy-Item -LiteralPath (Join-Path $source 'LICENSE') -Destination (Join-Path $PSScriptRoot 'dist\LICENSE-musicdl.txt') -Force
Write-Host (Join-Path $PSScriptRoot 'dist\Shiyin.exe')
