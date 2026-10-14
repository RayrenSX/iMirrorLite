[CmdletBinding()]
param([Parameter(Mandatory)][string] $Serial, [string] $AdbPath)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (!$AdbPath) { $AdbPath = Join-Path $projectRoot '.tools\android-sdk\platform-tools\adb.exe' }
if (!(Test-Path -LiteralPath $AdbPath)) { throw 'ADB was not found. Pass -AdbPath.' }
$outputDirectory = Join-Path $projectRoot 'artifacts'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$result = & $AdbPath -s $Serial shell run-as com.rayren.imirrorlite cat files/diagnostics/session.log 2>&1
if ($LASTEXITCODE -ne 0) { throw "Cannot read application diagnostics: $result" }
$outputPath = Join-Path $outputDirectory 'android-session.log'
$result | Set-Content -LiteralPath $outputPath -Encoding utf8
Write-Host "Diagnostics: $outputPath"
