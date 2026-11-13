<#
.SYNOPSIS
    Renders speech_sample.txt to a 16 kHz mono 16-bit PCM WAV with System.Speech.

.DESCRIPTION
    Dev-only helper for stt_probe.py. The text is read as UTF-8 and spoken by the
    installed voice whose name starts with -VoiceName (default: Microsoft Zira).
    The script fails if the result is shorter than 30 s or longer than 60 s.

.EXAMPLE
    powershell -NoProfile -File apps/backend/scripts/make_speech_sample.ps1 -Out test-results/live/sample.wav
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Out,

    [string]$TextFile = '',

    [string]$VoiceName = 'Microsoft Zira'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$MinSeconds = 30.0
$MaxSeconds = 60.0
$SampleRate = 16000
$BytesPerSecond = $SampleRate * 2  # mono, 16-bit

# .NET resolves relative paths against the process directory, not the PowerShell location.
$outPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)
if ([string]::IsNullOrEmpty($TextFile)) {
    $TextFile = Join-Path $PSScriptRoot 'speech_sample.txt'
}
$textPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($TextFile)

if (-not (Test-Path -LiteralPath $textPath -PathType Leaf)) {
    throw "Text file not found: $textPath"
}
$text = (Get-Content -LiteralPath $textPath -Raw -Encoding UTF8) -replace '\s+', ' '
$text = $text.Trim()
if ($text.Length -eq 0) {
    throw "Text file is empty: $textPath"
}

$outDir = Split-Path -Parent $outPath
if (-not (Test-Path -LiteralPath $outDir -PathType Container)) {
    New-Item -ItemType Directory -Path $outDir | Out-Null
}

Add-Type -AssemblyName System.Speech

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voice = $synth.GetInstalledVoices() |
        Where-Object { $_.Enabled -and $_.VoiceInfo.Name -like "$VoiceName*" } |
        Select-Object -First 1
    if ($null -eq $voice) {
        $names = ($synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }) -join ', '
        throw "Voice '$VoiceName' is not installed. Installed voices: $names"
    }
    $synth.SelectVoice($voice.VoiceInfo.Name)

    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        $SampleRate,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($outPath, $format)
    $synth.Speak($text)
}
finally {
    # Disposing finalises the WAV header and releases the file handle.
    $synth.Dispose()
}

# Duration from the 'data' chunk of the RIFF header.
$bytes = [System.IO.File]::ReadAllBytes($outPath)
$dataSize = $null
$pos = 12
while ($pos + 8 -le $bytes.Length) {
    $id = [System.Text.Encoding]::ASCII.GetString($bytes, $pos, 4)
    $size = [System.BitConverter]::ToUInt32($bytes, $pos + 4)
    if ($id -eq 'data') {
        $dataSize = [Math]::Min([double]$size, [double]($bytes.Length - $pos - 8))
        break
    }
    $pos += 8 + $size + ($size % 2)
}
if ($null -eq $dataSize) {
    throw "No data chunk found in $outPath"
}
$duration = $dataSize / $BytesPerSecond

Write-Output ("Voice:    {0}" -f $voice.VoiceInfo.Name)
Write-Output ("Output:   {0}" -f $outPath)
Write-Output ("Duration: {0:N2} s ({1} bytes)" -f $duration, $bytes.Length)

if ($duration -lt $MinSeconds -or $duration -gt $MaxSeconds) {
    throw ("Duration {0:N2} s is outside {1}-{2} s; edit speech_sample.txt" -f $duration, $MinSeconds, $MaxSeconds)
}
