param(
    [string]$Mode = "--show"
)

$ErrorActionPreference = "SilentlyContinue"

function Show-Usage {
    Write-Output "Usage:"
    Write-Output "  status.bat --show"
    Write-Output "  status.bat --help"
}

if ($Mode -in @("--help", "-h")) {
    Show-Usage
    exit 0
}
if ($Mode -ne "--show") {
    Write-Output "[ERROR] unknown arg: $Mode"
    Show-Usage
    exit 2
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$portsFile = Join-Path $repoRoot ".tinda_ports.list"
$selfEnv = "windows"

$trackedSet = [System.Collections.Generic.HashSet[int]]::new()
$trackedForeign = [System.Collections.Generic.HashSet[string]]::new()
$listenSet = [System.Collections.Generic.HashSet[int]]::new()
$listenPids = @{}

function Add-PortToken {
    param(
        [string]$Token,
        [System.Collections.Generic.HashSet[int]]$Set
    )
    if ([string]::IsNullOrWhiteSpace($Token)) { return }
    $n = 0
    if (-not [int]::TryParse($Token, [ref]$n)) { return }
    if ($n -le 0 -or $n -gt 65535) { return }
    [void]$Set.Add($n)
}

function Parse-PortRecord {
    param([string]$Token)
    if ([string]::IsNullOrWhiteSpace($Token)) { return $null }
    $envTag = "legacy"
    $rawPort = $Token
    if ($Token.Contains(":")) {
        $parts = $Token.Split(":", 2)
        if ($parts.Count -eq 2 -and -not [string]::IsNullOrWhiteSpace($parts[0]) -and -not [string]::IsNullOrWhiteSpace($parts[1])) {
            $envTag = $parts[0].ToLowerInvariant()
            $rawPort = $parts[1]
        }
    }
    switch ($envTag) {
        "win" { $envTag = "windows" }
        "nt" { $envTag = "windows" }
        "gnu/linux" { $envTag = "linux" }
        "" { $envTag = "legacy" }
    }
    $n = 0
    if (-not [int]::TryParse($rawPort, [ref]$n)) { return $null }
    if ($n -le 0 -or $n -gt 65535) { return $null }
    return @{
        env  = $envTag
        port = $n
    }
}

function Is-LocalListening {
    param([int]$Port)
    try {
        $found = netstat -ano -p tcp | findstr /r /c:":$Port .*LISTENING"
        return -not [string]::IsNullOrWhiteSpace(($found -join ""))
    } catch {
        return $false
    }
}

function Add-ListenPair {
    param(
        [int]$Port,
        [int]$Pid
    )
    [void]$listenSet.Add($Port)
    if (-not $listenPids.ContainsKey($Port)) {
        $listenPids[$Port] = [System.Collections.Generic.HashSet[int]]::new()
    }
    [void]$listenPids[$Port].Add($Pid)
}

function Is-AgentProcess {
    param([int]$Pid)
    $cmd = ""
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$Pid" | Select-Object -ExpandProperty CommandLine)
    } catch {}
    if ([string]::IsNullOrWhiteSpace($cmd)) { return $false }
    $lc = $cmd.ToLowerInvariant()
    return ($lc.Contains("dist/web/server.js") -or $lc.Contains("src/web/server.ts"))
}

if (Test-Path $portsFile) {
    $lines = Get-Content -Path $portsFile -Encoding UTF8
    foreach ($line in $lines) {
        $tokens = ($line -replace "[,;]", " ").Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)
        foreach ($token in $tokens) {
            $rec = Parse-PortRecord -Token $token
            if ($null -eq $rec) { continue }
            $envTag = [string]$rec.env
            $port = [int]$rec.port
            if ($envTag -eq $selfEnv) {
                [void]$trackedSet.Add($port)
            } elseif ($envTag -eq "legacy") {
                if (Is-LocalListening -Port $port) {
                    [void]$trackedSet.Add($port)
                } else {
                    [void]$trackedForeign.Add(("{0}:{1}" -f $envTag, $port))
                }
            } else {
                [void]$trackedForeign.Add(("{0}:{1}" -f $envTag, $port))
            }
        }
    }
}

$envPorts = [string]$env:TINDA_ACTIVE_PORTS
if (-not [string]::IsNullOrWhiteSpace($envPorts)) {
    $tokens = ($envPorts -replace "[,;]", " ").Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)
    foreach ($token in $tokens) {
        $rec = Parse-PortRecord -Token $token
        if ($null -eq $rec) { continue }
        [void]$trackedSet.Add([int]$rec.port)
    }
}

$conns = @()
try {
    $conns = Get-NetTCPConnection -State Listen
} catch {
    $conns = @()
}

foreach ($conn in $conns) {
    $port = [int]$conn.LocalPort
    $pid = [int]$conn.OwningProcess
    if ($port -le 0 -or $port -gt 65535) { continue }
    if (Is-AgentProcess -Pid $pid) {
        Add-ListenPair -Port $port -Pid $pid
    }
}

$tracked = $trackedSet.ToArray() | Sort-Object
$listening = $listenSet.ToArray() | Sort-Object

if ($tracked.Count -eq 0) {
    Write-Output "[status] tracked: none"
} else {
    Write-Output ("[status] tracked: " + (($tracked | ForEach-Object { "$_" }) -join " "))
}

if ($listening.Count -eq 0) {
    Write-Output "[status] listening(agent): none"
} else {
    Write-Output ("[status] listening(agent): " + (($listening | ForEach-Object { "$_" }) -join " "))
    foreach ($p in $listening) {
        $pidText = (($listenPids[$p].ToArray() | Sort-Object | ForEach-Object { "$_" }) -join " ")
        Write-Output "[listen] port $p - pids $pidText"
    }
}

$trackedForeignArr = $trackedForeign.ToArray() | Sort-Object
if ($trackedForeignArr.Count -eq 0) {
    Write-Output "[status] tracked-foreign: none"
} else {
    Write-Output ("[status] tracked-foreign: " + (($trackedForeignArr | ForEach-Object { "$_" }) -join " "))
}

$orphan = @()
foreach ($p in $tracked) {
    if (-not $listenSet.Contains([int]$p)) {
        $orphan += [int]$p
    }
}

$untracked = @()
foreach ($p in $listening) {
    if (-not $trackedSet.Contains([int]$p)) {
        $untracked += [int]$p
    }
}

if ($orphan.Count -eq 0) {
    Write-Output "[status] orphan-tracked: none"
} else {
    Write-Output ("[status] orphan-tracked: " + (($orphan | Sort-Object | ForEach-Object { "$_" }) -join " "))
}

if ($untracked.Count -eq 0) {
    Write-Output "[status] untracked-listening: none"
} else {
    Write-Output ("[status] untracked-listening: " + (($untracked | Sort-Object | ForEach-Object { "$_" }) -join " "))
}

exit 0
