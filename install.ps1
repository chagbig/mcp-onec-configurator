# install.ps1 — установщик поставки mcp-configurator v1.2.0
# Пути НЕ привязаны к диску: дом задаётся -HomeDir (любой путь без кириллицы),
# мост находит его сам (env MCP_SNEGOPAT_HOME + маркер mcp\mcp_bridge.home).
# Снегопат берётся из zip (любая версия формата snegopat-client), ПОЛНЫЙ комплект
# без обрезки; мост включается как пользовательский аддин (snegopat.set).
# Запуск из корня распакованной поставки:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1
# Ключи: -HomeDir C:\Snegopat-mcp | -SnegopatZip <путь> | -SkipPip | -SkipSnegopat
param(
    [string]$HomeDir = "C:\Snegopat-mcp",
    [string]$SnegopatZip = "",
    [switch]$SkipPip,
    [switch]$SkipSnegopat
)
$ErrorActionPreference = "Stop"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Fail($msg) { Write-Host "ОТКАЗ: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

Write-Host "=== Установка mcp-configurator v1.2.0 в $HomeDir ==="

# ---------- 1. Проверки окружения ----------
Step "Проверки окружения"
$Platform = "C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe"
if (-not (Test-Path $Platform)) {
    Write-Host "    ВНИМАНИЕ: платформа 8.3.27.2074 не найдена ($Platform) —" -ForegroundColor Yellow
    Write-Host "    продолжаю (мост стартует только на поддерживаемой платформе)." -ForegroundColor Yellow
}
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Fail "python не найден в PATH. Установите Python >= 3.11 (галочка 'Add to PATH')." }
$pyVer = (& python --version) 2>&1
if ($pyVer -notmatch "3\.(\d+)\.") { Fail "Не удалось определить версию Python: $pyVer" }
if ([int]$Matches[1] -lt 11) { Fail "Нужен Python >= 3.11, найден: $pyVer" }

