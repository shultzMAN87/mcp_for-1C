# SRC-14 — вернуть каталог конфигурации (src/) к канону после заливки .cf.
#
# Запускается на ХОСТЕ (Windows), не в Docker: работает с вашим клоном
# выгрузки, а не со стеком MCP.
#
# Зачем. Правки агента лежат в src/ незакоммиченными. Пока они там,
# ubd-git-sync pull не делает (skipReason dirty_src). После того как .cf с
# правками ушёл в хранилище, src/ надо вернуть к HEAD — тогда sync подтянет
# свежую выгрузку, где ваши правки уже есть.
#
# Правки НЕ удаляются, а откладываются в git stash: если окажется, что в
# хранилище ушло не всё, их можно достать обратно (git stash list / pop).
#
# Граф подхватит возврат сам (watcher, такт раз в минуту) или по
# metadata_refresh() из чата / .\scripts\graph-refresh.ps1.
#
#   .\scripts\restore-src-canon.ps1 -Repo D:\work\ubd_for_ai
#   .\scripts\restore-src-canon.ps1                 # Repo из config.json ubd-git-sync
#   .\scripts\restore-src-canon.ps1 -Yes -NoSync    # без вопросов и без pull

param(
    [string]$Repo = "",
    [string]$Subdir = "src",
    [string]$SyncRepo = "",
    [switch]$Yes,
    [switch]$NoSync
)

$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Host "ОШИБКА: $msg" -ForegroundColor Red
    exit 1
}

# ── где клон ──
$syncConfig = $null
if ($env:LOCALAPPDATA) {
    $cfgPath = Join-Path $env:LOCALAPPDATA "CursorUbdGitSync\config.json"
    if (Test-Path $cfgPath) {
        $syncConfig = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
}
if (-not $Repo -and $syncConfig -and $syncConfig.ubdRepoPath) {
    $Repo = $syncConfig.ubdRepoPath
}
if (-not $Repo) {
    $Repo = (Get-Location).Path
}
if (-not (Test-Path (Join-Path $Repo ".git"))) {
    Fail "в $Repo нет .git — укажите корень клона: -Repo <путь> (рядом с src/, не сам src/)"
}
if (-not (Test-Path (Join-Path $Repo $Subdir))) {
    Fail "в $Repo нет каталога $Subdir — укажите -Subdir"
}
Write-Host "Клон: $Repo, каталог конфигурации: $Subdir"

# ── что изменено ──
$status = & git -C $Repo status --porcelain=v1 --untracked-files=all -- $Subdir
if ($LASTEXITCODE -ne 0) { Fail "git status не отработал" }
$changes = @($status | Where-Object { $_ -and $_.Trim() })

if ($changes.Count -eq 0) {
    Write-Host "$Subdir/ совпадает с HEAD — возвращать нечего." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Изменено в $Subdir/: $($changes.Count)" -ForegroundColor Yellow
    $changes | Select-Object -First 40 | ForEach-Object { Write-Host "    $_" }
    if ($changes.Count -gt 40) { Write-Host "    … ещё $($changes.Count - 40)" }
    Write-Host ""
    if (-not $Yes) {
        Write-Host ".cf с этими правками уже залит в хранилище?" -ForegroundColor Yellow
        Write-Host "Правки не удаляются — уходят в git stash." -ForegroundColor Gray
        $answer = Read-Host "Вернуть $Subdir/ к HEAD? [y/N]"
        if ($answer -notmatch '^(y|yes|д|да)$') {
            Write-Host "Отменено."
            exit 0
        }
    }
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    $msg = "restore-src-canon $stamp ($($changes.Count) файлов в $Subdir/)"
    & git -C $Repo stash push --include-untracked -m $msg -- $Subdir
    if ($LASTEXITCODE -ne 0) { Fail "git stash не отработал — ничего не изменено" }
    $left = @(& git -C $Repo status --porcelain=v1 --untracked-files=all -- $Subdir |
              Where-Object { $_ -and $_.Trim() })
    if ($left.Count -ne 0) { Fail "после stash в $Subdir/ остались изменения: $($left -join '; ')" }
    Write-Host "Готово: $Subdir/ = HEAD. Отложено в stash: «$msg»" -ForegroundColor Green
    Write-Host "  посмотреть:  git -C `"$Repo`" stash list" -ForegroundColor Gray
    Write-Host "  вернуть:     git -C `"$Repo`" stash pop" -ForegroundColor Gray
}

# ── подтянуть свежую выгрузку ──
if (-not $NoSync) {
    if (-not $SyncRepo) {
        $hooks = Join-Path $Repo ".cursor\hooks.json"
        if (Test-Path $hooks) {
            $raw = Get-Content $hooks -Raw -Encoding UTF8
            $m = [regex]::Match($raw, '([A-Za-z]:[^"]*?)[\\/]+Invoke-UbdSessionHook\.ps1')
            if ($m.Success) { $SyncRepo = ($m.Groups[1].Value -replace '\\\\', '\') }
        }
    }
    $update = if ($SyncRepo) { Join-Path $SyncRepo "Update-UbdRepo.ps1" } else { "" }
    if ($update -and (Test-Path $update)) {
        Write-Host ""
        Write-Host "Подтягиваю свежую выгрузку: $update" -ForegroundColor Cyan
        & powershell -NoProfile -ExecutionPolicy Bypass -File $update -Source manual
    } else {
        Write-Host ""
        Write-Host "ubd-git-sync не найден — подтяните выгрузку сами (git pull --ff-only)" -ForegroundColor Gray
        Write-Host "или укажите -SyncRepo <корень клона ubd-git-sync>." -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "Граф подхватит изменения сам (watcher) или по команде:" -ForegroundColor Gray
Write-Host "  metadata_refresh() из чата  /  .\scripts\graph-refresh.ps1" -ForegroundColor Gray
exit 0
