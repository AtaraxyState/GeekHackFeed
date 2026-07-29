<#
Builds geekhack-feed.apk straight from the Android SDK tools -- no Gradle, no
Android Studio, nothing to download. Uses the SDK and JDK that ship with Unity
unless you point it somewhere else.

    .\build.ps1
    .\build.ps1 -Sdk "C:\path\to\Sdk" -Jdk "C:\path\to\jdk"
    .\build.ps1 -Install        # also push it to a connected phone
#>

param(
    [string]$Sdk = "",
    [string]$Jdk = "",
    [switch]$Install
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$build = Join-Path $root "build"

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# ---------------------------------------------------------------- toolchain

if (-not $Sdk -or -not $Jdk) {
    $unityRoot = "C:\Program Files\Unity\Hub\Editor"
    if (Test-Path $unityRoot) {
        $candidates = Get-ChildItem $unityRoot -Directory | Sort-Object Name -Descending
        foreach ($c in $candidates) {
            $ap = Join-Path $c.FullName "Editor\Data\PlaybackEngines\AndroidPlayer"
            if (Test-Path (Join-Path $ap "SDK\platform-tools")) {
                if (-not $Sdk) { $Sdk = Join-Path $ap "SDK" }
                if (-not $Jdk) { $Jdk = Join-Path $ap "OpenJDK" }
                break
            }
        }
    }
}

if (-not $Sdk -or -not (Test-Path $Sdk)) { Fail "Android SDK not found. Pass -Sdk <path>." }
if (-not $Jdk -or -not (Test-Path $Jdk)) { Fail "JDK not found. Pass -Jdk <path>." }

$buildTools = Get-ChildItem (Join-Path $Sdk "build-tools") -Directory |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $buildTools) { Fail "No build-tools under $Sdk" }

# Prefer a platform we have actually tested against.
$platform = $null
foreach ($p in @("android-34", "android-35", "android-36")) {
    $jar = Join-Path $Sdk "platforms\$p\android.jar"
    if (Test-Path $jar) { $platform = $jar; break }
}
if (-not $platform) { Fail "No android.jar under $Sdk\platforms" }

$aapt2    = Join-Path $buildTools.FullName "aapt2.exe"
$aapt     = Join-Path $buildTools.FullName "aapt.exe"
$d8       = Join-Path $buildTools.FullName "d8.bat"
$zipalign = Join-Path $buildTools.FullName "zipalign.exe"
$apksigner= Join-Path $buildTools.FullName "apksigner.bat"
$javac    = Join-Path $Jdk "bin\javac.exe"
$keytool  = Join-Path $Jdk "bin\keytool.exe"

foreach ($t in @($aapt2, $aapt, $d8, $zipalign, $apksigner, $javac, $keytool)) {
    if (-not (Test-Path $t)) { Fail "missing tool: $t" }
}

# d8 and apksigner are batch wrappers that shell out to java.
$env:JAVA_HOME = $Jdk
$env:PATH = (Join-Path $Jdk "bin") + ";" + $env:PATH

Write-Host "SDK         $Sdk"
Write-Host "build-tools $($buildTools.Name)"
Write-Host "platform    $(Split-Path (Split-Path $platform -Parent) -Leaf)"
Write-Host "JDK         $Jdk"

# -------------------------------------------------------------------- build

if (Test-Path $build) { Remove-Item $build -Recurse -Force }
New-Item -ItemType Directory -Path $build | Out-Null

Step "launcher icons"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($python) {
    & $python (Join-Path $root "make_icon.py")
    if ($LASTEXITCODE -ne 0) { Fail "icon generation failed" }
} else {
    Write-Host "  python not found - reusing existing icons" -ForegroundColor Yellow
    if (-not (Test-Path (Join-Path $root "res\mipmap-mdpi\ic_launcher.png"))) {
        Fail "no icons present and python unavailable to generate them"
    }
}

Step "compiling resources"
$resZip = Join-Path $build "res.zip"
& $aapt2 compile --dir (Join-Path $root "res") -o $resZip
if ($LASTEXITCODE -ne 0) { Fail "aapt2 compile failed" }

Step "linking resources"
$baseApk = Join-Path $build "base.apk"
$gen = Join-Path $build "gen"
New-Item -ItemType Directory -Path $gen | Out-Null
& $aapt2 link -o $baseApk -I $platform `
    --manifest (Join-Path $root "AndroidManifest.xml") `
    -R $resZip --java $gen `
    --min-sdk-version 24 --target-sdk-version 34 `
    --auto-add-overlay
if ($LASTEXITCODE -ne 0) { Fail "aapt2 link failed" }

Step "compiling java"
$classes = Join-Path $build "classes"
New-Item -ItemType Directory -Path $classes | Out-Null
$sources = @()
$sources += (Get-ChildItem (Join-Path $root "src") -Recurse -Filter *.java).FullName
$sources += (Get-ChildItem $gen -Recurse -Filter R.java).FullName
& $javac --release 11 -nowarn -classpath $platform -d $classes $sources
if ($LASTEXITCODE -ne 0) { Fail "javac failed" }

Step "dexing"
$dexDir = Join-Path $build "dex"
New-Item -ItemType Directory -Path $dexDir | Out-Null
$classFiles = (Get-ChildItem $classes -Recurse -Filter *.class).FullName
& $d8 --lib $platform --min-api 24 --output $dexDir $classFiles
if ($LASTEXITCODE -ne 0) { Fail "d8 failed" }

Step "packaging"
# aapt add names the entry by the path given, so add it from inside dex/.
Push-Location $dexDir
& $aapt add -f $baseApk "classes.dex" | Out-Null
$addFailed = $LASTEXITCODE -ne 0
Pop-Location
if ($addFailed) { Fail "could not add classes.dex to the apk" }

Step "aligning"
$aligned = Join-Path $build "aligned.apk"
& $zipalign -f 4 $baseApk $aligned
if ($LASTEXITCODE -ne 0) { Fail "zipalign failed" }

Step "signing"
$keystore = Join-Path $root "debug.keystore"
if (-not (Test-Path $keystore)) {
    Write-Host "  creating debug keystore"
    & $keytool -genkeypair -keystore $keystore -storepass android -keypass android `
        -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 `
        -dname "CN=geekhack feed, OU=personal, O=personal, C=FR"
    if ($LASTEXITCODE -ne 0) { Fail "keytool failed" }
}

$apk = Join-Path $root "geekhack-feed.apk"
& $apksigner sign --ks $keystore --ks-pass pass:android --key-pass pass:android `
    --out $apk $aligned
if ($LASTEXITCODE -ne 0) { Fail "apksigner failed" }

& $apksigner verify $apk
if ($LASTEXITCODE -ne 0) { Fail "signature verification failed" }

$size = "{0:N0} KB" -f ((Get-Item $apk).Length / 1KB)
Write-Host "`nBuilt $apk ($size)" -ForegroundColor Green

# ------------------------------------------------------------------ install

if ($Install) {
    Step "installing"
    $adb = Join-Path $Sdk "platform-tools\adb.exe"
    if (-not (Test-Path $adb)) { Fail "adb not found at $adb" }
    $devices = (& $adb devices) -split "`n" | Where-Object { $_ -match "\tdevice$" }
    if (-not $devices) { Fail "no device connected (enable USB debugging first)" }
    & $adb install -r $apk
    if ($LASTEXITCODE -ne 0) { Fail "adb install failed" }
    Write-Host "installed" -ForegroundColor Green
}
