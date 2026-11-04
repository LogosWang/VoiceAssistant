# Puts the build tools on PATH for this PowerShell session: Rust, Node and, where there's no Visual
# Studio C++ compiler, WinLibs (mingw-w64). None of them needs administrator rights:
#   winget install --id OpenJS.NodeJS.LTS --scope user
#   winget install --id BrechtSanders.WinLibs.POSIX.UCRT --scope user
#   rustup-init.exe --default-host x86_64-pc-windows-gnu --profile minimal -y
$add = @("$env:USERPROFILE\.cargo\bin", "$env:ProgramFiles\nodejs")
$packages = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages"
foreach ($pattern in 'OpenJS.NodeJS.LTS*\node-*', 'BrechtSanders.WinLibs.POSIX.UCRT*\mingw64\bin') {
    $found = Get-ChildItem -Path "$packages\$pattern" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $add += $found.FullName }
}
$env:Path = (($add | Where-Object { Test-Path $_ }) -join ';') + ';' + $env:Path
