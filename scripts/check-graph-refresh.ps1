# TEST-SRC — проверка частичного обновления графа на стенде (PowerShell).
#
# Два сценария, которые встречаются каждый день:
#   1. пришла новая выгрузка из GitLab (git pull уже сделан, граф отстал);
#   2. разработчик дописал код руками и не закоммитил.
# После каждого — доиндексация и сверка графа с файлами на диске: какие
# процедуры в модуле, с каких строк начинаются, куда ведут вызовы.
#
# Сценарий 2 правит рабочую копию: дописывает в общий модуль процедуру
# `ТестГрафа_Проверка` и в конце возвращает файл как был (git checkout),
# даже если проверка упала посередине. Коммитов не делает.
#
#   .\scripts\check-graph-refresh.ps1                     # оба сценария
#   .\scripts\check-graph-refresh.ps1 -SkipScenario2      # только «пришла выгрузка»
#   .\scripts\check-graph-refresh.ps1 -Repo D:\work\ubd   # другой репозиторий выгрузки
#
# Полный вывод — ещё и в logs\check-graph-refresh-<дата>.txt.

param(
    [string]$Repo = "",
    [string]$ConfigSubdir = "",
    [string]$TestModule = "CommonModules/РасчетыСПоставщикамиСервер/Ext/Module.bsl",
    [switch]$SkipScenario1,
    [switch]$SkipScenario2
)

$ErrorActionPreference = "Continue"
$Stack = Split-Path -Parent $PSScriptRoot
$Refresh = Join-Path $PSScriptRoot "graph-refresh.ps1"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$prevOut = $OutputEncoding
$prevCon = [Console]::OutputEncoding
$OutputEncoding = $utf8
[Console]::OutputEncoding = $utf8

$logDir = Join-Path $Stack "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$transcript = Join-Path $logDir ("check-graph-refresh-{0}.txt" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
Start-Transcript -Path $transcript | Out-Null

$results = New-Object System.Collections.ArrayList
$TestProc = "ТестГрафа_Проверка"

function Say([string]$text, [string]$color = "Gray") { Write-Host $text -ForegroundColor $color }
function Head([string]$text) { Write-Host ""; Write-Host "═══ $text" -ForegroundColor Cyan }
function Check([string]$name, [bool]$ok, [string]$detail = "") {
    $mark = if ($ok) { "ПРОЙДЕНО" } else { "ПРОВАЛ  " }
    $color = if ($ok) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1}{2}" -f $mark, $name, $(if ($detail) { " — $detail" } else { "" })) -ForegroundColor $color
    [void]$results.Add([pscustomobject]@{ Ok = $ok; Name = $name; Detail = $detail })
}
function Warn([string]$text) { Write-Host "  [ВНИМАНИЕ] $text" -ForegroundColor Yellow }

# ─── Neo4j ────────────────────────────────────────────────────────────────

function Cy([string]$q) {
    # Пароль берётся из окружения контейнера neo4j (его же читает
    # healthcheck), а не из .env: там бывают кавычки и комментарии.
    $out = $q | docker exec -i neo4j sh -c 'cypher-shell --format plain -u neo4j -p $HC_NEO4J_PASSWORD' 2>&1
    $lines = @($out | ForEach-Object { "$_" })
    # `return ,` — иначе PowerShell развернёт массив из одной строки в саму
    # строку, и `$rows[0]` вернёт её первый символ.
    if ($LASTEXITCODE -ne 0) {
        Warn ("cypher-shell: " + ($lines -join " "))
        return ,@()
    }
    if ($lines.Count -le 1) { return ,@() }
    return ,@($lines[1..($lines.Count - 1)])
}
function Unq([string]$s) { return $s.Trim().Trim('"') }
function CyValue([string]$q) {
    $rows = Cy $q
    if ($rows.Count -eq 0) { return "" }
    return Unq (($rows[0] -split ",")[0])
}
function Esc([string]$s) {
    # Кириллица — экранами \uXXXX внутри строки Cypher, а не байтами: по пути
    # PowerShell 5.1 → docker exec → cypher-shell она приходила в Neo4j
    # искажённой, и запрос по пути модуля молча ничего не находил
    # (первый прогон на стенде 4 ноября: «в графе 0» у всех модулей).
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $s.ToCharArray()) {
        if ($ch -eq [char]'\') { [void]$sb.Append('\\') }
        elseif ($ch -eq [char]"'") { [void]$sb.Append("\'") }
        elseif ([int]$ch -gt 127) { [void]$sb.Append(('\u{0:x4}' -f [int]$ch)) }
        else { [void]$sb.Append($ch) }
    }
    return $sb.ToString()
}

