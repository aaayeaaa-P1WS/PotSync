# PotSync 自更新脚本（由 client/updater.py 渲染占位符后调用）
# 职责：等旧进程退出 → 复制并校验 → 顶替并校验 → 带工作目录重启 → 观察 → 失败回滚
# 参数全部来自渲染时替换的占位符（OLD_EXE / NEW_EXE / OLD_PID / LOG_PATH），运行时不需要传参。

$ErrorActionPreference = 'Continue'

$old = '@@OLD_EXE@@'
$new = '@@NEW_EXE@@'
$oldPid = @@OLD_PID@@
$log = '@@LOG_PATH@@'

function Say([string]$m) {
    $line = '{0:HH:mm:ss} {1}' -f (Get-Date), $m
    try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 } catch { }
}

function Fail([string]$m) {
    Say "FAILED: $m"
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($m, 'PotSync 更新', 'OK', 'Warning') | Out-Null
    } catch { }
    exit 1
}

function Hash([string]$path) {
    try { return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash } catch { return '' }
}

# 回滚：把 .bak 换回原位置，保证用户至少还有一个能用的旧版本
function Restore-Backup {
    $bak = "$old.bak"
    if (-not (Test-Path -LiteralPath $bak)) { return }
    try {
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue }
        Move-Item -LiteralPath $bak -Destination $old -Force
        Say '已回滚到旧版本（旧版本仍可正常使用）'
    } catch {
        Say "回滚失败：$($_.Exception.Message)（旧版本保留在 $bak）"
    }
}

Say "开始更新：$old -> $new（旧进程 PID $oldPid）"

$newHash = Hash $new
if (-not $newHash) {
    Fail "新版本文件读不出来：`n$new`n`n请到发布页重新下载。"
}

# 1. 只等自己那个旧进程退出（按 PID，最多 60 秒）
if ($oldPid -gt 0) {
    $proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($proc) {
        try { [void]$proc.WaitForExit(60000) } catch { }
        $proc.Refresh()
        if (-not $proc.HasExited) {
            Fail "旧版本 60 秒内没有退出，已取消本次更新。请完全退出 PotSync 后重新检查更新。"
        }
    }
}
# 文件句柄可能比进程晚一点释放：最多等 15 秒
$released = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $fs = [IO.File]::Open($old, 'Open', 'ReadWrite', 'None')
        $fs.Close()
        $released = $true
        break
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $released) {
    Fail "程序文件仍被占用，已取消本次更新。`n`n如果你的安全软件正在扫描它，稍等一会儿再试。"
}

# 2. 去掉网络来源标记，避免某些机器上 SmartScreen/杀软对首次运行更严格
try { Unblock-File -LiteralPath $new -ErrorAction SilentlyContinue } catch { }

# 3. 复制成同目录的 .new 并校验，复制失败时原程序完好无损
$tmp = "$old.new"
$copied = $false
for ($i = 1; $i -le 8; $i++) {
    try {
        Copy-Item -LiteralPath $new -Destination $tmp -Force
        if ((Hash $tmp) -eq $newHash) { $copied = $true; break }
        Say "复制后校验不一致（第 $i 次），重试"
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    } catch {
        Say "复制失败（第 $i 次）：$($_.Exception.Message)"
    }
    Start-Sleep -Seconds 1
}
if (-not $copied) {
    Fail "无法写入新版本文件（可能被安全软件拦截或磁盘不可写）。`n`n新版本已保留在：`n$new`n`n请关闭 PotSync 后手动运行它，或到发布页重新下载。"
}

# 4. 顶替：PS 5.1 的 Move-Item -Force 不覆盖已有文件，所以先改名留底
$bak = "$old.bak"
$replaced = $false
for ($i = 1; $i -le 8; $i++) {
    try {
        Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $old -Destination $bak -Force
        try {
            Move-Item -LiteralPath $tmp -Destination $old -Force
            $replaced = $true
        } catch {
            try { Move-Item -LiteralPath $bak -Destination $old -Force } catch { }
            throw
        }
        break
    } catch {
        Say "替换失败（第 $i 次）：$($_.Exception.Message)"
        Start-Sleep -Seconds 1
    }
}
if (-not $replaced) {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    Fail "无法替换程序文件（可能仍被占用）。`n`n新版本已保留在：`n$new`n`n请关闭 PotSync 后手动运行它。"
}

# 5. 顶替后校验：不一致说明写到一半坏了，回滚
$oldHash = Hash $old
if ($oldHash -ne $newHash) {
    Say "顶替后校验不一致（$oldHash != $newHash），回滚"
    Restore-Backup
    Fail "更新文件写入不完整，已回滚到旧版本。`n`n请重新检查更新，或到发布页手动下载。`n`n日志：$log"
}

# 6. 给安全软件一点扫描时间，然后带工作目录启动；失败时多等一会儿再试
Start-Sleep -Seconds 3
$dir = Split-Path -Parent $old
$started = $false
$waits = @(3, 6, 10, 15)
for ($i = 0; $i -lt $waits.Count; $i++) {
    if ($i -gt 0) { Start-Sleep -Seconds $waits[$i - 1] }
    try {
        $p = Start-Process -FilePath $old -WorkingDirectory $dir -PassThru
        Start-Sleep -Seconds 10
        if ($p -and -not $p.HasExited) {
            Say "启动成功（pid $($p.Id)）"
            $started = $true
            break
        }
        if ($p) { Say "新版本启动后立即退出（exit $($p.ExitCode)，第 $($i + 1) 次）" }
    } catch {
        Say "启动异常（第 $($i + 1) 次）：$($_.Exception.Message)"
    }
}

if (-not $started) {
    Restore-Backup
    Fail "新版本启动失败（常见原因：安全软件拦截了首次解压，或文件被占用）。`n`n已尝试回滚到旧版本。新版本保留在：`n$new`n`n可以手动运行它；若仍打不开，请到发布页重新下载。`n`n日志：$log"
}

# 7. 清理：删除备份、脚本自身、两周前的旧安装包
Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
try {
    Get-ChildItem -LiteralPath (Split-Path -Parent $new) -Filter 'PotSync-*.exe' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $new -and $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { }
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
