<#
.SYNOPSIS
    Project Olorin - environment setup for Windows.

.DESCRIPTION
    Creates the venv, installs dependencies, sets up the Ollama model,
    and configures .env. Safe to re-run.

.NOTES
    Run from the repo root: .\setup.ps1
#>

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot

function Write-Step($msg) {
    Write-Host ""
    Write-Host "== $msg ==" -ForegroundColor Cyan
}

function Test-CommandExists($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# --- Python ---
Write-Step "Checking Python"

$usePyLauncher = $false
if (Test-CommandExists "py") {
    py -3.11 --version *> $null
    if ($LASTEXITCODE -eq 0) {
        $usePyLauncher = $true
    }
}

if ($usePyLauncher) {
    $pyVersionOutput = py -3.11 --version 2>&1
    Write-Host "Found: $pyVersionOutput (via py launcher)"
} else {
    if (-not (Test-CommandExists "python")) {
        Write-Host "python not found on PATH. Install Python 3.11+ and re-run." -ForegroundColor Red
        exit 1
    }
    $pyVersionOutput = python --version 2>&1
    Write-Host "Found: $pyVersionOutput"
    if ($pyVersionOutput -notmatch "Python 3\.11") {
        Write-Host "WARNING: requirements.txt pins torch for Python 3.11 (cp311 wheels). $pyVersionOutput will likely fail on torch install." -ForegroundColor Yellow
    }
}

# --- venv ---
Write-Step "Setting up virtual environment"
$venvPath = Join-Path $repoRoot "venv"
if (Test-Path $venvPath) {
    Write-Host "venv already exists - skipping."
} else {
    if ($usePyLauncher) {
        py -3.11 -m venv $venvPath
    } else {
        python -m venv $venvPath
    }
}

$venvPython = Join-Path $venvPath "Scripts\python.exe"
$venvPip = Join-Path $venvPath "Scripts\pip.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "venv creation failed - $venvPython not found." -ForegroundColor Red
    exit 1
}

# --- Dependencies ---
Write-Step "Installing dependencies"
& $venvPython -m pip install --upgrade pip
& $venvPip install -r (Join-Path $repoRoot "requirements.txt")

Write-Host ""
Write-Host "Manual step required: playwright install chromium" -ForegroundColor Yellow

# --- Ollama ---
Write-Step "Checking Ollama"
if (-not (Test-CommandExists "ollama")) {
    Write-Host "ollama not found on PATH. Install from https://ollama.com and re-run." -ForegroundColor Yellow
} else {
    $existingModels = ollama list 2>&1 | Out-String

    if ($existingModels -notmatch "local:latest") {
        if ($existingModels -notmatch "qwen3:8b") {
            Write-Host "Pulling qwen3:8b ..."
            ollama pull qwen3:8b
        }

        $modelfilePath = Join-Path $repoRoot "local.modelfile"
        ollama show qwen3:8b --modelfile | Out-File -Encoding utf8 $modelfilePath
        Add-Content -Path $modelfilePath -Value "PARAMETER num_ctx 16384"
        ollama create local:latest -f $modelfilePath
        Write-Host "Created local:latest."
    } else {
        Write-Host "local:latest already exists - skipping."
    }
}

# --- .env ---
Write-Step "Configuring .env"
$envPath = Join-Path $repoRoot ".env"

if (Test-Path $envPath) {
    Write-Host ".env already exists - leaving it untouched."
} else {
    Write-Host "GROQ_API_KEY is required. Everything else is optional - press Enter to skip."
    Write-Host ""

    $groqKey = Read-Host "GROQ_API_KEY (required)"
    while ([string]::IsNullOrWhiteSpace($groqKey)) {
        Write-Host "GROQ_API_KEY is required." -ForegroundColor Yellow
        $groqKey = Read-Host "GROQ_API_KEY (required)"
    }

    $cerebrasKey = Read-Host "CEREBRAS_API_KEY (optional)"
    $tavilyKey   = Read-Host "TAVILY_API_KEY (optional)"
    $jinaKey     = Read-Host "JINA_API_KEY (optional)"
    $serperKey   = Read-Host "SERPER_API_KEY (optional)"
    $exaKey      = Read-Host "EXA_API_KEY (optional)"

    $envLines = @("GROQ_API_KEY=$groqKey")
    if ($cerebrasKey) { $envLines += "CEREBRAS_API_KEY=$cerebrasKey" }
    if ($tavilyKey)   { $envLines += "TAVILY_API_KEY=$tavilyKey" }
    if ($jinaKey)     { $envLines += "JINA_API_KEY=$jinaKey" }
    if ($serperKey)   { $envLines += "SERPER_API_KEY=$serperKey" }
    if ($exaKey)      { $envLines += "EXA_API_KEY=$exaKey" }

    $envLines | Out-File -Encoding utf8 $envPath
    Write-Host ".env written." -ForegroundColor Green
}

# --- indexer_core ---
Write-Step "Checking indexer_core"
$indexerBinary = Join-Path $repoRoot "indexer_core\target\release\indexer_core.exe"
if (Test-Path $indexerBinary) {
    Write-Host "indexer_core.exe already built."
} else {
    Write-Host "indexer_core.exe not found." -ForegroundColor Yellow
    if (Test-CommandExists "cargo") {
        Write-Host "Build with: cd indexer_core; cargo build --release; cd .." -ForegroundColor Yellow
    } else {
        Write-Host "Install Rust from https://rustup.rs, then: cd indexer_core; cargo build --release; cd .." -ForegroundColor Yellow
    }
}

# --- Done ---
Write-Step "Setup complete"
Write-Host "Next steps:"
Write-Host "  1. .\venv\Scripts\Activate.ps1"
Write-Host "  2. playwright install chromium (if not done already)"
Write-Host "  3. Build indexer_core if flagged above"
Write-Host "  4. python cli.py index ."
Write-Host "  5. python cli.py ask `"what is this project?`""
