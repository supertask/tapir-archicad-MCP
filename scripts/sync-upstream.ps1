# Merge the latest upstream (SzamosiMate/tapir-archicad-MCP) master into the fork's working branch,
# reinstall, and run the tests. Nothing is pushed: review the result, then `git push` yourself.
#
#   powershell -ExecutionPolicy Bypass -File scripts\sync-upstream.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\sync-upstream.ps1 -Branch feature/command-timeouts-and-dialogs
param(
    [string]$Branch = "feature/command-timeouts-and-dialogs",
    [string]$Uv = "C:\Users\voyager2\.pyenv\pyenv-win\versions\3.12.3\Scripts\uv.exe"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (git status --porcelain) { throw "Working tree is not clean. Commit or stash your changes first." }
if (-not (git remote | Select-String -SimpleMatch "upstream")) {
    git remote add upstream https://github.com/SzamosiMate/tapir-archicad-MCP.git
    git remote set-url --push upstream DISABLED
}

git fetch upstream --tags
if ($LASTEXITCODE) { throw "git fetch upstream failed." }

# Keep the fork's master an exact mirror of upstream (it never carries fork commits).
git switch master
git merge --ff-only upstream/master
if ($LASTEXITCODE) { throw "master has diverged from upstream/master; inspect it before continuing." }

git switch $Branch
Write-Host "New upstream commits:"
git log --oneline "HEAD..upstream/master"
git merge --no-edit upstream/master
if ($LASTEXITCODE) {
    Write-Host ""
    Write-Host "Merge conflicts. Resolve the files below, then: git add <files>; git commit" -ForegroundColor Yellow
    git diff --name-only --diff-filter=U
    exit 1
}

& $Uv sync
if ($LASTEXITCODE) { throw "uv sync failed." }
& $Uv run pytest -q
if ($LASTEXITCODE) { throw "Tests failed after the merge; fix them before pushing." }

Write-Host ""
Write-Host "Merged and tested. Push when satisfied:  git push origin master $Branch" -ForegroundColor Green
