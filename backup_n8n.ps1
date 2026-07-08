# Backs up the n8n database + encryption key + a JSON export of all workflows
# into a timestamped folder. Run anytime: powershell -File .\backup_n8n.ps1
# For a guaranteed-consistent DB copy, stop n8n first (SQLite copy while
# running is usually fine, but a stopped instance is safest).

$ErrorActionPreference = "Stop"
$n8nDir   = Join-Path $env:USERPROFILE ".n8n"
$stamp    = Get-Date -Format "yyyy-MM-dd_HHmmss"
$dest     = Join-Path $PSScriptRoot "n8n_backups\$stamp"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# 1. Critical files: DB + encryption key
foreach ($f in @("database.sqlite", "config")) {
    $src = Join-Path $n8nDir $f
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $dest $f) -Force
        Write-Host "  copied $f"
    } else {
        Write-Warning "  missing $f at $src"
    }
}

# 2. Portable JSON export of every workflow (human-readable, no secrets)
try {
    & npx n8n export:workflow --all --pretty --output (Join-Path $dest "workflows.json")
    Write-Host "  exported workflows.json"
} catch {
    Write-Warning "  CLI export skipped (n8n must be installed/closed): $_"
}

Write-Host ""
Write-Host "Backup complete -> $dest"
