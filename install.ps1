# install.ps1 — установщик поставки mcp-configurator v1.1.0
# Целевая раскладка жёсткая: мост пишет в <Drive>:\Snegopat-mcp\mcp (константы моста).
# Запуск из корня распакованной поставки:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 [-Drive Y]
# Повторный запуск поверх установленного: снегопат-сборка уезжает в
#   <Drive>:\Snegopat-mcp\x64-backup-<штамп>, проект обновляется поверх (git — путь обновлений).
param(
    [string]$Drive = "Y:",
    [switch]$SkipPip,
    [switch]$SkipSnegopat
)
$ErrorActionPreference = "Stop"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Root = Join-Path $Drive "Snegopat-mcp"

function Fail($msg) { Write-Host "ОТКАЗ: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

Write-Host "=== Установка mcp-configurator v1.1.0 в $Root ==="

# ---------- 1. Проверки окружения ----------
Step "Проверки окружения"
$Platform = "C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe"
if (-not (Test-Path $Platform)) {
    Fail "Платформа 1С 8.3.27.2074 не найдена ($Platform). Установите её и повторите."
}
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Fail "python не найден в PATH. Установите Python >= 3.11 (галочка 'Add to PATH')."}
$pyVer = (& python --version) 2>&1
if ($pyVer -notmatch "3\.(\d+)\.") { Fail "Не удалось определить версию Python: $pyVer" }
if ([int]$Matches[1] -lt 11) { Fail "Нужен Python >= 3.11, найден: $pyVer" }
if (-not (Test-Path ($Drive + "\"))) { Fail "Диска $Drive нет. Мост жёстко пишет в ${Drive}:\Snegopat-mcp\mcp (константы bridge/mcp_bridge.js)." }

# ---------- 2. Снегопат (модифицированная сборка 1.6.43) ----------
if (-not $SkipSnegopat) {
    Step "Снегопат 1.6.43 -> $Root\x64"
    $src = Join-Path $PSScriptRoot "snegopat-x64"
    if (-not (Test-Path $src)) { Fail "в поставке нет snegopat-x64 (собиралась без снегопата?)" }
    if (Test-Path (Join-Path $Root "x64")) {
        $bak = Join-Path $Root "x64-backup-$Stamp"
        Move-Item (Join-Path $Root "x64") $bak
        Write-Host "    прежняя сборка сохранена: $bak"
    }
    New-Item -ItemType Directory -Path (Join-Path $Root "x64") -Force | Out-Null
    robocopy $src (Join-Path $Root "x64") /E /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy снегопата: код $LASTEXITCODE" }
    # пользовательские каталоги — пустыми
    foreach ($d in "logs", "data", "autosavedata", "journal") {
        New-Item -ItemType Directory -Path (Join-Path $Root "x64\$d") -Force | Out-Null
    }
} else { Step "Снегопат пропущен (-SkipSnegopat)" }

# ---------- 3. Каталоги моста и флаг включения ----------
Step "Каталоги моста"
foreach ($d in "mcp\sessions", "mcp\journal", "mcp\forms", "backups", "bin", "verify", "temp") {
    New-Item -ItemType Directory -Path (Join-Path $Root $d) -Force | Out-Null
}
$OnFlag = Join-Path $Root "mcp\mcp_bridge.on"
if (-not (Test-Path $OnFlag)) { New-Item -ItemType File -Path $OnFlag | Out-Null }

# ---------- 4. Конфиг сервера ----------
Step "Конфиг сервера mcp_snegopat.json"
$Cfg = Join-Path $Root "mcp\mcp_snegopat.json"
if (-not (Test-Path $Cfg)) {
    $tpl = Join-Path $PSScriptRoot "mcp-configurator\server\mcp_snegopat.json.example"
    $j = Get-Content $tpl -Raw -Encoding UTF8
    $j = $j.Replace("Y:\\Snegopat-mcp\\mcp", "$Drive`:\\Snegopat-mcp\\mcp")
    # own_bases поставки не содержит — владелец дописывает свои базы
    [System.IO.File]::WriteAllText($Cfg, $j, (New-Object System.Text.UTF8Encoding($true)))
    Write-Host "    создан $Cfg — допишите свои базы в own_bases (формат: File=`"путь`";)"
} else { Write-Host "    уже есть — не тронут" }

# ---------- 5. DynamicWrapperX (per-user, без админа) ----------
Step "DynamicWrapperX 2.2.0.0 (per-user регистрация)"
$Dwx = Join-Path $Root "bin\dynwrapx.dll"
Copy-Item (Join-Path $PSScriptRoot "bin\dynwrapx.dll") $Dwx -Force
Copy-Item (Join-Path $PSScriptRoot "bin\dynwrapx32.dll") (Join-Path $Root "bin\dynwrapx32.dll") -Force
Copy-Item (Join-Path $PSScriptRoot "bin\DWX-Help_Ru.chm") (Join-Path $Root "bin\DWX-Help_Ru.chm") -Force
regsvr32 /i $Dwx | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "regsvr32 /i dynwrapx.dll: код $LASTEXITCODE" }
Write-Host "    зарегистрирован per-user (HKCU): $Dwx"

# ---------- 6. Проект и пакет Python ----------
Step "Проект -> $Drive\mcp-configurator"
$ProjSrc = Join-Path $PSScriptRoot "mcp-configurator"
$ProjDst = Join-Path $Drive "mcp-configurator"
if (-not (Test-Path $ProjSrc)) { Fail "в поставке нет mcp-configurator" }
if (Test-Path $ProjDst) {
    Write-Host "    каталог уже существует — обновляю файлы поверх (незакоммиченные правки сохраняются, git pull — путь обновлений)"
}
robocopy $ProjSrc $ProjDst /E /NFL /NDL /NJH /NJS /XD __pycache__ .git | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "robocopy проекта: код $LASTEXITCODE" }
if (-not $SkipPip) {
    Step "pip install -e server"
    Push-Location (Join-Path $ProjDst)
    & python -m pip install -e server
    if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "pip install -e server: код $LASTEXITCODE (нужен доступ к PyPI для mcp)" }
    Pop-Location
} else { Step "pip пропущен (-SkipPip)" }

# ---------- 7. Старт стартера ----------
Step "Стартер Снегопата (резидентный инжектор)"
$Starter = Join-Path $Root "x64\starter.exe"
$Running = Get-Process starter -ErrorAction SilentlyContinue
if (-not $Running) {
    Start-Process -FilePath $Starter -WorkingDirectory (Join-Path $Root "x64")
    Start-Sleep -Seconds 3
    Write-Host "    запущен (cwd = x64 — важно для внедрения)"
} else { Write-Host "    уже запущен (pid $($Running.Id))" }

# ---------- Готово ----------
Write-Host ""
Write-Host "=== УСТАНОВЛЕНО. Дальше вручную: ===" -ForegroundColor Green
Write-Host "1. ZCode: добавить MCP-сервер в ~/.zcode/cli/config.json -> mcp.servers:"
Write-Host '   "mcp-snegopat": {"command": "mcp-snegopat"}  (+ env MCP_SNEGOPAT_CONFIG=<Cfg>, если конфиг не в умолчании)'
Write-Host "2. Свои базы: дописать в own_bases конфига $Cfg (формат: File=`"<путь>`";)"
Write-Host "3. Конфигуратор: starter.exe уже запущен; живой запуск конфигуратора — строго"
Write-Host "   1cv8.exe DESIGNER /F <база>  (без слова DESIGNER откроется лаунчер и повиснет)."
Write-Host "4. Проверка: в ZCode вызвать list_sessions при открытом конфигураторе — мост 1.1."
Write-Host "Подробности: mcp-configurator\README.md (установка, требования, ловушки)."
