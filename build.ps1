# Build the Autopilot AI web dashboard for production.

$projectRoot = $PSScriptRoot
$frontendPath = Join-Path $projectRoot "frontend"
$npmCachePath = Join-Path $projectRoot "down\npm-cache"

Write-Host "Installing frontend dependencies..."
Push-Location $frontendPath
try {
    npm.cmd install --cache $npmCachePath
    if ($LASTEXITCODE -ne 0) {
        throw "npm install failed with exit code $LASTEXITCODE."
    }

    Write-Host "Building frontend..."
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

Write-Host "Build complete. Run with: python -m autopilot_ai.web"
