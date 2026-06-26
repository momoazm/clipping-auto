name: Daily MrBeast clipping

on:
  schedule:
    - cron: "0 21 * * *"
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Dry run (build clips but do NOT really upload)"
        type: boolean
        default: false
      privacy:
        description: "Upload privacy"
        type: choice
        options: [public, unlisted, private]
        default: public
      limit:
        description: "Max clips this run (blank = config default of 6)"
        type: string
        default: ""

permissions:
  contents: write

defaults:
  run:
    shell: powershell

jobs:
  clip-and-upload:
    runs-on: self-hosted
    timeout-minutes: 180
    steps:
      - uses: actions/checkout@v4
        with:
          clean: false

      - name: Python venv + deps
        run: |
          if (-not (Test-Path ".venv")) { python -m venv .venv }
          $py = Join-Path (Get-Location) ".venv\Scripts\python.exe"
          Invoke-Expression "& '$py' -m pip install --upgrade pip"
          Invoke-Expression "& '$py' -m pip install -r requirements.txt"
          if ($LASTEXITCODE -ne 0) { exit 1 }

      - name: Materialize secrets
        env:
          API_ENV: ${{ secrets.API_ENV }}
          YT_TOKEN_JSON: ${{ secrets.YT_TOKEN_JSON }}
          ZERNIO_API: ${{ secrets.ZERNIO_API }}
          ZERNIO_INSTAGRAM_ID: ${{ secrets.ZERNIO_INSTAGRAM_ID }}
          ZERNIO_YOUTUBE_ID: ${{ secrets.ZERNIO_YOUTUBE_ID }}
        run: |
          $root = Get-Location
          # Rebuild your main environment file containing your legacy/AI configurations
          [System.IO.File]::WriteAllText("$root\API.env", $env:API_ENV)
          
          # Safely append your exact Zernio keys to the bottom of the environment file
          $zernio_lines = "`nZERNIO_API=$($env:ZERNIO_API)`nZERNIO_INSTAGRAM_ID=$($env:ZERNIO_INSTAGRAM_ID)`nZERNIO_YOUTUBE_ID=$($env:ZERNIO_YOUTUBE_ID)"
          Add-Content -Path "$root\API.env" -Value $zernio_lines
          
          [System.IO.File]::WriteAllText("$root\token.json", $env:YT_TOKEN_JSON)
          if (-not (Test-Path "API.env")   -or (Get-Item "API.env").Length   -eq 0) { Write-Host "::error::API_ENV secret is empty"; exit 1 }
          if (-not (Test-Path "token.json") -or (Get-Item "token.json").Length -eq 0) { Write-Host "::error::YT_TOKEN_JSON secret is empty"; exit 1 }
          if (Test-Path "$root\cookies.txt") { Remove-Item "$root\cookies.txt" -Force }

      - name: Ensure full ffmpeg on PATH
        run: |
          $ff = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
          if (-not $ff) {
            $c = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($c) { $ff = $c.FullName }
          }
          if (-not $ff) { Write-Host "::error::ffmpeg not found on the runner. Install it: winget install Gyan.FFmpeg"; exit 1 }
          $dir = Split-Path $ff
          Add-Content -Path $env:GITHUB_PATH -Value $dir
          Write-Host "Using ffmpeg: $ff"

      - name: Regenerate SFX + music beds
        run: |
          $py = Join-Path (Get-Location) ".venv\Scripts\python.exe"
          Invoke-Expression "& '$py' tools\build_sfx.py"
          Invoke-Expression "& '$py' tools\build_music.py"

      - name: Run daily clipping
        env:
          IN_PRIVACY: ${{ inputs.privacy }}
          IN_DRYRUN: ${{ inputs.dry_run }}
          IN_LIMIT: ${{ inputs.limit }}
          ZERNIO_API: ${{ secrets.ZERNIO_API }}
          ZERNIO_INSTAGRAM_ID: ${{ secrets.ZERNIO_INSTAGRAM_ID }}
          ZERNIO_YOUTUBE_ID: ${{ secrets.ZERNIO_YOUTUBE_ID }}
        run: |
          $py = Join-Path (Get-Location) ".venv\Scripts\python.exe"
          $argv = @("--privacy", $(if ($env:IN_PRIVACY) { $env:IN_PRIVACY } else { "public" }))
          if ($env:IN_DRYRUN -eq "true") { $argv += "--dry-run" }
          if ($env:IN_LIMIT) { $argv += @("--limit", $env:IN_LIMIT) }
          
          $awake = Start-Process $py -ArgumentList "-c","import ctypes,time; ctypes.windll.kernel32.SetThreadExecutionState(0x80000001); time.sleep(86400)" -PassThru -WindowStyle Hidden
          try {
            $argsStr = $argv -join " "
            Write-Host "run_daily.py $argsStr"
            Invoke-Expression "& '$py' run_daily.py $argsStr"
            if ($LASTEXITCODE -ne 0) { exit 1 }
          } finally {
            Stop-Process -Id $awake.Id -Force -ErrorAction SilentlyContinue
          }

      - name: Commit updated history
        if: ${{ inputs.dry_run != 'true' }}
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add state/clipped_history.json
          git diff --staged --quiet
          if ($LASTEXITCODE -ne 0) {
            git commit -m "chore: update clipped history [skip ci]"
            git push
          } else {
            Write-Host "No history change to commit."
          }
