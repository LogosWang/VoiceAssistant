# Builds dist\Carnot-Setup.exe: everything inside, for a Windows PC with no developer tools.
# Build machine needs what env.ps1 lists, and internet. Downloads are cached in dist\windows-cache.
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1            the installer
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1 -StageOnly   just the service, in dist\windows\server
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1 -PackageOnly the installer from what's already staged
#   add -Mirror for dist\Carnot-Setup-CN.exe, which fetches Claude Code, pinned here to the official hash, and Git from mirrors
param([switch]$StageOnly, [switch]$PackageOnly, [switch]$Mirror)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. "$PSScriptRoot\env.ps1"
$Root = (Resolve-Path "$PSScriptRoot\..").Path
$Cache = "$Root\dist\windows-cache"
$Stage = "$Root\dist\windows"
$Server = "$Stage\server"
$PyVer = '3.13.18'
$PyTag = '20261026'
$PyName = "cpython-$PyVer+$PyTag-x86_64-pc-windows-msvc-install_only.tar.gz"
$PyBase = "https://github.com/astral-sh/python-build-standalone/releases/download/$PyTag"
$Speex = 'speexdsp-1.2.1'
$Kws = 'sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
$Asr = 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17'
$ModelBase = 'https://github.com/k2-fsa/sherpa-onnx/releases/download'
$out = if ($Mirror) { "$Root\dist\Carnot-Setup-CN.exe" } else { "$Root\dist\Carnot-Setup.exe" }

function Step($text) { Write-Host "`n==> $text" }
function Run($exe) {
    & $exe @args
    if ($LASTEXITCODE) { throw "$exe failed with exit code $LASTEXITCODE" }
}
function Fetch($url, $name) {
    $path = "$Cache\$name"
    if (-not (Test-Path $path)) {
        Run curl.exe -fsSL -o "$path.part" $url
        Move-Item "$path.part" $path
    }
    return $path
}
function Extension {
    Push-Location "$Root\vscode"
    $ErrorActionPreference = 'Continue'
    npx.cmd --yes "@vscode/vsce@3" package --no-dependencies --allow-missing-repository --skip-license -o "$Server\carnot.vsix"
    if ($LASTEXITCODE) { throw "vsce failed" }
    $ErrorActionPreference = 'Stop'
    Pop-Location
}
function CheckSum($path, $want) {
    $got = (Get-FileHash $path -Algorithm SHA256).Hash
    if (-not $want -or $got -ne $want.Trim().ToUpper()) { Remove-Item $path; throw "checksum mismatch: $path" }
}

