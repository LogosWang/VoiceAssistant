# Runs at the end of the installer, as the user. Nothing here may fail the install: each step is on its
# own, and what happened goes in ~/.carnot/logs/install.log.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$Server = $PSScriptRoot
$Home2 = if ($env:CARNOT_HOME) { $env:CARNOT_HOME } else { Join-Path $env:USERPROFILE '.carnot' }
New-Item -ItemType Directory -Force (Join-Path $Home2 'logs') | Out-Null
Start-Transcript -Path (Join-Path $Home2 'logs\install.log') -Force | Out-Null
$Py = Join-Path $Server '.venv\python.exe'
$env:PYTHONUTF8 = '1'

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;$env:APPDATA\npm;$env:USERPROFILE\.local\bin"
}
Refresh-Path

# where the VS Code extension finds the service (no byte order mark, it reads this as plain text)
[IO.File]::WriteAllText((Join-Path $Home2 'server_dir'), "$Server`n")

Push-Location $Server
try {
    & $Py carnot_agent.py seed
    & $Py -c "import shared; shared.ensure_persona()"
} catch { Write-Host "memory: $_" }
Pop-Location

# The build's own test install stops here: it only checks the files, and downloads nothing
if ($env:CARNOT_INSTALL_TEST) { Stop-Transcript | Out-Null; exit 0 }

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# The China edition carries this file; every edition falls back on the other route if the first one fails.
$MirrorFile = Join-Path $Server 'mirror.json'
$Registry = if (Test-Path $MirrorFile) { (Get-Content $MirrorFile -Raw | ConvertFrom-Json).registries[0] } else { $null }
$Fallback = 'https://registry.npmmirror.com'
$Bin = Join-Path $env:USERPROFILE '.local\bin'
$Claude = Join-Path $Bin 'claude.exe'

function Have-Claude { (Test-Path $Claude) -or [bool](Get-Command claude -ErrorAction SilentlyContinue) }

function Git-FromMirror($registry) {
    $list = Invoke-RestMethod "$registry/-/binary/git-for-windows/"
    $newest = $list | Where-Object { $_.name -match '^v\d+\.\d+\.\d+\.windows\.\d+/$' } |
        Sort-Object { [version](($_.name -replace '^v|/$', '') -replace '\.windows', '') } | Select-Object -Last 1
    # through a variable: in this PowerShell, a JSON array piped straight from the command stays one object
    $files = Invoke-RestMethod "$registry/-/binary/git-for-windows/$($newest.name)"
    $file = $files | Where-Object { $_.name -match '^Git-.*-64-bit\.exe$' } | Select-Object -First 1
    $setup = Join-Path $env:TEMP $file.name
    curl.exe -fsSL --ssl-revoke-best-effort --connect-timeout 20 --speed-time 60 --speed-limit 102400 --retry 1 -o $setup $file.url
    if ($LASTEXITCODE) { throw "download failed: $($file.url)" }
    Start-Process $setup -ArgumentList '/VERYSILENT', '/NORESTART', '/CURRENTUSER', '/SP-', '/NOCANCEL' -Wait
    Remove-Item $setup -ErrorAction SilentlyContinue
    Refresh-Path
}

function Git-FromWinget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'no winget' }
    winget install --id Git.Git --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    Refresh-Path
}

function Have-Git { [bool](Get-Command git -ErrorAction SilentlyContinue) }

# The China edition's build, pinned when the installer was made to the hash on registry.npmjs.org
function Claude-Pinned {
    Push-Location $Server
    try { & $Py carnot_agent.py install claude } finally { Pop-Location }
}

# The standard edition's second route: the npm package's native build, against the hash the mirror publishes.
function Claude-FromMirror($registry) {
    $name = '@anthropic-ai/claude-code-win32-x64'
    $version = (Invoke-RestMethod "$registry/@anthropic-ai/claude-code/latest").version
    $package = Invoke-RestMethod "$registry/$($name.Replace('/', '%2f'))/$version"
    $work = Join-Path $env:TEMP ("carnot-claude-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory $work | Out-Null
    try {
        $tgz = Join-Path $work 'claude.tgz'
        curl.exe -fsSL --ssl-revoke-best-effort --connect-timeout 20 --speed-time 60 --speed-limit 102400 --retry 1 -o $tgz $package.dist.tarball
        if ($LASTEXITCODE) { throw "download failed: $($package.dist.tarball)" }
        $hex = (Get-FileHash $tgz -Algorithm SHA512).Hash
        $bytes = [byte[]]::new($hex.Length / 2)
        for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($hex.Substring($i * 2, 2), 16) }
        $got = 'sha512-' + [Convert]::ToBase64String($bytes)
        if ($got -ne $package.dist.integrity) { throw 'the download does not match the published hash' }
        & "$env:WINDIR\System32\tar.exe" -xzf $tgz -C $work
        if ($LASTEXITCODE) { throw 'cannot unpack the download' }
        $exe = Get-ChildItem $work -Recurse -Filter 'claude.exe' | Select-Object -First 1
        if (-not $exe) { throw 'no claude.exe in the package' }
        New-Item -ItemType Directory -Force $Bin | Out-Null
        Copy-Item $exe.FullName $Claude -Force
        $user = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (($user -split ';') -notcontains $Bin) { [Environment]::SetEnvironmentVariable('Path', "$user;$Bin".TrimStart(';'), 'User') }
        Write-Host "Claude Code $version installed from $registry"
    } finally {
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}

function Claude-FromOfficial { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression }

if (-not (Have-Git)) {
    $routes = if ($Registry) { @({ Git-FromMirror $Registry }, { Git-FromWinget }) } else { @({ Git-FromWinget }, { Git-FromMirror $Fallback }) }
    foreach ($route in $routes) {
        try { & $route } catch { Write-Host "git: $_" }
        if (Have-Git) { break }
    }
}

if (-not (Have-Claude)) {
    $routes = if ($Registry) { @({ Claude-Pinned }, { Claude-FromOfficial }) } else { @({ Claude-FromOfficial }, { Claude-FromMirror $Fallback }) }
    foreach ($route in $routes) {
        try { & $route } catch { Write-Host "claude: $_" }
        if (Have-Claude) { break }
    }
}

try {
    $code = Get-Command code.cmd -ErrorAction SilentlyContinue
    $path = if ($code) { $code.Source } else { Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\bin\code.cmd' }
    if (Test-Path $path) { & $path --install-extension (Join-Path $Server 'carnot.vsix') --force }
} catch { Write-Host "vscode: $_" }

Stop-Transcript | Out-Null
exit 0
