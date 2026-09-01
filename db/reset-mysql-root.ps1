# ============================================================
#  重置 MySQL root 密码
#  用法：powershell -ExecutionPolicy Bypass -File .\db\reset-mysql-root.ps1
#
#  ⚠️ 副作用：重置后，其它使用同一个 MySQL 的项目（如 assignment 库）
#     也要同步修改各自配置里的密码，否则会连不上。
#
#  新密码由你在提示符里输入，只写进一个临时文件，执行完立即删除。
#  注意：本文件必须保存为 UTF-8 with BOM。
# ============================================================

$mysqlBase = "C:\Java\mysql-9.3.0-winx64"
$mysqld    = Join-Path $mysqlBase "bin\mysqld.exe"
$mysql     = Join-Path $mysqlBase "bin\mysql.exe"

if (-not (Test-Path $mysqld)) { Write-Host "找不到 mysqld.exe: $mysqld" -ForegroundColor Red; exit 1 }

Write-Host "[1/5] 停止正在运行的 MySQL ..." -ForegroundColor Cyan
Get-Process mysqld -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3
if (Get-Process mysqld -ErrorAction SilentlyContinue) {
    Write-Host "MySQL 未能停止，请手动关闭后重试。" -ForegroundColor Red
    exit 1
}
Write-Host "      已停止。" -ForegroundColor DarkGray

$sec  = Read-Host "请设置新的 MySQL root 密码" -AsSecureString
$sec2 = Read-Host "请再输入一次确认" -AsSecureString

$bstr  = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

$bstr2  = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec2)
$plain2 = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr2)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr2)

if ($plain -ne $plain2) { Write-Host "两次输入不一致，已取消。" -ForegroundColor Red; exit 1 }
if ([string]::IsNullOrEmpty($plain)) { Write-Host "密码不能为空，已取消。" -ForegroundColor Red; exit 1 }

# SQL 字符串里的单引号和反斜杠要转义
$escaped = $plain.Replace("\", "\\").Replace("'", "''")
$initFile = Join-Path $env:TEMP ("mysql-reset-" + [guid]::NewGuid().ToString("N") + ".sql")

try {
    Write-Host "[2/5] 生成临时初始化脚本 ..." -ForegroundColor Cyan
    $sql = "ALTER USER 'root'@'localhost' IDENTIFIED BY '$escaped';`nFLUSH PRIVILEGES;`n"
    [System.IO.File]::WriteAllText($initFile, $sql, (New-Object System.Text.UTF8Encoding($false)))

    Write-Host "[3/5] 以初始化模式启动 MySQL ..." -ForegroundColor Cyan
    $proc = Start-Process -FilePath $mysqld -ArgumentList "--init-file=`"$initFile`"" -PassThru -WindowStyle Hidden

    $ready = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 1
        $test = Test-NetConnection -ComputerName 127.0.0.1 -Port 3306 -WarningAction SilentlyContinue
        if ($test.TcpTestSucceeded) { $ready = $true; break }
    }
    if (-not $ready) { Write-Host "MySQL 启动超时，请检查 data 目录下的 .err 日志。" -ForegroundColor Red; exit 1 }
    Write-Host "      密码已重置。" -ForegroundColor DarkGray

    Write-Host "[4/5] 验证新密码 ..." -ForegroundColor Cyan
    $env:MYSQL_PWD = $plain
    & $mysql -u root -e "SELECT '密码重置成功' AS result;"
    if ($LASTEXITCODE -ne 0) { Write-Host "验证失败。" -ForegroundColor Red; exit 1 }

    Write-Host "[5/5] 完成。MySQL 正在运行，可以直接执行 init-db.ps1 导入数据库了。" -ForegroundColor Green
}
finally {
    if (Test-Path $initFile) { Remove-Item $initFile -Force -ErrorAction SilentlyContinue }
    Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
    $plain = $null; $plain2 = $null
}
