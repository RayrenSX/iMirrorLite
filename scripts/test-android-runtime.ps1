[CmdletBinding()]
param([Parameter(Mandatory)][string] $Serial, [string] $AdbPath, [switch] $ExternalDecoderHost)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (!$AdbPath) { $AdbPath = Join-Path $projectRoot '.tools\android-sdk\platform-tools\adb.exe' }
if (!(Test-Path -LiteralPath $AdbPath)) { throw 'Pass -AdbPath for your Android SDK platform-tools/adb.exe.' }
& $AdbPath -s $Serial install -r (Join-Path $projectRoot 'app\build\outputs\apk\debug\app-debug.apk')
if ($LASTEXITCODE -ne 0) { throw 'App installation failed.' }
& $AdbPath -s $Serial install -r -t (Join-Path $projectRoot 'app\build\outputs\apk\androidTest\debug\app-debug-androidTest.apk')
if ($LASTEXITCODE -ne 0) { throw 'Test package installation failed.' }
$runner='com.rayren.imirrorlite.test/com.rayren.imirrorlite.RuntimeSmokeTest'
if($ExternalDecoderHost) {
    $captureRoot=Join-Path $projectRoot 'artifacts'
    New-Item -ItemType Directory -Path $captureRoot -Force | Out-Null
    $outputFile=Join-Path $captureRoot ('instrumentation-foreground-'+[guid]::NewGuid().ToString('N')+'.log')
    $errorFile=$outputFile+'.stderr'
    $process=Start-Process -FilePath $AdbPath -ArgumentList @('-s',$Serial,'shell','am','instrument','-w','-e','externalHost','true',$runner) -PassThru -WindowStyle Hidden -RedirectStandardOutput $outputFile -RedirectStandardError $errorFile
    try {
        $deadline=[DateTime]::UtcNow.AddSeconds(60)
        $opened=$false
        while(!$process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
            if(!$opened -and (Get-Content -LiteralPath $outputFile -Raw -ErrorAction SilentlyContinue) -match 'EXTERNAL_DECODER_HOST_READY') {
                & $AdbPath -s $Serial shell am start -n 'com.rayren.imirrorlite/.DecoderTestActivity'
                if($LASTEXITCODE -ne 0) { throw 'Cannot open foreground decoder host.' }
                $opened=$true
            }
            Start-Sleep -Milliseconds 250
        }
        if(!$process.HasExited) { throw 'Foreground runtime check timed out.' }
        $process.WaitForExit()
        $result=@(Get-Content -LiteralPath $outputFile; Get-Content -LiteralPath $errorFile)
        $instrumentExit=$process.ExitCode
    } finally {
        if(!$process.HasExited) {
            & $AdbPath -s $Serial shell am force-stop 'com.rayren.imirrorlite'
            $process.WaitForExit(5000) | Out-Null
            if(!$process.HasExited) { $process.Kill() }
        }
    }
} else {
    $result = & $AdbPath -s $Serial shell am instrument -w $runner 2>&1
    $instrumentExit=$LASTEXITCODE
}
$result | ForEach-Object { Write-Host $_ }
if ($instrumentExit -ne 0 -or ($result -join "`n") -notmatch 'RUNTIME_SMOKE_OK') {
    throw 'The packaged Android runtime check failed.'
}
