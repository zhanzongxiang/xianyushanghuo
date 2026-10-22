param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "config\test.local.json"),
    [switch]$Maintain
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $ConfigPath)) { return }
$appConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $appConfig.data_server_enabled -or $appConfig.data_server_backend -ne "mysql" -or
    $appConfig.data_server_db_host -ne "127.0.0.1" -or
    [int]$appConfig.data_server_db_port -ne 13306 -or $appConfig.data_server_db_tls -ne "disable") {
    return
}

$settingsPath = Join-Path $env:LOCALAPPDATA "XianYuMaterialPipeline\database-tunnel.json"
if (-not (Test-Path -LiteralPath $settingsPath)) {
    throw "Database tunnel settings are missing: $settingsPath"
}
$tunnel = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($tunnel.host -notmatch '^[A-Za-z0-9.-]+$' -or $tunnel.user -notmatch '^[A-Za-z0-9_-]+$' -or
    [int]$tunnel.port -lt 1 -or [int]$tunnel.port -gt 65535 -or
    [int]$tunnel.local_port -ne 13306 -or $tunnel.remote_host -ne "127.0.0.1" -or
    [int]$tunnel.remote_port -ne 3306) {
    throw "Invalid database tunnel settings."
}
foreach ($path in @($tunnel.key_path, $tunnel.known_hosts)) {
    if (-not $path -or $path -match '["\r\n]' -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Database tunnel key or known-hosts file is missing."
    }
}

function Get-TunnelProcess {
    $listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 13306 -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { return $null }
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener[0].OwningProcess)"
    if ($owner.Name -ne "ssh.exe" -or -not $owner.CommandLine.Contains($tunnel.key_path) -or
        -not $owner.CommandLine.Contains("127.0.0.1:13306:127.0.0.1:3306") -or
        -not $owner.CommandLine.Contains("$($tunnel.user)@$($tunnel.host)")) {
        throw "Port 13306 is already occupied by another process."
    }
    return Get-Process -Id $owner.ProcessId -ErrorAction Stop
}

$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$arguments = @(
    "-N", "-T", "-p", [string]$tunnel.port,
    "-i", "`"$($tunnel.key_path)`"",
    "-o", "`"UserKnownHostsFile=$($tunnel.known_hosts)`"",
    "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
    "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
    "-L", "127.0.0.1:13306:127.0.0.1:3306", "$($tunnel.user)@$($tunnel.host)"
)

if ($Maintain) {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutex = New-Object System.Threading.Mutex($false, "Local\XianYuDatabaseTunnel-13306-$identity")
    $ownsMutex = $false
    $process = $null
    try {
        try { $ownsMutex = $mutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
        if (-not $ownsMutex) { return }
        while ($true) {
            $current = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if (-not $current.data_server_enabled -or $current.data_server_backend -ne "mysql" -or
                $current.data_server_db_host -ne "127.0.0.1" -or
                [int]$current.data_server_db_port -ne 13306 -or $current.data_server_db_tls -ne "disable") {
                break
            }
            if ($process) {
                $process.Refresh()
                if ($process.HasExited) { $process.Dispose(); $process = $null }
            }
            if (-not $process) {
                $process = Get-TunnelProcess
                if (-not $process) {
                    $process = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru
                }
            }
            Start-Sleep -Seconds 3
        }
    } finally {
        if ($ownsMutex) {
            if ($process) {
                $process.Refresh()
                if (-not $process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
                $process.Dispose()
            }
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
    return
}

# One maintainer per Windows user restores SSH forwarding after a network interruption.
$null = Get-TunnelProcess
$supervisorArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
                    "-ConfigPath", "`"$((Resolve-Path -LiteralPath $ConfigPath).Path)`"", "-Maintain")
$supervisor = Start-Process -FilePath "powershell.exe" -ArgumentList $supervisorArgs -WindowStyle Hidden -PassThru
for ($attempt = 0; $attempt -lt 100; $attempt++) {
    $process = Get-TunnelProcess
    if ($process) {
        Write-Host "Database tunnel connected at 127.0.0.1:13306; automatic reconnect enabled."
        return
    }
    Start-Sleep -Milliseconds 200
}
throw "Database tunnel could not connect. It will retry while online database sync remains enabled."
