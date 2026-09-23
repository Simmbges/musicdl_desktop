$ErrorActionPreference = 'Stop'
$musicPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $musicPython)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show('Python environment missing. Please see README.md.', 'Music Library') | Out-Null
    exit 1
}
Start-Process -FilePath $musicPython -ArgumentList @('-X', 'utf8', ('"' + (Join-Path $PSScriptRoot 'app.py') + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
