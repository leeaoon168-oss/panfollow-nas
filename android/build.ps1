# 不用 Gradle，直接用 SDK 的 aapt2 + javac + d8 + apksigner 打 APK
$ErrorActionPreference = 'Stop'

$SDK  = "F:\Users\Aoon\Documents\ChatGPT\WOA\build_tools\android-sdk"
$BT   = Join-Path $SDK "build-tools\35.0.0"
$AJ   = Join-Path $SDK "platforms\android-35\android.jar"
$proj = $PSScriptRoot
$gen  = Join-Path $proj "build"

foreach ($p in @($BT, $AJ)) { if (-not (Test-Path $p)) { throw "找不到 $p" } }

Write-Host "== 清理 =="
Remove-Item $gen -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $gen | Out-Null
New-Item -ItemType Directory (Join-Path $gen "classes") | Out-Null
New-Item -ItemType Directory (Join-Path $gen "dex") | Out-Null

Write-Host "== aapt2 compile =="
& "$BT\aapt2.exe" compile --dir (Join-Path $proj "res") -o (Join-Path $gen "res.zip")
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile 失败" }

Write-Host "== aapt2 link =="
& "$BT\aapt2.exe" link -o (Join-Path $gen "base.apk") -I $AJ `
    --manifest (Join-Path $proj "AndroidManifest.xml") `
    --min-sdk-version 24 --target-sdk-version 34 `
    --version-code 1 --version-name "1.0" `
    (Join-Path $gen "res.zip")
if ($LASTEXITCODE -ne 0) { throw "aapt2 link 失败" }

Write-Host "== javac =="
$srcs = Get-ChildItem (Join-Path $proj "java") -Recurse -Filter *.java | ForEach-Object { $_.FullName }
& javac --release 8 -encoding UTF-8 -nowarn -classpath $AJ -d (Join-Path $gen "classes") $srcs
if ($LASTEXITCODE -ne 0) { throw "javac 失败" }

Write-Host "== d8 =="
$classes = Get-ChildItem (Join-Path $gen "classes") -Recurse -Filter *.class | ForEach-Object { $_.FullName }
& "$BT\d8.bat" --lib $AJ --min-api 24 --output (Join-Path $gen "dex") $classes
if ($LASTEXITCODE -ne 0) { throw "d8 失败" }

Write-Host "== 打包 dex =="
Copy-Item (Join-Path $gen "base.apk") (Join-Path $gen "unsigned.apk") -Force
Push-Location (Join-Path $gen "dex")
try {
    & "$BT\aapt.exe" add "..\unsigned.apk" "classes.dex"
    if ($LASTEXITCODE -ne 0) { throw "aapt add 失败" }
} finally { Pop-Location }

Write-Host "== zipalign =="
& "$BT\zipalign.exe" -f 4 (Join-Path $gen "unsigned.apk") (Join-Path $gen "aligned.apk")
if ($LASTEXITCODE -ne 0) { throw "zipalign 失败" }

Write-Host "== 签名 =="
$ks = Join-Path $proj "kdocs-tracker.jks"
if (-not (Test-Path $ks)) {
    & keytool -genkeypair -v -keystore $ks -alias kdocs -keyalg RSA -keysize 2048 `
        -validity 10000 -storepass kdocs2026 -keypass kdocs2026 `
        -dname "CN=KDocs Tracker, OU=Home, O=Home, L=Local, S=Local, C=CN" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "keytool 失败" }
}

$out = Join-Path $proj "kdocs-tracker.apk"
& "$BT\apksigner.bat" sign --ks $ks --ks-key-alias kdocs `
    --ks-pass pass:kdocs2026 --key-pass pass:kdocs2026 `
    --out $out (Join-Path $gen "aligned.apk")
if ($LASTEXITCODE -ne 0) { throw "apksigner 失败" }

Write-Host "== 校验 =="
& "$BT\apksigner.bat" verify --print-certs $out | Select-Object -First 6
& "$BT\aapt.exe" dump badging $out | Select-String -Pattern "package:|application-label|sdkVersion|targetSdkVersion"

$size = (Get-Item $out).Length
Write-Host ""
Write-Host ("OK -> {0}  ({1:N0} KB)" -f $out, ($size / 1KB))