function GraphProcs([string]$rel) {
    # [имя → строка] процедур модуля в графе.
    $map = @{}
    $list = New-Object System.Collections.ArrayList
    foreach ($r in (Cy "MATCH (c:Callable) WHERE c.source_path = '$(Esc $rel)' RETURN c.name, c.line_start ORDER BY c.line_start;")) {
        $parts = $r -split ",\s*"
        if ($parts.Count -lt 2) { continue }
        $name = Unq $parts[0]
        [void]$list.Add($name)
        if (-not $map.ContainsKey($name)) { $map[$name] = [int](Unq $parts[1]) }
    }
    return @{ Names = $list; Lines = $map }
}

$DeclRe = New-Object System.Text.RegularExpressions.Regex(
    '^\s*(Процедура|Функция)\s+([^\s(]+)\s*\(',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)

function DiskProcs([string]$path) {
    $map = @{}
    $list = New-Object System.Collections.ArrayList
    if (-not (Test-Path -LiteralPath $path)) { return @{ Names = $list; Lines = $map } }
    $lines = [System.IO.File]::ReadAllLines($path, [System.Text.Encoding]::UTF8)
    for ($i = 0; $i -lt $lines.Length; $i++) {
        $m = $DeclRe.Match($lines[$i])
        if ($m.Success) {
            $name = $m.Groups[2].Value
            [void]$list.Add($name)
            if (-not $map.ContainsKey($name)) { $map[$name] = $i + 1 }
        }
    }
    return @{ Names = $list; Lines = $map }
}

function CompareModule([string]$rel, [string]$label) {
    # Граф обязан совпасть с файлом: те же процедуры, те же строки (граф
    # считает начало с директивы &НаСервере — допуск в одну строку).
    $disk = DiskProcs (Join-Path $Src $rel)
    $graph = GraphProcs $rel
    $d = @($disk.Names | Sort-Object) -join "|"
    $g = @($graph.Names | Sort-Object) -join "|"
    if ($disk.Names.Count -gt 0 -and $graph.Names.Count -eq 0 -and $rel -match "/Forms/") {
        Warn "$rel — в графе нет процедур; если форма не числится в XML объекта, так и должно быть (FIX-41)"
        return
    }
    $missing = @($disk.Names | Where-Object { $graph.Names -notcontains $_ })
    $extra = @($graph.Names | Where-Object { $disk.Names -notcontains $_ })
    $shifted = @()
    foreach ($n in $disk.Lines.Keys) {
        if ($graph.Lines.ContainsKey($n) -and [Math]::Abs($graph.Lines[$n] - $disk.Lines[$n]) -gt 1) {
            $shifted += "$n (файл $($disk.Lines[$n]), граф $($graph.Lines[$n]))"
        }
    }
    $detail = "процедур в файле $($disk.Names.Count), в графе $($graph.Names.Count)"
    if ($missing.Count) { $detail += "; нет в графе: " + (($missing | Select-Object -First 5) -join ", ") }
    if ($extra.Count) { $detail += "; лишние в графе: " + (($extra | Select-Object -First 5) -join ", ") }
    if ($shifted.Count) { $detail += "; строки разошлись: " + (($shifted | Select-Object -First 3) -join ", ") }
    Check "$label $rel" (($d -eq $g) -and $shifted.Count -eq 0) $detail
}

function RunRefresh([string]$switch = "") {
    # Ключ — через хеш-таблицу: строка "-DryRun" в массиве при splatting
    # уходит в скрипт позиционным аргументом, а не ключом, и graph-refresh
    # молча делал обычный запуск (первый прогон: «-DryRun» обновил 14 файлов).
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $p = @{}
    if ($switch) { $p[$switch] = $true }
    & $Refresh @p
    $rc = $LASTEXITCODE
    $sw.Stop()
    Say ("  → graph-refresh {0}: код {1}, {2:N1} с" -f $(if ($switch) { "-$switch" } else { "" }), $rc, $sw.Elapsed.TotalSeconds) "White"
    return @{ Rc = $rc; Sec = $sw.Elapsed.TotalSeconds }
}