# ---------- 2. Снегопат из zip (любая версия формата snegopat-client) ----------
# раскладка: пакет (install.ps1 + mcp-configurator\ рядом) или репо (всё в корне)
$LayoutProj = Join-Path $PSScriptRoot "mcp-configurator"
$IsPackage = Test-Path (Join-Path $LayoutProj "server")
$ProjSrc = if ($IsPackage) { $LayoutProj } else { $PSScriptRoot }
$BridgeSrc = Join-Path $PSScriptRoot "bridge\mcp_bridge.js"
if (-not (Test-Path $BridgeSrc)) { $BridgeSrc = Join-Path $ProjSrc "bridge\mcp_bridge.js" }
$Gen = Join-Path $ProjSrc "server\tools\make_snegopat_set.py"
$TplCfg = Join-Path $ProjSrc "server\mcp_snegopat.json.example"
$SnegRoot = ""
if (-not $SkipSnegopat) {
    Step "Снегопат из zip"
    if (-not $SnegopatZip) {
        $cand = Get-ChildItem $PSScriptRoot -Filter "snegopat-client-*.zip" | Select-Object -First 1
        if (-not $cand) {
            $cand = Get-ChildItem (Join-Path $PSScriptRoot "mcp-configurator") -Filter "snegopat-client-*.zip" -ErrorAction SilentlyContinue | Select-Object -First 1
        }
        if (-not $cand) { Fail "в поставке нет snegopat-client-*.zip (собиралась без снегопата?)" }
        $SnegopatZip = $cand.FullName
    }
    if (-not (Test-Path $SnegopatZip)) { Fail "не найден $SnegopatZip" }
    $tmp = Join-Path $env:TEMP ("sneg-install-" + $Stamp)
    Expand-Archive -Path $SnegopatZip -DestinationPath $tmp -Force
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    $srcX64 = Join-Path $inner.FullName "x64"
    if (-not (Test-Path $srcX64)) { Fail "в zip нет x64: $SnegopatZip" }
    # версия из version.json
    $vj = Get-Content (Join-Path $srcX64 "version.json") -Raw | ConvertFrom-Json
    $Ver = $vj.ours
    if (-not $Ver) { Fail "в version.json нет поля ours" }
    Write-Host "    Снегопат $Ver (сборка $($vj.released))"
    $SnegRoot = Join-Path $HomeDir "snegopat\$Ver"
    if (Test-Path (Join-Path $SnegRoot "x64")) {
        # живой starter/1cv8 из ЗАМЕНЯЕМОГО каталога ломает Move-Item (#2 v1.8.0):
        # starter — гасим молча (резидентный инжектор, стартует заново шагом 6),
        # конфигуратор — явный отказ (убивать молча нельзя)
        Get-Process starter -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -like "$SnegRoot*"
        } | ForEach-Object {
            Write-Host "    останавливаю живой starter из заменяемого каталога (pid $($_.Id))"
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
        $busy1c = Get-Process 1cv8 -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -like "$SnegRoot*"
        }
        if ($busy1c) { Fail "конфигуратор запущен из $SnegRoot — закройте его и повторите установку" }
        $bak = Join-Path $SnegRoot ("x64-backup-" + $Stamp)
        Move-Item (Join-Path $SnegRoot "x64") $bak
        Write-Host "    прежняя сборка сохранена: $bak"
    }
    New-Item -ItemType Directory -Path (Join-Path $SnegRoot "x64") -Force | Out-Null
    robocopy $srcX64 (Join-Path $SnegRoot "x64") /E /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy снегопата: код $LASTEXITCODE" }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

    # пользовательские каталоги — пустыми
    foreach ($d in "logs", "autosavedata", "journal") {
        New-Item -ItemType Directory -Path (Join-Path $SnegRoot "x64\$d") -Force | Out-Null
    }
    if (-not (Test-Path (Join-Path $SnegRoot "x64\data"))) {
        New-Item -ItemType Directory -Path (Join-Path $SnegRoot "x64\data") -Force | Out-Null
    }

    # мост -> addins\mcp (в КАЖДУЮ версию снегопата)
    $AddinDir = Join-Path $SnegRoot "x64\modules\snegopat\addins\mcp"
    New-Item -ItemType Directory -Path $AddinDir -Force | Out-Null
    Copy-Item $BridgeSrc (Join-Path $AddinDir "mcp_bridge.js") -Force

    # main.js НЕ патчится (полный комплект без обрезки); мост включается
    # пользовательским аддином через snegopat.set
    Step "Включение моста в snegopat.set (пользовательский аддин)"
    $SetDir = Join-Path $SnegRoot "x64\data\Snegopat\settings"
    $Factory = Join-Path $SnegRoot "x64\data\Snegopat\defaults\snegopat.set"
    New-Item -ItemType Directory -Path $SetDir -Force | Out-Null
    if (Test-Path $Factory) {
        & python $Gen --src $Factory --out (Join-Path $SetDir "snegopat.set")
    } else {
        & python $Gen --fresh --out (Join-Path $SetDir "snegopat.set")
    }
    if ($LASTEXITCODE -ne 0) { Fail "make_snegopat_set: код $LASTEXITCODE" }
    # _version рядом (формат профайла Снегопата)
    $SetDirDef = Join-Path $SnegRoot "x64\data\Snegopat\defaults"
    if (Test-Path (Join-Path $SetDirDef "_version")) {
        Copy-Item (Join-Path $SetDirDef "_version") (Join-Path $SetDir "_version") -Force
    }
} else { Step "Снегопат пропущен (-SkipSnegopat)" }

