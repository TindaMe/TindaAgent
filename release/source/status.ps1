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

$trackedSet = [System.Collections.Generic.HashSet[int]]::new()
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
    return ($lc.Contains("run_web.py") -or $lc.Contains("uvicorn") -or $lc.Contains("tindaagent.web.server") -or $lc.Contains("web.server"))
}

if (Test-Path $portsFile) {
    $lines = Get-Content -Path $portsFile -Encoding UTF8
    foreach ($line in $lines) {
        $tokens = ($line -replace "[,;]", " ").Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)
        foreach ($token in $tokens) {
            Add-PortToken -Token $token -Set $trackedSet
        }
    }
}

$envPorts = [string]$env:TINDA_ACTIVE_PORTS
if (-not [string]::IsNullOrWhiteSpace($envPorts)) {
    $tokens = ($envPorts -replace "[,;]", " ").Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)
    foreach ($token in $tokens) {
        Add-PortToken -Token $token -Set $trackedSet
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