function State() {
    return @{
        Sha   = CyValue "MATCH (s:SourceState {key:'main'}) RETURN s.indexed_sha;"
        Lock  = CyValue "MATCH (s:SourceState {key:'main'}) RETURN coalesce(s.lock_owner, '');"
        Dirty = CyValue "MATCH (s:SourceState {key:'main'}) RETURN coalesce(s.dirty_json, '{}');"
    }
}
function Orphans() { return CyValue "MATCH (c:Callable) WHERE NOT ()-[:HAS_METHOD]->(c) RETURN count(c);" }

# ─── Где выгрузка ─────────────────────────────────────────────────────────

function EnvValue([string]$name) {
    $f = Join-Path $Stack ".env"
    if (-not (Test-Path $f)) { return "" }
    $line = Get-Content $f -Encoding UTF8 | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -First 1
    if (-not $line) { return "" }
    $v = ($line -split "=", 2)[1]
    $v = ($v -split "\s+#", 2)[0]
    return $v.Trim().Trim('"').Trim("'")
}
if (-not $Repo) { $Repo = EnvValue "WORKSPACE_DIR" }
if (-not $ConfigSubdir) { $ConfigSubdir = EnvValue "CONFIG_SUBDIR" }
if (-not $ConfigSubdir) { $ConfigSubdir = "src" }
$Src = Join-Path $Repo $ConfigSubdir
# Не `Git`: имена функций в PowerShell не различают регистр, и функция
# `Git` перекрыла бы сам git — вызов внутри неё ушёл бы в рекурсию.
function RepoGit { & git -C $Repo -c core.quotepath=off @args }