# ---------- 2b. Синхронизация моста во ВСЕ установленные версии ----------
# Смоук v1.2.3 (чистая машина, отчёт mcp-configurator-v1.2.3-smoke-all-tools):
# при -SkipSnegopat рабочий аддин snegopat\<вер>\...\addins\mcp\mcp_bridge.js
# НЕ обновлялся — сессии оставались на старом мосту (голое «Error» вместо
# текстов ошибок v1.2.3). Выполняется ВСЕГДА (и после установки новой версии —
# идемпотентно): перекладываем мост из поставки в каждую найденную версию.
Step "Синхронизация моста в установленные версии Снегопата"
$Synced = 0
$SyncTargets = @()
Get-ChildItem (Join-Path $HomeDir "snegopat") -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $SyncTargets += @{ Name = $_.Name; Dir = (Join-Path $_.FullName "x64\modules\snegopat\addins\mcp") }
}
# легаси-раскладка v1.1.x: мост прямо в <дом>\x64\...
$SyncTargets += @{ Name = "x64 (легаси)"; Dir = (Join-Path $HomeDir "x64\modules\snegopat\addins\mcp") }
foreach ($t in $SyncTargets) {
    if (Test-Path $t.Dir) {
        try {
            Copy-Item $BridgeSrc (Join-Path $t.Dir "mcp_bridge.js") -Force
            Write-Host "    мост обновлён: $($t.Name)"
            $Synced++
        } catch {
            Fail ("не удалось обновить мост в {0}: {1} (конфигураторы закрыты?)" -f $t.Name, $_.Exception.Message)
        }
    }
}
if ($Synced -eq 0) {
    Write-Host "    версий с аддином не найдено (мост ставится шагом «Снегопат из zip»)"
}

# ---------- 3. Дом: каталоги моста, маркер, флаг, конфиг ----------
Step "Дом: $HomeDir"
foreach ($d in "mcp\sessions", "mcp\journal", "mcp\forms", "backups", "bin", "verify", "temp") {
    New-Item -ItemType Directory -Path (Join-Path $HomeDir $d) -Force | Out-Null
}
New-Item -ItemType File -Path (Join-Path $HomeDir "mcp\mcp_bridge.home") -Force | Out-Null
$OnFlag = Join-Path $HomeDir "mcp\mcp_bridge.on"
if (-not (Test-Path $OnFlag)) { New-Item -ItemType File -Path $OnFlag | Out-Null }

