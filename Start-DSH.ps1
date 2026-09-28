param([switch]$BuildOnly, [switch]$ForceBuild, [switch]$Safe, [switch]$Normal, [switch]$Manage)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:DSH_TAURI_ROOT = $root
$exe = Join-Path $root 'target\release\dsh-wsl-tauri.exe'
$stampPath = Join-Path $root 'ui-build.json'
$mutex = [Threading.Mutex]::new($false, 'Local\DSH-WSL-Tauri-Build')
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw '另一个窗口正在构建，请稍候。' }
    if (([int]$Safe + [int]$Normal + [int]$Manage) -gt 1) { throw '只能选择一种启动模式。' }
    $version = (Get-Content -LiteralPath (Join-Path $root 'tauri.conf.json') -Raw | ConvertFrom-Json).version
    function Get-SourceHash {
        $paths = @('Cargo.toml', 'Cargo.lock', 'build.rs', 'tauri.conf.json') | ForEach-Object { Join-Path $root $_ }
        foreach ($folder in @('src', 'ui', 'icons')) {
            $paths += @(Get-ChildItem -LiteralPath (Join-Path $root $folder) -Recurse -File | ForEach-Object { $_.FullName })
        }
        $entries = @($paths | Sort-Object | ForEach-Object {
            $_.Substring($root.Length + 1).Replace('\', '/') + ':' + (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash
        })
        $sha = [Security.Cryptography.SHA256]::Create()
        try { return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($entries -join "`n")))).Replace('-', '') }
        finally { $sha.Dispose() }
    }
    $sourceHash = Get-SourceHash
    $stamp = $null
    if (Test-Path -LiteralPath $stampPath) {
        try { $stamp = Get-Content -LiteralPath $stampPath -Raw | ConvertFrom-Json } catch { $stamp = $null }
    }
    $needsBuild = $ForceBuild -or -not (Test-Path -LiteralPath $exe) -or $stamp.shellVersion -ne $version -or $stamp.sourceSha256 -ne $sourceHash
    if (-not $needsBuild) { $needsBuild = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ne $stamp.exeSha256 }
    if ($needsBuild) {
        $running = @(Get-Process -Name 'dsh-wsl-tauri' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
        if ($running.Count -gt 0) { throw '请先关闭外壳窗口再构建，后端无需关闭。' }
        Write-Host "正在构建独立管理器 $version……"
        Push-Location $root
        try {
            & $env:ComSpec /d /c ('""{0}" > "{1}" 2>&1"' -f (Join-Path $root 'build.cmd'), (Join-Path $root 'build.log'))
            if ($LASTEXITCODE -ne 0) { throw '构建失败，请查看 build.log。' }
        } finally { Pop-Location }
        if ((Get-SourceHash) -ne $sourceHash) { throw '构建期间源码发生变化，请重新启动。' }
        $record = [ordered]@{shellVersion=$version; sourceSha256=$sourceHash; exeSha256=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash; builtAt=[DateTime]::UtcNow.ToString('o')}
        $temporary = $stampPath + '.tmp'
        [IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $stampPath -Force
    }
    if (-not $BuildOnly) {
        $mode = if ($Safe) { '--safe' } elseif ($Normal) { '--normal' } else { '--manage' }
        Start-Process -FilePath $exe -ArgumentList $mode -WorkingDirectory $root -WindowStyle Hidden
    }
} catch {
    Write-Host $_.Exception.Message
    if (-not $BuildOnly) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show($_.Exception.Message, 'DSH Tauri 启动失败') | Out-Null
    }
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
