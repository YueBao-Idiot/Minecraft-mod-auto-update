$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$AppName = "MC模组更新助手"
$AppDir = Join-Path $Root $AppName
$DistDir = Join-Path $Root "build_app_dist"
$WorkDir = Join-Path $Root "build_app"
$Assets = Join-Path $Root "assets"
$Icon = Join-Path $Assets "app.ico"
$Manifest = Join-Path $Assets "app.manifest"

Write-Host "检查 PyInstaller..."
python -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 不可用，请先运行：python -m pip install pyinstaller"
}

if (-not (Test-Path $Icon)) {
    Write-Host "生成图标..."
    python tools\make_icon.py
}

Write-Host "开始打包便携版文件夹..."
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name $AppName `
    --icon $Icon `
    --manifest $Manifest `
    --add-data "$Assets;assets" `
    --paths $Root `
    --distpath $DistDir `
    --workpath $WorkDir `
    --specpath $WorkDir `
    "launcher.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败"
}

$BuiltDir = Join-Path $DistDir $AppName
$BuiltExe = Join-Path $BuiltDir "$AppName.exe"
if (-not (Test-Path $BuiltExe)) {
    throw "没有找到打包产物：$BuiltExe"
}

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
if (Test-Path (Join-Path $AppDir "_internal")) {
    Remove-Item -LiteralPath (Join-Path $AppDir "_internal") -Recurse -Force
}
Copy-Item -Path (Join-Path $BuiltDir "*") -Destination $AppDir -Recurse -Force

Copy-Item -LiteralPath (Join-Path $Root "README.md") -Destination (Join-Path $AppDir "README.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "LICENSE") -Destination (Join-Path $AppDir "LICENSE") -Force
$AppAssets = Join-Path $AppDir "assets"
if (Test-Path $AppAssets) {
    Remove-Item -LiteralPath $AppAssets -Recurse -Force
}
Copy-Item -LiteralPath $Assets -Destination $AppAssets -Recurse -Force

$InstanceDir = Join-Path $AppDir "instance"
New-Item -ItemType Directory -Force -Path (Join-Path $InstanceDir "mods") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstanceDir "config") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "output") | Out-Null

$InstanceReadme = Join-Path $InstanceDir "把mods放这里.txt"
if (-not (Test-Path $InstanceReadme)) {
    Set-Content -LiteralPath $InstanceReadme -Encoding UTF8 -Value @"
把要移植的源版本里的 mods 内容放进本目录的 mods 中，
把 config 内容放进本目录的 config 中。

也可以直接启动程序，选择外部的 .minecraft 或 versions/<版本> 文件夹。
源目录只读，程序不会修改它。
"@
}

$AppLauncher = Join-Path $AppDir "启动.bat"
$LauncherText = "@echo off`r`ncd /d `"%~dp0`"`r`nfor %%F in (`"*.exe`") do (`r`n    start `"`" `"%%~fF`"`r`n    exit /b 0`r`n)`r`necho Could not find the application executable.`r`npause`r`n"
Set-Content -LiteralPath $AppLauncher -Value $LauncherText -Encoding Ascii

Write-Host ""
Write-Host "打包完成：$AppDir"
$Exe = Join-Path $AppDir "$AppName.exe"
Write-Host "启动文件：$Exe"
Write-Host "大小：$([math]::Round((Get-Item -LiteralPath $Exe).Length / 1MB, 1)) MB（不含 _internal）"
