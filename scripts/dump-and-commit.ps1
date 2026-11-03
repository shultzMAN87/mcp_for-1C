# SRC-14 — своя выгрузка конфигурации + коммит: сигнал для графа.
#
# Для конфигурации, которой нет в GitLab: выгрузка лежит в <Repo>\src,
# в <Repo> — свой git (git init). Граф узнаёт о новой версии по коммиту:
# выгрузил — закоммитил, и watcher (или graph-refresh.ps1) догонит граф.
#
# Запускается на ХОСТЕ (Windows): нужен Конфигуратор.
#
#   # один раз: подготовить репозиторий
#   .\scripts\dump-and-commit.ps1 -Repo D:\work\my_config -Init -SkipDump
#
#   # выгрузка из серверной базы и коммит
#   .\scripts\dump-and-commit.ps1 -Repo D:\work\my_config `
#       -Platform "C:\Program Files\1cv8\8.3.23.1865\bin\1cv8.exe" `
#       -Server "srv01" -Infobase "my_ib" -User "Админ"
#
#   # из файловой базы
#   .\scripts\dump-and-commit.ps1 -Repo D:\work\my_config -Platform … -File "D:\bases\my"
#
#   # выгрузили руками из Конфигуратора в <Repo>\src — только коммит
#   .\scripts\dump-and-commit.ps1 -Repo D:\work\my_config -SkipDump
#
# Пароль в командную строку не принимается: переменная окружения
# ONEC_PASSWORD или запрос (-AskPassword). Путь к платформе — параметр
# -Platform или переменная ONEC_PLATFORM_EXE.
#
# Выгрузка инкрементальная (-update), если в src уже есть ConfigDumpInfo.xml:
# платформа перезаписывает только изменившиеся объекты и удаляет файлы
# удалённых. -force — полная выгрузка, если формат выгрузки сменился.
# Проверьте ключи на своей версии платформы (8.3.11+).

param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$Subdir = "src",
    [string]$Platform = "",
    [string]$Server = "",
    [string]$Infobase = "",
    [string]$File = "",
    [string]$User = "",
    [switch]$AskPassword,
    [string]$Message = "",
    [switch]$SkipDump,
    [switch]$Init,
    [switch]$Full
)

$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Host "ОШИБКА: $msg" -ForegroundColor Red
    exit 1
}

$target = Join-Path $Repo $Subdir

# ── репозиторий ──
if ($Init) {
    if (-not (Test-Path $Repo)) { New-Item -ItemType Directory -Path $Repo | Out-Null }
    if (-not (Test-Path (Join-Path $Repo ".git"))) {
        & git -C $Repo init -q
        if ($LASTEXITCODE -ne 0) { Fail "git init не отработал" }
        Write-Host "git init: $Repo" -ForegroundColor Green
    }
    # Файлы 1С храним байт в байт: преобразование CRLF дало бы ложные
    # расхождения «диск против HEAD».
    & git -C $Repo config core.autocrlf false
    if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
}
if (-not (Test-Path (Join-Path $Repo ".git"))) {
    Fail "в $Repo нет .git. Один раз: -Init (git init + core.autocrlf false)"
}

# ── выгрузка ──
if (-not $SkipDump) {
    if (-not $Platform) { $Platform = $env:ONEC_PLATFORM_EXE }
    if (-not $Platform -or -not (Test-Path $Platform)) {
        Fail "не найден 1cv8.exe: укажите -Platform или ONEC_PLATFORM_EXE"
    }
    $conn = @()
    if ($File) {
        $conn = @("/F", $File)
    } elseif ($Server -and $Infobase) {
        $conn = @("/S", "$Server\$Infobase")
    } else {
        Fail "нужна база: -File <каталог> или -Server <сервер> -Infobase <имя>"
    }
    if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target | Out-Null }

    $args1c = @("DESIGNER") + $conn
    if ($User) { $args1c += "/N$User" }
    $password = $env:ONEC_PASSWORD
    if (-not $password -and $AskPassword) {
        $secure = Read-Host "Пароль 1С" -AsSecureString
        $password = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    }
    if ($password) { $args1c += "/P$password" }

    $incremental = (Test-Path (Join-Path $target "ConfigDumpInfo.xml")) -and -not $Full
    $args1c += @("/DumpConfigToFiles", $target, "-Format", "Hierarchical")
    if ($incremental) { $args1c += @("-update", "-force") }
    $log = Join-Path $env:TEMP "dump-and-commit-$(Get-Date -Format yyyyMMdd-HHmmss).log"
    $args1c += @("/Out", $log, "/DisableStartupDialogs", "/DisableStartupMessages")

    $mode = if ($incremental) { "инкрементальная (-update)" } else { "полная" }
    Write-Host "Выгрузка ($mode) в $target …" -ForegroundColor Cyan
    # Start-Process в Windows PowerShell 5.1 склеивает массив аргументов
    # пробелами без кавычек — путь «D:\my config\src» разъехался бы на два.
    $argLine = ($args1c | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join ' '
    $p = Start-Process -FilePath $Platform -ArgumentList $argLine -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -ne 0) {
        if (Test-Path $log) { Get-Content $log -Encoding Default | Write-Host }
        Fail "Конфигуратор завершился с кодом $($p.ExitCode) — коммит не делается"
    }
    Write-Host "Выгрузка завершена." -ForegroundColor Green
}

# ── коммит ──
& git -C $Repo add -A -- $Subdir
if ($LASTEXITCODE -ne 0) { Fail "git add не отработал" }
& git -C $Repo diff --cached --quiet -- $Subdir
if ($LASTEXITCODE -eq 0) {
    Write-Host "В $Subdir/ изменений нет — коммит не нужен." -ForegroundColor Green
    exit 0
}
$count = @(& git -C $Repo diff --cached --name-only -- $Subdir).Count
if (-not $Message) { $Message = "Выгрузка $(Get-Date -Format 'yyyy-MM-dd HH:mm')" }
& git -C $Repo commit -q -m $Message -- $Subdir
if ($LASTEXITCODE -ne 0) { Fail "git commit не отработал (не задан user.name / user.email?)" }
$head = (& git -C $Repo rev-parse --short HEAD).Trim()
Write-Host "Закоммичено: $count файлов, $head «$Message»" -ForegroundColor Green
Write-Host "Граф догонит сам (watcher) или: .\scripts\graph-refresh.ps1" -ForegroundColor Gray
exit 0
