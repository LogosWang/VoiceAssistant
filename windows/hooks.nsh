; Added to the installer Tauri generates.

; An upgrade goes over the old version, which needs nothing from the user: build.ps1 takes out Tauri's page that
; would run the old uninstaller, since that removes the login item and the VS Code extension. Here the running copy
; quits, and anything run from the folder, as VS Code's chats are: on Windows it would hold its files open. Then the
; old service goes, so what this version doesn't ship can't stay behind, like the other edition's mirror list.
; Settings, sign-ins and conversations are in ~/.carnot, and keys in Credential Manager.
!macro NSIS_HOOK_PREINSTALL
  ${If} ${FileExists} "$INSTDIR\server\*.*"
    DetailPrint "正在退出旧版本的 Carnot..."
    System::Call 'Kernel32::SetEnvironmentVariable(t "CARNOT_OLD_DIR", t "$INSTDIR\")'
    nsExec::Exec '"$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "foreach ($$i in 1..10) { $$p = @(Get-CimInstance Win32_Process | Where-Object { $$_.ExecutablePath -and $$_.ExecutablePath.StartsWith($$env:CARNOT_OLD_DIR, [StringComparison]::OrdinalIgnoreCase) }); if (-not $$p.Count) { break }; $$p | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 500 }"'
    Pop $0
    DetailPrint "正在移除旧版本的文件..."
    RMDir /r "$INSTDIR\server"
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "正在配置 Carnot（首次可能要下载 Claude Code（约 150 MB）和 Git，请稍候）..."
  ; the installer is a 32-bit program: plain powershell.exe would be the 32-bit one, and Claude Code's
  ; own install script refuses to run there
  nsExec::ExecToLog '"$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\server\postinstall.ps1"'
  Pop $0
!macroend

; What the service created at run time inside the install folder, the login item and the VS Code extension.
; Memory, settings and conversations in ~/.carnot stay: whether to delete them is the user's call.
!macro NSIS_HOOK_POSTUNINSTALL
  RMDir /r "$INSTDIR\server"
  ReadEnvStr $1 CARNOT_INSTALL_TEST  ; the build's test install: a real copy of Carnot may be in use on that computer
  ${If} $UpdateMode <> 1  ; an upgrade runs this too, and must keep the user's choice
  ${AndIf} $1 == ""
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Carnot"
    DeleteRegKey HKCU "Software\carnot"  ; the remembered install folder, or the next install would reuse it
    nsExec::Exec 'cmd.exe /c code --uninstall-extension carnot-local.carnot'
    Pop $0
  ${EndIf}
!macroend
