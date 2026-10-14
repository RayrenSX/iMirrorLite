[CmdletBinding()]
param(
    [string[]] $Tasks = @(':app:assembleDebug', ':app:lintDebug'),
    [string] $JavaHome,
    [string] $AndroidSdk,
    [string] $GradlePath
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$toolsRoot = Join-Path $projectRoot '.tools'

if (!$JavaHome) {
    $portableJdk = Get-ChildItem -LiteralPath $toolsRoot -Directory -Filter 'jdk-17*' -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\java.exe') } |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($portableJdk) { $JavaHome = $portableJdk.FullName }
    elseif ($env:JAVA_HOME) { $JavaHome = $env:JAVA_HOME }
}
if (!$JavaHome -or !(Test-Path -LiteralPath (Join-Path $JavaHome 'bin\javac.exe'))) {
    throw 'JDK 17 is required. Put a JDK under .tools/jdk-17*, set JAVA_HOME, or pass -JavaHome.'
}

if (!$AndroidSdk) {
    $portableSdk = Join-Path $toolsRoot 'android-sdk'
    if (Test-Path -LiteralPath (Join-Path $portableSdk 'platforms\android-35\android.jar')) { $AndroidSdk = $portableSdk }
    elseif ($env:ANDROID_HOME) { $AndroidSdk = $env:ANDROID_HOME }
    elseif ($env:ANDROID_SDK_ROOT) { $AndroidSdk = $env:ANDROID_SDK_ROOT }
    elseif (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'Android\Sdk')) {
        $AndroidSdk = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
    }
}
if (!$AndroidSdk -or !(Test-Path -LiteralPath (Join-Path $AndroidSdk 'platforms\android-35\android.jar'))) {
    throw 'Android SDK Platform 35 is required. Put the SDK under .tools/android-sdk, set ANDROID_HOME, or pass -AndroidSdk.'
}

if (!$GradlePath) {
    $portableGradle = Join-Path $toolsRoot 'gradle-8.9\bin\gradle.bat'
    if (Test-Path -LiteralPath $portableGradle) { $GradlePath = $portableGradle }
    else { $GradlePath = Join-Path $projectRoot 'gradlew.bat' }
}
if (!(Test-Path -LiteralPath $GradlePath)) {
    throw 'Gradle was not found. Restore gradlew.bat, install Gradle 8.9 under .tools, or pass -GradlePath.'
}

# Keep caches and SDK configuration local to this checkout. Restore process
# variables even when Gradle fails; no user or system environment is modified.
$environmentNames = @('JAVA_HOME', 'ANDROID_HOME', 'ANDROID_SDK_ROOT', 'GRADLE_USER_HOME', 'PATH')
$previousEnvironment = @{}
foreach ($name in $environmentNames) { $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
Push-Location $projectRoot
try {
    $env:JAVA_HOME = [IO.Path]::GetFullPath($JavaHome)
    $env:ANDROID_HOME = [IO.Path]::GetFullPath($AndroidSdk)
    $env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
    $env:GRADLE_USER_HOME = Join-Path $toolsRoot 'gradle-user-home'
    $env:PATH = (Join-Path $env:JAVA_HOME 'bin') + [IO.Path]::PathSeparator + $env:PATH

    Write-Host "JDK: $env:JAVA_HOME"
    Write-Host "Android SDK: $env:ANDROID_HOME"
    & $GradlePath --no-daemon --console=plain @Tasks
    if ($LASTEXITCODE -ne 0) { throw "Gradle failed with exit code $LASTEXITCODE." }

    $debugApk = Join-Path $projectRoot 'app\build\outputs\apk\debug\app-debug.apk'
    if (($Tasks -contains ':app:assembleDebug' -or $Tasks -contains 'assembleDebug') -and (Test-Path -LiteralPath $debugApk)) {
        Write-Host "APK: $debugApk"
    }
}
finally {
    Pop-Location
    foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process') }
}
