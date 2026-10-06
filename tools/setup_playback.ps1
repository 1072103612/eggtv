$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonWindowed = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe'
if (!(Test-Path -LiteralPath $pythonWindowed)) { throw '未找到本机 Python 运行环境' }
$launcher = Join-Path $PSScriptRoot 'playback_launcher.py'
if (!(Test-Path -LiteralPath $launcher)) { throw '未找到测速工具' }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw '本工具的周一04:00任务要求 Windows 使用中国标准时间' }
$taskAction = New-ScheduledTaskAction -Execute $pythonWindowed -Argument ('"' + $launcher + '" --scheduled') -WorkingDirectory $projectRoot
$taskTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '04:00'
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 7)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'EggTV Weekly Playback' -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Principal $taskPrincipal -Description '蛋壳影院：每周一北京时间04:00在雷电影视里检测电影播放并更新健康记录。日常运行不调用AI。' -Force | Out-Null
$desktopDir = [Environment]::GetFolderPath('Desktop')
$shellLink = New-Object -ComObject WScript.Shell
$shortcut = $shellLink.CreateShortcut((Join-Path $desktopDir '蛋壳影院自动测速.lnk'))
$shortcut.TargetPath = $pythonWindowed
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = '试测、每周自动测速、查看中文报告'
$shortcut.Save()
Write-Output '已安装桌面入口及每周一04:00计划任务。电脑需保持登录；锁屏可以，退出Windows登录后不会运行。'
