# PotSync 自更新脚本（由 client/updater.py 渲染占位符后调用）
# 职责：等旧进程退出 → 复制到 *.new → 顶替 → 带工作目录重启 → 观察 8 秒 → 失败弹框
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

Say "开始更新：$old -> $new（旧进程 PID $oldPid）"

# 1. 只等自己那个旧进程退出（按 PID，最多 60 秒）
if ($oldPid -gt 0) {
    $proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($proc) {
        try { [void]$proc.WaitForExit(60000) } catch { }
        $proc.Refresh()
        if (-not $proc.HasExited) {
            Fail "旧版本 60 秒内没有退出，已取消本次更新。请退出 PotSync 后重新检查更新。"
        }
    }
}
# 文件句柄可能比进程晚一点释放
for ($i = 0; $i -lt 20; $i++) {
    try {
        $fs = [IO.File]::Open($old, 'Open', 'ReadWrite', 'None')
        $fs.Close()
        break
    } catch { Start-Sleep -Milliseconds 400 }
}

# 2. 先复制成同目录的 .new，再顶替；复制失败时原程序完好无损
$tmp = "$old.new"
$copied = $false
for ($i = 1; $i -le 8; $i++) {
    try {
        Copy-Item -LiteralPath $new -Destination $tmp -Force
        $copied = $true
        break
    } catch {
        Say "复制失败（第 $i 次）：$($_.Exception.Message)"
        Start-Sleep -Seconds 1
    }
}
if (-not $copied) {
    Fail "无法写入新版本文件（可能被安全软件拦截或磁盘不可写）。`n`n新版本已保留在：`n$new`n`n请关闭 PotSync 后手动运行它，或到发布页重新下载。"
}

# PS 5.1 的 Move-Item -Force 不覆盖已存在文件，所以：旧文件改名留底 → 新文件就位 → 删底
$replaced = $false
for ($i = 1; $i -le 8; $i++) {
    $bak = "$old.bak"
    try {
        Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $old -Destination $bak -Force
        try {
            Move-Item -LiteralPath $tmp -Destination $old -Force
            $replaced = $true
        } catch {
            # 新文件没能就位，把旧文件还回去，保证程序还能用
            try { Move-Item -LiteralPath $bak -Destination $old -Force } catch { }
            throw
        }
        Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
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

# 3. 给安全软件一点扫描时间，然后带工作目录启动并观察 8 秒
Start-Sleep -Seconds 3
$dir = Split-Path -Parent $old
$started = $false
for ($i = 1; $i -le 3; $i++) {
    try {
        $p = Start-Process -FilePath $old -WorkingDirectory $dir -PassThru
        Start-Sleep -Seconds 8
        if ($p -and -not $p.HasExited) {
            Say "启动成功（pid $($p.Id)）"
            $started = $true
            break
        }
        if ($p) { Say "新版本启动后立即退出（exit $($p.ExitCode)，第 $i 次）" }
    } catch {
        Say "启动异常（第 $i 次）：$($_.Exception.Message)"
    }
    Start-Sleep -Seconds 2
}

if (-not $started) {
    Fail "新版本启动失败——常见原因是安全软件拦截了首次解压，或文件被占用。`n`n新版本已保留在：`n$new`n`n请手动运行它；若仍失败，请到发布页重新下载。`n`n日志：$log"
}

# 4. 清理：脚本自身 + 两周前的旧安装包
Start-Sleep -Seconds 1
try {
    Get-ChildItem -LiteralPath (Split-Path -Parent $new) -Filter 'PotSync-*.exe' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $new -and $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { }
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