$modified = $false
$restored = $true
try {
    Head "Подготовка"
    Say "  стек:      $Stack"
    Say "  выгрузка:  $Repo (конфигурация — $ConfigSubdir)"
    Say "  протокол:  $transcript"
    if (-not (Test-Path (Join-Path $Repo ".git"))) {
        Check "репозиторий выгрузки найден" $false "$Repo\.git нет — задайте -Repo или WORKSPACE_DIR в .env"
        throw "нет репозитория"
    }
    $head = (RepoGit rev-parse HEAD).Trim()
    $st0 = State
    Check "Neo4j отвечает, состояние источника есть" ([bool]$st0.Sha) "indexed_sha=$($st0.Sha)"
    if (-not $st0.Sha) { throw "нет :SourceState — граф ещё не индексировался в режиме git" }
    Check "граф никто не держит" (-not $st0.Lock) $(if ($st0.Lock) { "аренда у $($st0.Lock)" } else { "" })
    Say "  HEAD выгрузки:   $head"
    Say "  граф собран по:  $($st0.Sha)"
    $dirtySrc = @(RepoGit status --porcelain -- $ConfigSubdir)
    if ($dirtySrc.Count) { Warn "в $ConfigSubdir есть незакоммиченные правки ($($dirtySrc.Count)) — сверка их тоже подхватит" }
    $watcher = EnvValue "WATCHER_ENABLED"
    if ($watcher -eq "true") { Warn "WATCHER_ENABLED=true — watcher может обновить граф сам, раньше скрипта; результат проверок от этого не меняется" }
    Say ("  процедур без владельца до теста: {0}" -f (Orphans))

    # ─── Сценарий 1 ──────────────────────────────────────────────────────
    if (-not $SkipScenario1) {
        Head "Сценарий 1: пришла выгрузка из GitLab"
        if ($st0.Sha -eq $head) {
            Warn "граф уже на HEAD ($head) — новых коммитов нет. Сделайте git pull в $Repo и запустите снова (или -SkipScenario1)"
        } else {
            $commits = @(RepoGit log --oneline "$($st0.Sha)..HEAD")
            $files = @(RepoGit diff --name-only $st0.Sha HEAD -- $ConfigSubdir)
            $bsl = @($files | Where-Object { $_ -like "*.bsl" } | ForEach-Object { $_.Substring($ConfigSubdir.Length + 1) })
            Say "  коммитов: $($commits.Count), файлов: $($files.Count), из них модулей: $($bsl.Count)"
            $bsl | ForEach-Object { Say "    $_" }

            Say ""
            Say "  До обновления (расхождения здесь ожидаемы — граф ещё старый):" "White"
            foreach ($rel in $bsl) {
                $d = DiskProcs (Join-Path $Src $rel); $g = GraphProcs $rel
                Say ("    {0}: в файле {1}, в графе {2}" -f $rel, $d.Names.Count, $g.Names.Count)
            }

            Say ""
            $r = RunRefresh "DryRun"
            Check "план (-DryRun) построен" ($r.Rc -eq 0) "код $($r.Rc)"
            $r = RunRefresh
            Check "доиндексация прошла" ($r.Rc -eq 0) ("код {0}, {1:N1} с" -f $r.Rc, $r.Sec)

            Say ""
            Say "  После обновления:" "White"
            $st1 = State
            Check "граф собран по новому HEAD" ($st1.Sha -eq $head) "indexed_sha=$($st1.Sha)"
            Check "аренда отпущена" (-not $st1.Lock) $st1.Lock
            foreach ($rel in $bsl) { CompareModule $rel "модуль совпал с файлом:" }
            $o = Orphans
            Check "процедур без владельца нет" ($o -eq "0") "без владельца $o"
            $r = RunRefresh "DryRun"
            Check "повторный -DryRun: делать нечего" ($r.Rc -eq 0) "код $($r.Rc)"
        }
    }

    # ─── Сценарий 2 ──────────────────────────────────────────────────────
    if (-not $SkipScenario2) {
        Head "Сценарий 2: правка руками, без коммита"
        $file = Join-Path $Src ($TestModule -replace "/", "\")
        if (-not (Test-Path -LiteralPath $file)) {
            Check "тестовый модуль найден" $false "$file нет — задайте -TestModule"
            throw "нет тестового модуля"
        }
        if (@(RepoGit status --porcelain -- "$ConfigSubdir/$TestModule").Count) {
            Check "тестовый модуль без правок" $false "в $TestModule уже есть незакоммиченные правки — скрипт его не трогает"
            throw "тестовый модуль грязный"
        }
        $moduleId = CyValue "MATCH (c:Callable) WHERE c.source_path = '$(Esc $TestModule)' RETURN c.module_id LIMIT 1;"
        $callee = Cy ("MATCH (a:Callable {module_id: '$(Esc $moduleId)'})-[:CALLS]->(t:Callable) " +
                      "WHERE t.module_id STARTS WITH 'CommonModule.' AND t.module_id <> '$(Esc $moduleId)' " +
                      "RETURN t.module_id, t.name, t.id LIMIT 1;")
        if (-not $callee.Count) {
            # Модуль может не звать другие общие модули вовсе (только менеджеры
            # объектов). Тогда берём процедуру общего модуля, которую уже
            # успешно вызывают из серверного кода (модуль объекта или
            # менеджера) — из серверного общего модуля она тоже разрешится.
            Say "  $moduleId не вызывает другие общие модули; его вызовы:" "DarkGray"
            foreach ($row in (Cy "MATCH (:Callable {module_id: '$(Esc $moduleId)'})-[:CALLS]->(t:Callable) RETURN t.module_id, count(*) AS n ORDER BY n DESC LIMIT 5;")) { Say "    $row" "DarkGray" }
            $callee = Cy ("MATCH (a:Callable)-[:CALLS]->(t:Callable) " +
                          "WHERE (a.module_id ENDS WITH '.ObjectModule' OR a.module_id ENDS WITH '.ManagerModule') " +
                          "AND t.module_id STARTS WITH 'CommonModule.' AND t.module_id <> '$(Esc $moduleId)' AND t.is_export " +
                          "RETURN t.module_id, t.name, t.id LIMIT 1;")
        }
        if (-not $callee.Count) { Check "найдена процедура общего модуля для вызова" $false "в графе нет вызовов общих модулей из серверного кода — задайте -TestModule"; throw "нет цели вызова" }
        $p = $callee[0] -split ",\s*"
        $calleeModule = (Unq $p[0]) -replace "^CommonModule\.", ""
        $calleeName = Unq $p[1]
        $calleeId = Unq $p[2]
        $expectedId = "$moduleId.$TestProc"
        Say "  модуль:  $TestModule ($moduleId)"
        Say "  вызов:   $calleeModule.$calleeName() → ожидаем CALLS в $calleeId"

        $raw = [System.IO.File]::ReadAllText($file, [System.Text.Encoding]::UTF8)
        $nl = if ($raw.Contains("`r`n")) { "`r`n" } else { "`n" }
        $tail = if ($raw.EndsWith("`n")) { "" } else { $nl }
        $add = "$tail$nl" + "Процедура $TestProc() Экспорт$nl`t$calleeModule.$calleeName();${nl}КонецПроцедуры$nl"
        $restored = $false
        [System.IO.File]::AppendAllText($file, $add, $utf8)
        $modified = $true
        Say "  дописана процедура $TestProc (git status: $((RepoGit status --porcelain -- "$ConfigSubdir/$TestModule") -join ' '))"

        Say ""
        Say "  2а. Обычный запуск: правку без коммита он не видит — так и задумано" "White"
        $r = RunRefresh
        $n = CyValue "MATCH (c:Callable {id: '$(Esc $expectedId)'}) RETURN count(c);"
        Check "обычный запуск не трогает незаявленную правку" ($r.Rc -eq 0 -and $n -eq "0") "код $($r.Rc), процедур $TestProc в графе: $n"

        Say ""
        Say "  2б. Сверка (-Reconcile): находит правку через git status" "White"
        $r = RunRefresh "Reconcile"
        Check "сверка прошла" ($r.Rc -eq 0) ("код {0}, {1:N1} с — время сверки на боевой выгрузке" -f $r.Rc, $r.Sec)
        $n = CyValue "MATCH (c:Callable {id: '$(Esc $expectedId)'}) RETURN count(c);"
        Check "процедура появилась в графе" ($n -eq "1") $expectedId
        $calls = CyValue "MATCH (:Callable {id: '$(Esc $expectedId)'})-[:CALLS]->(t:Callable {id: '$(Esc $calleeId)'}) RETURN count(t);"
        Check "вызов разрешён (CALLS)" ($calls -ne "" -and [int]$calls -ge 1) "→ $calleeId"
        $st2 = State
        Check "файл помечен как правка поверх коммита (dirty)" ($st2.Dirty -like "*$TestModule*") $st2.Dirty
        Check "indexed_sha не сдвинулся (коммита не было)" ($st2.Sha -eq $head) $st2.Sha
        CompareModule $TestModule "модуль совпал с файлом:"

        Say ""
        Say "  2в. Вернуть файл как в коммите и доиндексировать" "White"
        RepoGit checkout -q -- "$ConfigSubdir/$TestModule"
        $restored = $true
        $r = RunRefresh
        Check "доиндексация после отката прошла" ($r.Rc -eq 0) "код $($r.Rc)"
        $n = CyValue "MATCH (c:Callable {id: '$(Esc $expectedId)'}) RETURN count(c);"
        Check "тестовой процедуры в графе больше нет" ($n -eq "0") "найдено: $n"
        $st3 = State
        Check "dirty очищен" (-not ($st3.Dirty -like "*$TestModule*")) $st3.Dirty
        CompareModule $TestModule "модуль совпал с файлом:"
        $o = Orphans
        Check "процедур без владельца нет" ($o -eq "0") "без владельца $o"
    }
}
catch {
    Say ""
    Say "  Остановлено: $($_.Exception.Message)" "Red"
    Check "скрипт дошёл до конца" $false $_.Exception.Message
}
finally {
    if ($modified -and -not $restored) {
        Say ""
        Warn "возвращаю $TestModule как в коммите и доиндексирую"
        RepoGit checkout -q -- "$ConfigSubdir/$TestModule"
        & $Refresh | Out-Null
    }
    Head "Итог"
    $fail = @($results | Where-Object { -not $_.Ok })
    $ok = @($results | Where-Object { $_.Ok })
    foreach ($f in $fail) { Say ("  ПРОВАЛ: {0} — {1}" -f $f.Name, $f.Detail) "Red" }
    $color = if ($fail.Count) { "Red" } else { "Green" }
    Say ("  Проверок: {0}, пройдено {1}, провалено {2}" -f $results.Count, $ok.Count, $fail.Count) $color
    Say "  Протокол целиком: $transcript"
    Stop-Transcript | Out-Null
    $OutputEncoding = $prevOut
    [Console]::OutputEncoding = $prevCon
}
if (@($results | Where-Object { -not $_.Ok }).Count) { exit 1 }
exit 0
