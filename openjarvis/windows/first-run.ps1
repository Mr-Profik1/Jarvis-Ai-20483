# OpenJarvis: запуск на Windows.
# При первом открытии сам ставит всё нужное и скачивает модель, потом запускает jarvis.
# Повторные запуски пропускают уже сделанное и стартуют сразу.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
Set-Location $root

# Модель по умолчанию лёгкая (~1.3 ГБ), подходит для ПК с 4 ГБ ОЗУ.
# Чтобы взять другую, впишите её имя в файл windows\model.txt (например qwen3:4b).
$Model = 'llama3.2:1b'
$modelFile = Join-Path $here 'model.txt'
if (Test-Path $modelFile) {
    $custom = (Get-Content $modelFile -Raw).Trim()
    if ($custom) { $Model = $custom }
}

function Say($text) { Write-Host "[Jarvis] $text" -ForegroundColor Cyan }

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;$env:USERPROFILE\.local\bin;$env:LOCALAPPDATA\Programs\Ollama"
}

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

try {
    Update-SessionPath

    # 1. uv: сам скачает нужный Python (3.11-3.13), системный Python не нужен.
    if (-not (Test-Command 'uv')) {
        Say 'Ставлю uv (менеджер Python)...'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        Update-SessionPath
        if (-not (Test-Command 'uv')) { throw 'uv не установился. Закройте окно и запустите start.bat ещё раз.' }
    }

    # 2. Ollama: движок для локальных моделей.
    if (-not (Test-Command 'ollama')) {
        Say 'Ставлю Ollama...'
        $installed = $false
        if (Test-Command 'winget') {
            try {
                winget install -e --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements
                $installed = $true
            } catch { $installed = $false }
        }
        Update-SessionPath
        if (-not (Test-Command 'ollama')) {
            Say 'winget не сработал, скачиваю установщик Ollama напрямую...'
            $setup = Join-Path $env:TEMP 'OllamaSetup.exe'
            Invoke-WebRequest https://ollama.com/download/OllamaSetup.exe -OutFile $setup
            Start-Process $setup -ArgumentList '/SILENT' -Wait
            Update-SessionPath
        }
        if (-not (Test-Command 'ollama')) { throw 'Ollama не установился. Поставьте вручную с https://ollama.com/download и запустите start.bat снова.' }
    }

    # 3. Сервер Ollama должен работать.
    $up = $false
    try { Invoke-WebRequest http://127.0.0.1:11434 -UseBasicParsing -TimeoutSec 3 | Out-Null; $up = $true } catch { $up = $false }
    if (-not $up) {
        Say 'Запускаю Ollama...'
        Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden
        for ($i = 0; $i -lt 30 -and -not $up; $i++) {
            Start-Sleep -Seconds 1
            try { Invoke-WebRequest http://127.0.0.1:11434 -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true } catch { $up = $false }
        }
        if (-not $up) { throw 'Ollama не запустилась.' }
    }

    # 4. Модель: качается только если её ещё нет.
    $have = (& ollama list) -join "`n"
    if ($have -notmatch [regex]::Escape($Model)) {
        Say "Скачиваю модель $Model при первом запуске. Это может занять несколько минут..."
        & ollama pull $Model
        if ($LASTEXITCODE -ne 0) { throw "Не удалось скачать модель $Model. Проверьте интернет и запустите start.bat снова." }
    } else {
        Say "Модель $Model уже скачана."
    }

    # 5. Зависимости OpenJarvis (первый раз долго, дальше быстро).
    if (-not (Test-Path (Join-Path $root '.venv'))) {
        Say 'Ставлю OpenJarvis, первый раз это несколько минут...'
    }
    & uv sync --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'uv sync не удался.' }

    # 6. Настройка и выбор модели.
    $cfgPath = Join-Path $env:USERPROFILE '.openjarvis\config.toml'
    if (-not (Test-Path $cfgPath)) {
        Say 'Первичная настройка...'
        & uv run --python 3.12 jarvis init --no-download --no-scan --no-analytics --engine ollama
        if ($LASTEXITCODE -ne 0) { throw 'jarvis init не удался.' }
    }
    if (Test-Path $cfgPath) {
        $text = Get-Content $cfgPath -Raw
        $new = [regex]::Replace($text, '(?m)^default_model\s*=\s*".*"', "default_model = `"$Model`"")
        if ($new -ne $text) { Set-Content -Path $cfgPath -Value $new -Encoding UTF8 }
    }

    # 7. Запуск.
    Say 'Запускаю Jarvis...'
    & uv run --python 3.12 jarvis
}
catch {
    Write-Host ''
    Write-Host "[Ошибка] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
