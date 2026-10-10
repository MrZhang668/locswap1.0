# 重启本项目的后端 dev server。
#
# 存在的理由很实际：这个 uvicorn 是不带 --reload 起的，改完 Python 文件它不会重新
# 加载。2026-09-22 那天连续四轮改动全都没生效，四次复测看到的都是五天前的进程——
# 每次都要花十分钟才确认「代码是对的，只是没跑」。
#
# 它也是给 Claude 的**权限边界**：`.claude/settings.local.json` 放行的是这个脚本，
# 不是 `Stop-Process`。区别在于，这里只会停掉命令行里同时含有 uvicorn、app.main:app
# 和本项目端口的那些 python 进程——别的 python 一个都碰不到。
#
#     powershell -NoProfile -File scripts/restart_backend.ps1
#     powershell -NoProfile -File scripts/restart_backend.ps1 -NoReload   # 按老样子起
#
# **这个文件必须存成 UTF-8 with BOM。** Windows PowerShell 5.1 没有 BOM 就按
# 系统 ANSI 代码页读，注释里的中文会变成乱码并触发 ParserError——脚本跑不起来，
# 报的却是「missing closing }」，看上去像是语法写错了。

param(
    [int]$Port = 8010,
    # 默认带 --reload。不带的那个版本正是本脚本存在的原因，所以要显式要求。
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'
$backend = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
if (-not (Test-Path (Join-Path $backend 'app\main.py'))) {
    throw "找不到 backend\app\main.py（看了 $backend）——这个脚本得在项目里跑。"
}

# 三个条件同时满足才停：是 uvicorn、跑的是 app.main:app、端口对得上。
# 少任何一个，都可能停掉一个不相干的 python。
$pattern = "uvicorn.*app\.main:app.*--port\s+$Port"
$running = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -match $pattern }

foreach ($proc in $running) {
    Write-Output ("停止 pid={0}（起于 {1:yyyy-MM-dd HH:mm:ss}）" -f $proc.ProcessId, $proc.CreationDate)
    Stop-Process -Id $proc.ProcessId -Force
}
if (-not $running) { Write-Output "端口 $Port 上没有在跑的后端。" }

# 等端口真的松开。直接起会撞上 "address already in use"，而那条报错长得像
# 启动失败，不像「上一个还没退干净」。
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    $held = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $held) { break }
    Start-Sleep -Milliseconds 200
}

$uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--port', "$Port")
if (-not $NoReload) { $uvicornArgs += '--reload' }

$started = Start-Process -FilePath 'python' -ArgumentList $uvicornArgs `
    -WorkingDirectory $backend -PassThru
Start-Sleep -Seconds 2

$alive = Get-Process -Id $started.Id -ErrorAction SilentlyContinue
if (-not $alive) {
    throw "后端起来之后又退了（pid=$($started.Id)）。在 $backend 里手工跑一遍看报什么错。"
}
Write-Output ("已启动 pid={0}（{1}），端口 {2}{3}" -f `
    $started.Id, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Port,
    $(if ($NoReload) { '' } else { '，带 --reload' }))