$Py = "$Server\.venv\python.exe"
$Manifest = "$Stage\manifest.json"
if (-not $PackageOnly) {
New-Item -ItemType Directory -Force $Cache | Out-Null
if (Test-Path $Server) { Remove-Item -Recurse -Force $Server }
New-Item -ItemType Directory -Force $Server, "$Server\bin", "$Server\lib", "$Server\models" | Out-Null

Step 'Service code'
Copy-Item "$Root\server\*.py", "$Root\server\persona.md", "$Root\server\portrait.png", "$Root\server\config.toml",
          "$Root\server\requirements.txt", "$Root\server\say_win.ps1", "$PSScriptRoot\postinstall.ps1" $Server
Copy-Item "$Root\server\config.toml" "$Server\config.default.toml"  # an upgrade keeps the user's, and fills in from this
Copy-Item -Recurse "$Root\server\knowledge", "$Root\server\test_clips" $Server

Step "Python $PyVer, relocatable"
$sums = (Run curl.exe -fsSL "$PyBase/SHA256SUMS") -split "`n" | Where-Object { $_ -like "*$PyName" }
$archive = Fetch "$PyBase/$($PyName.Replace('+', '%2B'))" $PyName
CheckSum $archive (($sums -split '\s+')[0])
Run tar.exe -xzf $archive -C $Server
Rename-Item "$Server\python" '.venv'
$env:PIP_USER = $null
Run $Py -m pip install -q --only-binary ":all:" --no-warn-script-location -r "$Root\server\requirements.txt"
Get-ChildItem "$Server\.venv" -Recurse -Directory -Force |
    Where-Object { $_.Name -in '__pycache__', 'test', 'tests', 'idle_test', 'tkinter', 'ensurepip', 'include', 'tcl', 'share' } |
    ForEach-Object { if (Test-Path $_.FullName) { Remove-Item -Recurse -Force $_.FullName } }
Get-ChildItem "$Server\.venv" -Recurse -Force -File -Filter '*.pdb' | Remove-Item -Force  # debug symbols
Run $Py -I "$PSScriptRoot\verify.py" record $Server

Step 'Echo canceller'
$tar = Fetch "https://downloads.xiph.org/releases/speex/$Speex.tar.gz" "$Speex.tar.gz"
$src = "$Stage\speex"
if (Test-Path $src) { Remove-Item -Recurse -Force $src }
New-Item -ItemType Directory $src | Out-Null
Run tar.exe -xzf $tar -C $src
$dsp = "$src\$Speex"
@'
#ifndef SPEEXDSP_CONFIG_TYPES_H
#define SPEEXDSP_CONFIG_TYPES_H
#include <stdint.h>
typedef int16_t spx_int16_t;
typedef uint16_t spx_uint16_t;
typedef int32_t spx_int32_t;
typedef uint32_t spx_uint32_t;
#endif
'@ | Set-Content -Encoding ascii "$dsp\include\speex\speexdsp_config_types.h"
Run gcc -shared -O2 -static-libgcc -DFLOATING_POINT -DUSE_SMALLFT "-DEXPORT=" -DVAR_ARRAYS -I "$dsp\include" -I "$dsp\libspeexdsp" `
    "$dsp\libspeexdsp\mdf.c" "$dsp\libspeexdsp\preprocess.c" "$dsp\libspeexdsp\filterbank.c" "$dsp\libspeexdsp\fftwrap.c" "$dsp\libspeexdsp\smallft.c" `
    -o "$Server\lib\libspeexdsp.dll"

Step 'Audio helper'
Push-Location "$PSScriptRoot\audio"
Run cargo build --release --quiet
Pop-Location
Copy-Item "$PSScriptRoot\audio\target\release\carnot-audio.exe" "$Server\bin\"

Step 'FFmpeg (playback, camera, video frames)'
$ffzip = Fetch 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' 'ffmpeg-essentials.zip'
CheckSum $ffzip (Run curl.exe -fsSL 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256')
$ffdir = "$Stage\ffmpeg"
if (Test-Path $ffdir) { Remove-Item -Recurse -Force $ffdir }
Expand-Archive $ffzip $ffdir
Get-ChildItem $ffdir -Recurse -Include ffmpeg.exe, ffplay.exe, ffprobe.exe | Copy-Item -Destination "$Server\bin\"

Step 'Speech models'
foreach ($m in @(@('kws-models', $Kws), @('asr-models', $Asr))) {
    if (-not (Test-Path "$Cache\$($m[1])")) {
        $bz = Fetch "$ModelBase/$($m[0])/$($m[1]).tar.bz2" "$($m[1]).tar.bz2"
        Run tar.exe -xjf $bz -C $Cache
    }
}
$vad = Fetch "$ModelBase/asr-models/silero_vad.onnx" 'silero_vad.onnx'
New-Item -ItemType Directory -Force "$Server\models\$Kws", "$Server\models\$Asr" | Out-Null
foreach ($f in 'tokens.txt', 'en.phone', 'encoder-epoch-13-avg-2-chunk-16-left-64.onnx', 'decoder-epoch-13-avg-2-chunk-16-left-64.onnx', 'joiner-epoch-13-avg-2-chunk-16-left-64.onnx') {
    Copy-Item "$Cache\$Kws\$f" "$Server\models\$Kws\"
}
foreach ($f in 'tokens.txt', 'model.int8.onnx') { Copy-Item "$Cache\$Asr\$f" "$Server\models\$Asr\" }
Copy-Item $vad "$Server\models\"

Step 'Self-test on a recorded clip (no microphone, nothing sent to any AI)'
$test = "$Stage\test-config.toml"
(Get-Content -Encoding utf8 "$Root\server\config.toml") -replace '^enabled = true', 'enabled = false' | Set-Content -Encoding utf8 $test
$env:PYTHONUTF8 = '1'
$env:CARNOT_HOME = "$Stage\test-home"
$ErrorActionPreference = 'Continue'  # in this PowerShell, anything on a program's stderr would count as failure
$output = & $Py "$Server\voice_server.py" --wav "$Server\test_clips\command.wav" --dry-run --config $test 2>&1 | ForEach-Object { "$_" }
$ErrorActionPreference = 'Stop'
Remove-Item Env:\CARNOT_HOME
$output | Where-Object { $_ -match '\[wake\]|\[asr\]' }
if (-not ($output -match '\[wake\]')) { $output | Select-Object -Last 20; throw "the wake word wasn't detected in the self-test clip" }
Get-ChildItem $Server -Recurse -Directory -Force -Filter '__pycache__' | ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
Remove-Item -ErrorAction SilentlyContinue "$Server\models\keywords.generated.txt"

Step 'VS Code extension'
Extension

if ($StageOnly) { Write-Host "`nstaged in $Server"; exit 0 }
}
if (-not (Test-Path $Py)) { throw 'nothing staged yet: run without -PackageOnly first' }
# The test install below writes the same registry entries and shortcuts as a real install of Carnot on this computer,
# and its uninstall removes them. A real copy keeps running; its entries go back straight after the test. Should the
# build die in between, the next one puts them back first.
$Held = "$Stage\real-install"
$HeldKeys = @{ uninstall = 'HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Carnot'; folder = 'HKCU\Software\carnot' }
$HeldLinks = @{ programs = "$([Environment]::GetFolderPath('Programs'))\Carnot.lnk"; desktop = "$([Environment]::GetFolderPath('Desktop'))\Carnot.lnk" }
function Restore-RealInstall {
    if (-not (Test-Path $Held)) { return }
    foreach ($name in $HeldKeys.Keys) {
        if (Test-Path "$Held\$name.reg") {
            cmd /c "reg import `"$Held\$name.reg`" >nul 2>&1"
            if ($LASTEXITCODE) { throw "cannot put back $($HeldKeys[$name]): it is saved in $Held\$name.reg" }
        }
    }
    foreach ($name in $HeldLinks.Keys) {
        if (Test-Path "$Held\$name.lnk") { Copy-Item "$Held\$name.lnk" $HeldLinks[$name] -Force }
    }
    Remove-Item -Recurse -Force $Held
}
Restore-RealInstall
if ($PackageOnly) {  # the service's own code may have changed since it was staged
    Copy-Item "$Root\server\*.py", "$Root\server\say_win.ps1", "$Root\server\config.toml", "$PSScriptRoot\postinstall.ps1" $Server -Force
    Copy-Item "$Root\server\config.toml" "$Server\config.default.toml" -Force
    Step 'VS Code extension'
    Extension
}
if ($Mirror) {
    Step 'Claude Code mirrors'
    Run $Py -I -B "$Root\packaging\pin_mirror.py" "$Server\mirror.json"
} else { Remove-Item "$Server\mirror.json" -ErrorAction SilentlyContinue }
Get-ChildItem $Server -Recurse -Directory -Force -Filter '__pycache__' | ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
Remove-Item "$Server\models\keywords.generated.txt" -ErrorAction SilentlyContinue
Run $Py -I -B "$PSScriptRoot\verify.py" manifest $Server $Manifest

Step 'Carnot and the installer'
Push-Location "$PSScriptRoot\app"
$ErrorActionPreference = 'Continue'
# rustc on the GNU toolchain has been seen to crash at random here (0xc0000005); a crashed compile writes nothing
# All that's needed from Tauri is the app and the installer script; its own uncompressed package is thrown away
$started = Get-Date
# Tauri keeps compressed pictures here from one build to the next. A compile that crashed left one cut short, and
# every later build reused it, so the app showed a broken picture. It starts clean each time now.
Get-ChildItem "$PSScriptRoot\app\target\release\build" -Directory -Filter 'carnot-*' -ErrorAction SilentlyContinue | ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
$script = "$PSScriptRoot\app\target\release\nsis\x64\installer.nsi"
foreach ($try in 1..3) {
    npx.cmd --yes "@tauri-apps/cli@2" build
    if (-not $LASTEXITCODE -or ((Test-Path $script) -and (Get-Item $script).LastWriteTime -gt $started)) { break }
    if ($try -eq 3) { throw "tauri build failed" }
    Write-Host "tauri build failed, trying again"
}
$ErrorActionPreference = 'Stop'
Pop-Location
Step 'Comparing the pictures inside the exe with the originals'
$dump = "$Stage\asset-check"
if (Test-Path $dump) { Remove-Item -Recurse -Force $dump }
$env:CARNOT_HOME = "$Stage\test-home"  # or a running Carnot would answer for it
$exe = Start-Process "$PSScriptRoot\app\target\release\carnot.exe" -ArgumentList '--dump-assets', "`"$dump`"" -PassThru
Remove-Item Env:\CARNOT_HOME
$null = $exe.Handle
if (-not $exe.WaitForExit(30000)) { $exe.Kill(); throw 'carnot.exe --dump-assets did not finish' }
foreach ($file in Get-ChildItem "$Root\app\art") {
    $copy = "$dump\$($file.Name)"
    if (-not (Test-Path $copy) -or (Get-FileHash $copy).Hash -ne (Get-FileHash $file.FullName).Hash) { throw "the exe's copy of $($file.Name) is wrong: this build is bad, run the script again" }
}
Remove-Item -Recurse -Force $dump
Write-Host 'all pictures identical'

# Tauri writes the script, uncompressed, and the real installer is made here. A solid LZMA build came out unable
# to extract ("error decompressing data"), and LZMA's memory load kept crashing makensis on the build machine.
# zlib is about 70 MB bigger and needs a fraction of the memory; the file comparison below checks the result.
$nsisDir = "$PSScriptRoot\app\target\release\nsis\x64"
$text = [IO.File]::ReadAllText("$nsisDir\installer.nsi")
if (-not $text.Contains('SetCompress off')) { throw 'the installer script has changed: SetCompress off not found' }
$text = $text.Replace('SetCompress off', 'SetCompressor /SOLID "zlib"')
$text = [regex]::Replace($text, '!define OUTFILE\s+"[^"]*"', { param($m) "!define OUTFILE `"$out`"" })
# An upgrade goes straight over the old version, into the same folder: see hooks.nsh
$page = '(?m)^Function PageReinstall\r?$'
$folder = '!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive(\r?\n!insertmacro MUI_PAGE_DIRECTORY)'
if (-not ([regex]::IsMatch($text, $page) -and [regex]::IsMatch($text, $folder))) { throw 'the installer script has changed: its pages for an existing install not found' }
$text = [regex]::Replace($text, $page, "Function PageReinstall`n  Abort")
$text = [regex]::Replace($text, $folder, '!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfInstalled$1')
$text += "`nFunction SkipIfInstalled`n  ReadRegStr `$R1 SHCTX `"`${UNINSTKEY}`" `"UninstallString`"`n  `${IfThen} `$R1 != `"`" `${|} Abort `${|}`n  Call SkipIfPassive`nFunctionEnd`n"
[IO.File]::WriteAllText("$nsisDir\carnot.nsi", $text, (New-Object Text.UTF8Encoding $true))
Push-Location $nsisDir
Run "$env:LOCALAPPDATA\tauri\NSIS\makensis.exe" -V2 -INPUTCHARSET UTF8 -OUTPUTCHARSET UTF8 carnot.nsi
Pop-Location
Step 'Installing it for real, in a scratch folder, and comparing every file'
$scratch = "$Stage\install-test"
if (Test-Path $scratch) { cmd /c rmdir /s /q $scratch }
New-Item -ItemType Directory "$scratch\home" | Out-Null
New-Item -ItemType Directory -Force $Held | Out-Null
foreach ($name in $HeldKeys.Keys) { cmd /c "reg export $($HeldKeys[$name]) `"$Held\$name.reg`" /y >nul 2>&1" }
foreach ($name in $HeldLinks.Keys) { if (Test-Path $HeldLinks[$name]) { Copy-Item $HeldLinks[$name] "$Held\$name.lnk" } }
$saved = $env:USERPROFILE, $env:CARNOT_HOME
$env:USERPROFILE = "$scratch\home"; $env:CARNOT_HOME = "$scratch\home\.carnot"; $env:CARNOT_INSTALL_TEST = '1'
$setup = Start-Process $out -ArgumentList '/S', "/D=$scratch\app" -PassThru
$null = $setup.Handle
$setup.WaitForExit()
$failed = if ($setup.ExitCode) { "the installer failed with exit code $($setup.ExitCode)" }
if (-not $failed) {
    & $Py -I -B "$PSScriptRoot\verify.py" installed $Manifest "$scratch\app\server"
    if ($LASTEXITCODE) { $failed = 'files came out wrong' }
}
$uninstall = Start-Process "$scratch\app\uninstall.exe" -ArgumentList '/S' -PassThru
$null = $uninstall.Handle
$uninstall.WaitForExit()
foreach ($wait in 1..30) {  # the uninstaller carries on from a copy of itself
    if (-not (Test-Path "$scratch\app\server")) { break }
    Start-Sleep 2
}
$env:USERPROFILE, $env:CARNOT_HOME = $saved
Remove-Item Env:\CARNOT_INSTALL_TEST
Restore-RealInstall
if ($failed) { Remove-Item $out -ErrorAction SilentlyContinue; throw "$failed in the test install: this build is bad, run the script again" }
Get-Item $out | ForEach-Object { '{0}  {1:N0} MB' -f $_.FullName, ($_.Length / 1MB) }
