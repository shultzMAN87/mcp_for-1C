# =====================================================================
# setup-deps.ps1 — однокнопочная подготовка машины к работе с проектом
# =====================================================================
# Что делает:
#   1. Проверяет наличие Python (3.10+) — py или python в PATH
#   2. Устанавливает Python-зависимости из evals/runner/requirements.txt
#   3. Проверяет PowerShell ExecutionPolicy, при необходимости предлагает
#      выполнить Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
#   4. Проверяет системный прокси, при необходимости предлагает прописать
#      NO_PROXY=localhost,127.0.0.1 для текущего пользователя (навсегда)
#   5. Финальный отчёт с тем, что сделано и что делать дальше
#
# Когда использовать:
#   - На каждой НОВОЙ машине после клонирования проекта
#   - После обновления зависимостей в requirements.txt
#   - Перед первым запуском smoke-тестов
#
# Запуск из корня проекта:
#   .\setup-deps.ps1
#   .\setup-deps.ps1 -Force           # без подтверждений
#   .\setup-deps.ps1 -SkipPython      # пропустить установку pip-пакетов
#   .\setup-deps.ps1 -SkipPolicy      # пропустить проверку ExecutionPolicy
#   .\setup-deps.ps1 -SkipProxy       # пропустить настройку NO_PROXY
# =====================================================================

param(
    [switch]$Force,
    [switch]$SkipPython,
    [switch]$SkipPolicy,
    [switch]$SkipProxy
)

# ---------- Утилиты вывода ----------
function Section($title) {
    Write-Host ""
    Write-Host "==> $title" -ForegroundColor Cyan
}
function Info($msg)   { Write-Host "    $msg" -ForegroundColor Gray }
function Ok($msg)     { Write-Host "    [OK] $msg" -ForegroundColor Green }
function WarnMsg($msg){ Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function ErrMsg($msg) { Write-Host "    [ERROR] $msg" -ForegroundColor Red }

# ---------- Пути ----------
$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
$RequirementsFile = Join-Path $ProjectRoot "evals\runner\requirements.txt"
$ComposeFile      = Join-Path $ProjectRoot "docker-compose.yml"

# ---------- Заголовок ----------
Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " setup-deps.ps1 — bootstrap для 1C MCP Suite" -ForegroundColor Cyan
Write-Host "===================================================================" -ForegroundColor Cyan

# ---------- Валидация: мы в корне проекта? ----------
if (-not (Test-Path $ComposeFile)) {
    ErrMsg "Не вижу docker-compose.yml в текущей папке: $ProjectRoot"
    ErrMsg "Запустите скрипт из корня проекта 27_1c-mcp-suite-full-stack."
    exit 1
}

# Состояние для финального отчёта
$state = @{
    PythonOk     = $false
    PythonCmd    = $null
    PipInstalled = $false
    PolicyOk     = $false
    ProxyOk      = $false
    Issues       = @()
}

# =====================================================================
# 1. Проверка Python
# =====================================================================
Section "1/4  Проверка Python"

if ($SkipPython) {
    Info "пропущено (флаг -SkipPython)"
    $state.PythonOk = $true
} else {
    # Пытаемся найти py (Windows launcher) или python
    $pythonCmd = $null
    foreach ($cmd in @("py", "python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            $pythonCmd = $cmd
            break
        }
    }

    if (-not $pythonCmd) {
        ErrMsg "Python не найден в PATH"
        ErrMsg "Установите Python 3.10+ с https://www.python.org/downloads/"
        ErrMsg "при установке отметьте 'Add Python to PATH'"
        $state.Issues += "Python не установлен"
    } else {
        $version = (& $pythonCmd --version 2>&1) | Out-String
        $version = $version.Trim()
        Ok "найден: $pythonCmd ($version)"
        $state.PythonOk = $true
        $state.PythonCmd = $pythonCmd

        # Проверяем версию >= 3.10
        if ($version -match "(\d+)\.(\d+)\.?(\d+)?") {
            $major = [int]$matches[1]
            $minor = [int]$matches[2]
            if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
                WarnMsg "версия ниже 3.10 — некоторые пакеты могут не установиться"
                $state.Issues += "Python ниже 3.10"
            }
        }
    }
}

