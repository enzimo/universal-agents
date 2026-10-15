param([Parameter(Mandatory = $true)][ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$Distribution)

$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$entry = @(Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss | Get-ItemProperty | Where-Object DistributionName -eq $Distribution)
if ($entry.Count -ne 1) { throw 'Expected one installed distribution' }
$vhd = Join-Path $entry[0].BasePath 'ext4.vhdx'
if (-not (Test-Path -LiteralPath $vhd -PathType Leaf)) { throw 'Distribution VHD not found' }
Write-Output "Before bytes: $((Get-Item -LiteralPath $vhd).Length)"
$running = & rtk proxy wsl --list --running --quiet
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect running WSL distributions' }
if (($running -replace "`0", '' | ForEach-Object { $_.Trim() }) -contains $Distribution) {
    throw 'Stop the dedicated distribution after its runner is drained, then compact it'
}
if (-not $admin) { throw 'Offline VHD compaction requires an elevated Windows session' }
$script = Join-Path $env:TEMP ("ua-ci-compact-$([guid]::NewGuid()).txt")
try {
 @("select vdisk file=`"$vhd`"", 'compact vdisk', 'exit') | Set-Content -Encoding ASCII $script
 & rtk proxy diskpart /s $script
 if ($LASTEXITCODE -ne 0) { throw 'DiskPart failed' }
} finally { Remove-Item $script -ErrorAction SilentlyContinue }
Write-Output "After bytes: $((Get-Item -LiteralPath $vhd).Length)"
