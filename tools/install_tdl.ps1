param(
    [string]$Version = "v0.20.4"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if ($Version -notmatch '^v\d+\.\d+\.\d+$') { throw "Invalid release version." }
$architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
$suffix = switch ($architecture) {
    "X64" { "64bit" }
    "Arm64" { "arm64" }
    "X86" { "32bit" }
    default { throw "Unsupported architecture: $architecture" }
}
$asset = "tdl_Windows_$suffix.zip"
$destination = Join-Path $projectRoot ".cache\tools\tdl\$Version"
$executable = Join-Path $destination "tdl.exe"
if (Test-Path -LiteralPath $destination) {
    throw "Destination already exists. Preserve it or choose another version: $destination"
}
$downloadDir = Join-Path $projectRoot (".cache\tools\tdl-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
$release = "https://github.com/iyear/tdl/releases/download/$Version"
$checksums = Join-Path $downloadDir "tdl_checksums.txt"
$archive = Join-Path $downloadDir $asset

function Receive-ReleaseFile([string]$Uri, [string]$Target) {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Target -TimeoutSec 60
            return
        } catch {
            if ($attempt -eq 3) { throw }
            Write-Warning "Download attempt $attempt failed; retrying official GitHub release."
        }
    }
}

Receive-ReleaseFile "$release/tdl_checksums.txt" $checksums
Receive-ReleaseFile "$release/$asset" $archive
$entries = @(Get-Content -LiteralPath $checksums | Where-Object {
    $_ -match ('^[a-fA-F0-9]{64}\s+\*?' + [regex]::Escape($asset) + '$')
})
if ($entries.Count -ne 1) { throw "Expected exactly one checksum for $asset" }
$expected = ($entries[0] -split '\s+')[0]
$actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
if ($actual -ine $expected) { throw "SHA256 mismatch. The downloaded executable will not be run." }
Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $downloadDir "verified")
$verified = Join-Path $downloadDir "verified"
if (-not (Test-Path -LiteralPath (Join-Path $verified "tdl.exe"))) { throw "Release is missing tdl.exe" }
New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
Move-Item -LiteralPath $verified -Destination $destination
Copy-Item -LiteralPath $checksums -Destination $destination
& $executable version
if ($LASTEXITCODE -ne 0) { throw "tdl version check failed." }
Write-Host "Verified SHA256: $actual"
Write-Host "Installed: $executable"