# =====================================================================
# 2. Установка Python-зависимостей
# =====================================================================
Section "2/4  Установка Python-зависимостей"

if ($SkipPython) {
    Info "пропущено (флаг -SkipPython)"
    $state.PipInstalled = $true
} elseif (-not $state.PythonOk) {
    ErrMsg "пропущено — Python не найден"
    $state.Issues += "pip install не выполнен"
} elseif (-not (Test-Path $RequirementsFile)) {
    ErrMsg "не найден $RequirementsFile"
    $state.Issues += "requirements.txt отсутствует"
} else {
    Info "файл: $RequirementsFile"
    Info "команда: pip install -r `"$RequirementsFile`""
    Write-Host ""

    & $state.PythonCmd -m pip install -r $RequirementsFile
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Ok "пакеты установлены"
        $state.PipInstalled = $true
    } else {
        ErrMsg "pip install вернул код $LASTEXITCODE"
        Info "попробуйте запустить вручную:"
        Info "  $($state.PythonCmd) -m pip install -r `"$RequirementsFile`""
        $state.Issues += "pip install упал"
    }
}

# =====================================================================
# 3. PowerShell ExecutionPolicy
# =====================================================================
Section "3/4  PowerShell ExecutionPolicy"

if ($SkipPolicy) {
    Info "пропущено (флаг -SkipPolicy)"
    $state.PolicyOk = $true
} else {
    $currentPolicy = Get-ExecutionPolicy -Scope CurrentUser
    Info "текущая политика (CurrentUser): $currentPolicy"

    if ($currentPolicy -in @("RemoteSigned", "Unrestricted", "Bypass")) {
        Ok "политика позволяет запуск локальных .ps1 скриптов"
        $state.PolicyOk = $true
    } else {
        WarnMsg "политика '$currentPolicy' блокирует запуск .ps1 файлов"
        WarnMsg "из-за этого .\restore-lic.ps1 и .\run-smoke-server.ps1 не запустятся"
        Write-Host ""
        Info "рекомендуется выполнить:"
        Info "  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser"

        $doIt = $Force
        if (-not $doIt) {
            Write-Host ""
            $ans = Read-Host "    Выполнить сейчас? (y/n)"
            $doIt = ($ans -eq "y" -or $ans -eq "Y")
        }

        if ($doIt) {
            try {
                Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
                Ok "политика обновлена на RemoteSigned"
                $state.PolicyOk = $true
            } catch {
                ErrMsg "не удалось изменить политику: $_"
                $state.Issues += "ExecutionPolicy не изменена"
            }
        } else {
            Info "пропущено пользователем"
            Info "альтернативно — запускайте скрипты через:"
            Info "  powershell -ExecutionPolicy Bypass -File .\restore-lic.ps1"
            $state.Issues += "ExecutionPolicy не изменена"
        }
    }
}

# =====================================================================
# 4. NO_PROXY для системного прокси
# =====================================================================
Section "4/4  Настройка NO_PROXY для localhost"