$Cfg = Join-Path $HomeDir "mcp\mcp_snegopat.json"
# КОНФИГ НИКОГДА НЕ ПЕРЕЗАПИСЫЕТСЯ (в нём own_bases и allow-флаги владельца).
if (Test-Path -LiteralPath $Cfg) {
    Write-Host "    конфиг уже есть — не тронут: $Cfg"
} else {
    $j = Get-Content $TplCfg -Raw -Encoding UTF8
    $j = $j.Replace("C:\\Snegopat-mcp\\mcp", $HomeDir.Replace("\", "\\") + "\\mcp")
    [System.IO.File]::WriteAllText($Cfg, $j, (New-Object System.Text.UTF8Encoding($true)))
    Write-Host "    создан $Cfg — допишите свои базы в own_bases (формат: File=`"путь`";)"
}
if (-not (Test-Path -LiteralPath $Cfg)) { Fail "конфиг отсутствует после установки: $Cfg" }

# env пользователя: мост и сервер находят дом без угадывания
[Environment]::SetEnvironmentVariable("MCP_SNEGOPAT_HOME", $HomeDir, "User")
[Environment]::SetEnvironmentVariable("MCP_SNEGOPAT_CONFIG", $Cfg, "User")
Write-Host "    env (User): MCP_SNEGOPAT_HOME=$HomeDir; MCP_SNEGOPAT_CONFIG=$Cfg"

# ---------- 4. DynamicWrapperX (per-user, без админа) ----------
Step "DynamicWrapperX 2.2.0.0 (per-user регистрация)"
$Dwx = Join-Path $HomeDir "bin\dynwrapx.dll"
$DwxSrcPkg = Join-Path $PSScriptRoot "bin\dynwrapx.dll"
$DwxSrcHome = Join-Path $HomeDir "bin\dynwrapx.dll"
if (Test-Path $DwxSrcPkg) {
    New-Item -ItemType Directory -Path (Join-Path $HomeDir "bin") -Force | Out-Null
    Copy-Item $DwxSrcPkg $Dwx -Force
    Copy-Item (Join-Path $PSScriptRoot "bin\dynwrapx32.dll") (Join-Path $HomeDir "bin\dynwrapx32.dll") -Force
    Copy-Item (Join-Path $PSScriptRoot "bin\DWX-Help_Ru.chm") (Join-Path $HomeDir "bin\DWX-Help_Ru.chm") -Force
} elseif (Test-Path $DwxSrcHome) {
    Write-Host "    в пакете нет bin\ — используется уже установленный $DwxSrcHome"
} else {
    Fail "dynwrapx.dll не найден ни в пакете ($DwxSrcPkg), ни в $DwxSrcHome"
}
regsvr32 /s /i $Dwx | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "regsvr32 /i dynwrapx.dll: код $LASTEXITCODE" }
Write-Host "    зарегистрирован per-user (HKCU): $Dwx"

# ---------- 5. Проект и пакет Python ----------
Step "Проект -> $HomeDir\mcp-configurator"
$ProjDst = Join-Path $HomeDir "mcp-configurator"
if (-not (Test-Path $ProjSrc)) { Fail "в поставке нет mcp-configurator" }
if (Test-Path $ProjDst) {
    Write-Host "    каталог уже существует — обновляю файлы поверх (git pull — путь обновлений)"
}
robocopy $ProjSrc $ProjDst /E /NFL /NDL /NJH /NJS /XD __pycache__ .git | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "robocopy проекта: код $LASTEXITCODE" }
if (-not $SkipPip) {
    Step "pip install -e server"
    Push-Location $ProjDst
    & python -m pip install -e server
    if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "pip install -e server: код $LASTEXITCODE (нужен доступ к PyPI для mcp)" }
    Pop-Location
} else { Step "pip пропущен (-SkipPip)" }

# ---------- 6. Старт стартера (последней установленной версии) ----------
if ($SnegRoot) {
    Step "Стартер Снегопата (резидентный инжектор)"
    $Starter = Join-Path $SnegRoot "x64\starter.exe"
    $Running = Get-Process starter -ErrorAction SilentlyContinue
    if (-not $Running) {
        Start-Process -FilePath $Starter -WorkingDirectory (Join-Path $SnegRoot "x64")
        Start-Sleep -Seconds 3
        Write-Host "    запущен (cwd = x64 — важно для внедрения)"
    } else { Write-Host "    уже запущен (pid $($Running.Id))" }
}

# ---------- Готово ----------
Write-Host ""
Write-Host "=== УСТАНОВЛЕНО. Дальше вручную: ===" -ForegroundColor Green
Write-Host "1. ZCode: добавить MCP-сервер в ~/.zcode/cli/config.json -> mcp.servers:"
Write-Host '   "mcp-snegopat": {"command": "mcp-snegopat"}  (+ env MCP_SNEGOPAT_CONFIG, уже в User-env)'
Write-Host "2. Свои базы: дописать в own_bases конфига $Cfg"
Write-Host "3. Конфигуратор: starter.exe запущен; живой запуск конфигуратора — строго"
Write-Host "   1cv8.exe DESIGNER /F <база>  (без слова DESIGNER откроется лаунчер и повиснет)."
Write-Host "4. Разные версии Снегопата: повторите установку с другим -SnegopatZip —"
Write-Host "   версии живут параллельно в $HomeDir\snegopat\<версия>\."
Write-Host "5. Проверка: list_sessions в ZCode при открытом конфигураторе."
Write-Host "Подробности: РУКОВОДСТВО-АДМИНИСТРАТОРА.md и РУКОВОДСТВО-ПОЛЬЗОВАТЕЛЯ.md."
# успех — явный код: последний robocopy возвращает 1 («файлы скопированы») и
# без этого утекал в exit-код установщика (найдено на прогоне v1.2.4)
exit 0
