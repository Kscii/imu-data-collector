$ErrorActionPreference = "Stop"

$releaseTag = "autobuild-2026-08-20-13-45"
$archiveName = "ffmpeg-n8.1.2-44-g7c533d0f86-win64-gpl-8.1.zip"
$expectedSha256 = "410c82fc0a7d713fd83412138271b8559faa8cf8a74a75eaf541dfca75ea4590"
$url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/$releaseTag/$archiveName"
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$archive = Join-Path $tempRoot $archiveName
$expanded = Join-Path $tempRoot "imu-ffmpeg"
$destination = Join-Path $PSScriptRoot "..\..\third_party\ffmpeg"

Invoke-WebRequest -Uri $url -OutFile $archive
$actual = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLowerInvariant()
if ($actual -ne $expectedSha256) {
    throw "FFmpeg SHA-256 不匹配：期望 $expectedSha256，实际 $actual"
}

Remove-Item $expanded -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $destination -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive -Path $archive -DestinationPath $expanded
$ffmpegExe = Get-ChildItem $expanded -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
if (-not $ffmpegExe) {
    throw "FFmpeg 归档中没有 ffmpeg.exe"
}
$sourceRoot = Split-Path (Split-Path $ffmpegExe.FullName -Parent) -Parent
New-Item $destination -ItemType Directory -Force | Out-Null
Copy-Item (Join-Path $sourceRoot "bin") $destination -Recurse
Get-ChildItem $sourceRoot -File | Where-Object {
    $_.Name -match "^(LICENSE|README|COPYING)"
} | Copy-Item -Destination $destination