if ($SkipProxy) {
    Info "пропущено (флаг -SkipProxy)"
    $state.ProxyOk = $true
} else {
    # Проверяем что NO_PROXY уже установлен
    $currentNoProxy = [System.Environment]::GetEnvironmentVariable("NO_PROXY", "User")
    $hasLocalhost = $currentNoProxy -and ($currentNoProxy -match "localhost|127\.0\.0\.1")

    # Признаки наличия системного прокси
    $hasHttpProxy = [System.Environment]::GetEnvironmentVariable("HTTP_PROXY", "User") -or `
                    [System.Environment]::GetEnvironmentVariable("HTTPS_PROXY", "User") -or `
                    [System.Environment]::GetEnvironmentVariable("HTTP_PROXY", "Machine") -or `
                    [System.Environment]::GetEnvironmentVariable("HTTPS_PROXY", "Machine")

    if ($hasLocalhost) {
        Ok "NO_PROXY уже содержит localhost: $currentNoProxy"
        $state.ProxyOk = $true
    } else {
        Info "NO_PROXY для пользователя: $(if ($currentNoProxy) { $currentNoProxy } else { '(не установлен)' })"

        if ($hasHttpProxy) {
            WarnMsg "обнаружен системный прокси (HTTP_PROXY/HTTPS_PROXY)"
            WarnMsg "smoke-скрипты будут получать 503 на запросы к localhost без NO_PROXY"
        } else {
            Info "системный прокси не обнаружен через переменные окружения"
            Info "но он может быть в VPN-клиенте (V2RayN, Clash и т.п.)"
        }

        Write-Host ""
        Info "рекомендуется прописать NO_PROXY=localhost,127.0.0.1 для пользователя"
        Info "(сохранится между перезапусками PowerShell и сессиями)"

        $doIt = $Force
        if (-not $doIt) {
            Write-Host ""
            $ans = Read-Host "    Прописать NO_PROXY=localhost,127.0.0.1 сейчас? (y/n)"
            $doIt = ($ans -eq "y" -or $ans -eq "Y")
        }

        if ($doIt) {
            try {
                $newValue = "localhost,127.0.0.1"
                if ($currentNoProxy) {
                    # Дописываем к существующему значению
                    $newValue = "$currentNoProxy,localhost,127.0.0.1"
                }
                [System.Environment]::SetEnvironmentVariable("NO_PROXY", $newValue, "User")
                # И в текущую сессию тоже
                $env:NO_PROXY = $newValue
                Ok "NO_PROXY=$newValue (сохранено для User)"
                Info "перезапустите PowerShell, чтобы изменения подхватились всеми приложениями"
                $state.ProxyOk = $true
            } catch {
                ErrMsg "не удалось установить NO_PROXY: $_"
                $state.Issues += "NO_PROXY не установлен"
            }
        } else {
            Info "пропущено пользователем"
            Info "альтернативно — устанавливайте в каждой сессии PowerShell:"
            Info "  `$env:NO_PROXY = 'localhost,127.0.0.1'"
            $state.Issues += "NO_PROXY не установлен"
        }
    }
}

# =====================================================================
# Финальный отчёт
# =====================================================================
Section "ИТОГ"

$allOk = $state.PythonOk -and $state.PipInstalled -and $state.PolicyOk -and $state.ProxyOk

Write-Host ""
Write-Host "    Python ............ $(if ($state.PythonOk) { '[OK]' } else { '[FAIL]' })" -ForegroundColor $(if ($state.PythonOk) { 'Green' } else { 'Red' })
Write-Host "    pip-пакеты ........ $(if ($state.PipInstalled) { '[OK]' } else { '[FAIL]' })" -ForegroundColor $(if ($state.PipInstalled) { 'Green' } else { 'Red' })
Write-Host "    ExecutionPolicy ... $(if ($state.PolicyOk) { '[OK]' } else { '[WARN]' })" -ForegroundColor $(if ($state.PolicyOk) { 'Green' } else { 'Yellow' })
Write-Host "    NO_PROXY .......... $(if ($state.ProxyOk) { '[OK]' } else { '[WARN]' })" -ForegroundColor $(if ($state.ProxyOk) { 'Green' } else { 'Yellow' })

if ($state.Issues.Count -gt 0) {
    Write-Host ""
    Write-Host "    Незавершённые шаги:" -ForegroundColor Yellow
    foreach ($issue in $state.Issues) {
        Write-Host "      - $issue" -ForegroundColor Yellow
    }
}

Write-Host ""
if ($allOk) {
    Write-Host "===================================================================" -ForegroundColor Green
    Write-Host " ВСЁ ГОТОВО — машина настроена" -ForegroundColor Green
    Write-Host "===================================================================" -ForegroundColor Green
} else {
    Write-Host "===================================================================" -ForegroundColor Yellow
    Write-Host " ЧАСТИЧНО ВЫПОЛНЕНО — устраните проблемы и запустите снова" -ForegroundColor Yellow
    Write-Host "===================================================================" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Что делать дальше:" -ForegroundColor Cyan
Write-Host "  1. Прочитайте README.md — там пошаговая инструкция"
Write-Host "  2. Проверка готовности:  py scripts\check_prereqs.py"
Write-Host "  3. Подъём стека:         docker compose up -d --build"
Write-Host "  4. Тесты:                py scripts\run_all_tests.py"
Write-Host ""
