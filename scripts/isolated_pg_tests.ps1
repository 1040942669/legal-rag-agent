param(
    [string]$PostgresBin = 'D:\Program Files\PostgreSQL\18\bin',
    [string]$ReceiptName = 'isolated-service-pg',
    [string[]]$TestPaths = @('integration_tests/test_general_service_execution.py'),
    [switch]$RestartProbe
)

$ErrorActionPreference = 'Stop'
$taskRepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskTmpRoot = [IO.Path]::GetFullPath((Join-Path $taskRepoRoot '.tmp'))
if (-not [IO.Directory]::Exists($taskTmpRoot)) { [IO.Directory]::CreateDirectory($taskTmpRoot) | Out-Null }
$taskClusterRoot = [IO.Path]::GetFullPath((Join-Path $taskTmpRoot ('isolated-pg-' + [Guid]::NewGuid().ToString('N'))))
if (-not $taskClusterRoot.StartsWith($taskTmpRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Temporary cluster escaped the workspace temporary directory.'
}
$taskDataPath = Join-Path $taskClusterRoot 'data'
$taskPython = Join-Path $taskRepoRoot '.venv\Scripts\python.exe'
$taskInitDb = Join-Path $PostgresBin 'initdb.exe'
$taskPgCtl = Join-Path $PostgresBin 'pg_ctl.exe'
$taskCreatedb = Join-Path $PostgresBin 'createdb.exe'
foreach ($taskBinary in @($taskPython, $taskInitDb, $taskPgCtl)) {
    if (-not (Test-Path -LiteralPath $taskBinary -PathType Leaf)) { throw 'Required local PostgreSQL/Python binary is unavailable.' }
}
if ($RestartProbe -and -not (Test-Path -LiteralPath $taskCreatedb -PathType Leaf)) {
    throw 'Local createdb binary is required for the optional isolated restart probe.'
}
if ($ReceiptName -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Unsafe receipt name.' }
foreach ($taskSelector in $TestPaths) {
    if (-not $taskSelector.StartsWith('integration_tests/')) { throw 'Only explicit integration test selectors are accepted.' }
}
[IO.Directory]::CreateDirectory($taskClusterRoot) | Out-Null
$taskListener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$taskListener.Start()
$taskPort = $taskListener.LocalEndpoint.Port
$taskListener.Stop()
$taskOldUrl = $env:LEGAL_RAG_DATABASE_URL
$taskOldFlag = $env:LEGAL_RAG_INTEGRATION_TEST
$taskStarted = $false
$taskExit = 1
try {
    # Trust is limited to this new synthetic loopback-only cluster. No shared
    # service, shared authentication file or existing data directory is edited.
    & $taskInitDb '-D' $taskDataPath '-U' 'legal_rag_local_test' '--encoding=UTF8' '--locale=C' '-A' 'trust' *> (Join-Path $taskClusterRoot 'initdb.log')
    if ($LASTEXITCODE -ne 0) { throw 'Isolated initdb failed; see the new cluster log.' }
    $taskPgOptions = '-h 127.0.0.1 -p ' + $taskPort + ' -c max_connections=40 -c shared_buffers=32MB'
    # Native subprocess ownership avoids PowerShell descendant-wait and stale
    # Process.ExitCode behavior. CREATE_NO_WINDOW keeps pg_ctl/server hidden;
    # no shell interpolates the argv, and the wait applies only to pg_ctl.
    # Do not create output PIPEs: Windows PostgreSQL descendants can inherit
    # them and keep communicate() waiting after pg_ctl itself has exited.
    $taskHiddenRunner = 'import subprocess,sys; p=subprocess.run(sys.argv[1:],creationflags=subprocess.CREATE_NO_WINDOW,timeout=35); sys.exit(p.returncode)'
    $taskStarted = $true
    & $taskPython '-B' '-c' $taskHiddenRunner $taskPgCtl '-D' $taskDataPath '-l' (Join-Path $taskClusterRoot 'postgres.log') '-o' $taskPgOptions '-w' '-t' '30' 'start' *> (Join-Path $taskClusterRoot 'pg_ctl_start.log')
    if ($LASTEXITCODE -ne 0) { throw 'Isolated PostgreSQL startup failed; see the new cluster log.' }
    $env:LEGAL_RAG_DATABASE_URL = 'postgresql+psycopg://legal_rag_local_test@127.0.0.1:' + $taskPort + '/legal_rag_m3_test_isolated'
    $env:LEGAL_RAG_INTEGRATION_TEST = '1'
    Write-Output ('Isolated synthetic cluster ready: ' + $taskClusterRoot + '; loopback port ' + $taskPort)
    Push-Location $taskRepoRoot
    try {
        & $taskPython '-B' '-m' 'pytest' '-q' @TestPaths ('--junitxml=' + (Join-Path $taskTmpRoot ($ReceiptName + '.xml')))
        $taskExit = $LASTEXITCODE
        if ($taskExit -eq 0 -and $RestartProbe) {
            $taskRestartDatabase = 'legal_rag_m3_test_restart'
            & $taskCreatedb '-h' '127.0.0.1' '-p' $taskPort '-U' 'legal_rag_local_test' '-T' 'template0' $taskRestartDatabase *> (Join-Path $taskClusterRoot 'createdb_restart.log')
            if ($LASTEXITCODE -ne 0) { throw 'The isolated synthetic restart database could not be created.' }
            $env:LEGAL_RAG_DATABASE_URL = 'postgresql+psycopg://legal_rag_local_test@127.0.0.1:' + $taskPort + '/' + $taskRestartDatabase
            $taskRestartReceipt = Join-Path $taskClusterRoot 'restart-prepared.json'
            & $taskPython '-B' 'scripts/m3_restart_probe.py' 'prepare' '--receipt' $taskRestartReceipt *> (Join-Path $taskClusterRoot 'restart_prepare.log')
            if ($LASTEXITCODE -ne 0) { throw 'Isolated restart prepare failed; see the new cluster log.' }
            $taskRestartTarget = [IO.Path]::GetFullPath($taskDataPath)
            if (-not $taskRestartTarget.StartsWith($taskClusterRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Refusing to restart an unexpected data directory.'
            }
            & $taskPython '-B' '-c' $taskHiddenRunner $taskPgCtl '-D' $taskRestartTarget '-m' 'fast' '-w' '-t' '30' 'stop' *> (Join-Path $taskClusterRoot 'restart_stop.log')
            if ($LASTEXITCODE -ne 0) { throw 'Isolated PostgreSQL restart stop was not confirmed.' }
            & $taskPython '-B' '-c' $taskHiddenRunner $taskPgCtl '-D' $taskRestartTarget '-l' (Join-Path $taskClusterRoot 'postgres.log') '-o' $taskPgOptions '-w' '-t' '30' 'start' *> (Join-Path $taskClusterRoot 'restart_start.log')
            if ($LASTEXITCODE -ne 0) { throw 'Isolated PostgreSQL restart start was not confirmed.' }
            & $taskPython '-B' 'scripts/m3_restart_probe.py' 'verify' '--receipt' $taskRestartReceipt *> (Join-Path $taskClusterRoot 'restart_verify.log')
            if ($LASTEXITCODE -ne 0) { throw 'Fresh-process isolated restart verify failed; see the new cluster log.' }
            $taskRestartFacts = Get-Content -LiteralPath (Join-Path $taskClusterRoot 'restart_verify.log') -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($taskRestartFacts.stage -ne 'verified_after_service_restart' -or $taskRestartFacts.database.migration_revision -ne '0008_execution_money') {
                throw 'Isolated restart receipt did not prove the current candidate schema.'
            }
            Write-Output ('Actual isolated PostgreSQL restart verified by a fresh process; migration head ' + $taskRestartFacts.database.migration_revision + '; receipt ' + $taskRestartReceipt)
        }
    } finally { Pop-Location }
} finally {
    $env:LEGAL_RAG_DATABASE_URL = $taskOldUrl
    $env:LEGAL_RAG_INTEGRATION_TEST = $taskOldFlag
    if ($taskStarted -and (Test-Path -LiteralPath (Join-Path $taskDataPath 'postmaster.pid'))) {
        # Validate the exact self-created target again before stopping it.
        $taskStopTarget = [IO.Path]::GetFullPath($taskDataPath)
        if (-not $taskStopTarget.StartsWith($taskClusterRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to stop an unexpected data directory.'
        }
        & $taskPython '-B' '-c' $taskHiddenRunner $taskPgCtl '-D' $taskStopTarget '-m' 'fast' '-w' '-t' '30' 'stop' *> (Join-Path $taskClusterRoot 'pg_ctl_stop.log')
        if ($LASTEXITCODE -ne 0) { throw 'The isolated cluster could not confirm clean shutdown.' }
        Write-Output 'The self-created isolated cluster was stopped; logs and synthetic data remain in the ignored temporary directory.'
    }
}
exit $taskExit
