# The offline voice on Windows: speaks a UTF-8 text file with SAPI, then deletes it.
param([string]$Path)
Add-Type -AssemblyName System.Speech
$text = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
Remove-Item -LiteralPath $Path -ErrorAction SilentlyContinue
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$zh = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'zh-*' } | Select-Object -First 1
if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }
$s.Rate = 1
$s.Speak($text)
