# ============================================================
#  初始化 t040 数据库（房屋租赁系统）
#  用法：在 PowerShell 里执行  .\db\init-db.ps1
#  密码由你在提示符里输入，不会出现在命令行、历史记录或本文件中。
# ============================================================

$mysqlExe = "C:\Java\mysql-9.3.0-winx64\bin\mysql.exe"
$sqlFile  = Join-Path $PSScriptRoot "T040.sql"

if (-not (Test-Path $mysqlExe)) { Write-Host "找不到 mysql.exe：$mysqlExe" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $sqlFile))  { Write-Host "找不到 SQL 文件：$sqlFile"  -ForegroundColor Red; exit 1 }

$sec = Read-Host "请输入 MySQL root 密码" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$env:MYSQL_PWD = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

try {
    Write-Host "`n[1/3] 创建数据库 t040 ..." -ForegroundColor Cyan
    & $mysqlExe -u root -e "CREATE DATABASE IF NOT EXISTS t040 DEFAULT CHARACTER SET utf8mb4;"
    if ($LASTEXITCODE -ne 0) { Write-Host "创建失败——密码错误，或 MySQL 没启动。" -ForegroundColor Red; return }

    Write-Host "[2/3] 导入 T040.sql ..." -ForegroundColor Cyan
    # 用 cmd 做字节级重定向：PowerShell 的管道会按控制台代码页转码，破坏 UTF-8 中文
    cmd /c "`"$mysqlExe`" -u root --default-character-set=utf8mb4 t040 < `"$sqlFile`""
    if ($LASTEXITCODE -ne 0) { Write-Host "导入失败，见上方报错。" -ForegroundColor Red; return }

    Write-Host "[3/3] 校验导入结果 ..." -ForegroundColor Cyan
    & $mysqlExe -u root -e "SELECT TABLE_NAME AS 表名, TABLE_ROWS AS 行数 FROM information_schema.TABLES WHERE TABLE_SCHEMA='t040' ORDER BY TABLE_NAME;"
    & $mysqlExe -u root t040 -e "SELECT COUNT(*) AS 房屋记录数 FROM fangwuxinxi;"

    Write-Host "`n完成。" -ForegroundColor Green
}
finally {
    Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
}
