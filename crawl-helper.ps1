# crawl-helper.ps1 - table views and shortcuts for the websearch crawl API.
#
# Load it once per PowerShell window (note the leading dot and space):
#     . .\crawl-helper.ps1
# It asks for the Function key (hidden). To skip the prompt: $env:WEBSEARCH_KEY = "<key>" first.
#
# Commands:
#     Show-Crawl                 one row per crawl-enabled domain: latest job, counts, progress
#     Show-Crawl muse            only domains containing "muse"
#     Show-Crawl muse -Watch     refresh every 30 seconds (Ctrl+C to stop; -Every 10 to change)
#     Show-CrawlPages muse       what happened to the latest job's pages, grouped, plus failed URLs
#     Start-Crawl www.themuse.com   start a crawl and show the page limit it was given
#     Start-Crawl www.themuse.com -Wait   ...then follow it to the end with a progress bar
#     Wait-Crawl muse            follow the latest crawl of a domain: progress bar, pages/min, ETA, stall warning
#     Resume-Crawl muse          re-run the failed tasks of the domain's latest job
#     Get-CrawlJobId muse        the full job id of the domain's latest job
#     Call GET "/api/health"     any raw call

# Run as a normal script, the commands below would vanish when the script ends. Stop early and say how to load it.
if ($MyInvocation.InvocationName -ne ".") {
    Write-Warning "This file must be loaded with a leading dot and a space, so its commands stay in your session:"
    Write-Host "    . .\crawl-helper.ps1" -ForegroundColor Yellow
    return
}

$script:AzureUrl = if ($env:WEBSEARCH_URL) { $env:WEBSEARCH_URL.TrimEnd('/') } else { "https://websearch-h9arcweeftfrd8e0.eastus-01.azurewebsites.net" }
if ($env:WEBSEARCH_KEY) {
    $plainKey = $env:WEBSEARCH_KEY
} else {
    $secure   = Read-Host "Websearch Function key" -AsSecureString
    $plainKey = [System.Net.NetworkCredential]::new("", $secure).Password
}
$script:Headers = @{ "x-functions-key" = $plainKey }
Remove-Variable plainKey -ErrorAction SilentlyContinue

function Call($method, $path, $body = $null) {
    $p = @{ Method = $method; Uri = "$script:AzureUrl$path"; Headers = $script:Headers; ContentType = "application/json" }
    if ($body) { $p.Body = ($body | ConvertTo-Json -Depth 5) }
    try { Invoke-RestMethod @p }
    catch { Write-Warning "$method $path -> $($_.Exception.Message)"; $_.ErrorDetails.Message }
}

function Format-Stamp($s) {
    if (-not $s) { return "-" }
    try { ([datetime]$s).ToUniversalTime().ToString("MM-dd HH:mm") } catch { "$s" }
}

function Get-JobId($job) {
    if ($job.job_id) { $job.job_id } else { $job.RowKey }
}

$script:TerminalStatuses = @("done", "done+errors", "failed", "cancelled")

function Format-Bar($ended, $total, [int]$width = 8) {
    if (-not $total -or [int]$total -le 0) { return "-" }
    $p = [Math]::Min(1.0, [double]$ended / [double]$total)
    $n = [int][Math]::Round($p * $width)
    "[" + ("#" * $n) + ("-" * ($width - $n)) + "] " + ("{0,3:N0}%" -f (100 * $p))
}

function Short-Status($s) {
    switch ($s) {
        "completed"             { "done" }
        "completed_with_errors" { "done+errors" }
        default                 { $s }
    }
}

function Get-DashboardRows([string]$Domain) {
    $d = Call GET "/api/crawl-dashboard"
    if (-not $d -or -not $d.domains) {
        Write-Warning "The dashboard returned no domains. Raw response: $($d | ConvertTo-Json -Depth 3 -Compress)"
        return @()
    }
    $rows = foreach ($entry in $d.domains) {
        $j = $entry.latest_job
        $found = [int]$j.pages_discovered
        $ended = [int]$j.pages_processed + [int]$j.pages_skipped + [int]$j.pages_failed
        [pscustomobject]@{
            Domain   = $entry.domain
            Status   = if ($j) { Short-Status $j.status } else { "never crawled" }
            Limit    = $j.page_limit
            Found    = $j.pages_discovered
            Done     = $j.pages_processed
            Skip     = $j.pages_skipped
            Fail     = $j.pages_failed
            Pend     = $j.pending
            Progress = Format-Bar $ended $found
            Ended    = $ended
            Chunks   = $j.chunks_indexed
            Updated  = Format-Stamp $j.updated_utc
            Job      = if ($j) { (Get-JobId $j).Substring(0, 8) } else { "" }
            JobId    = if ($j) { Get-JobId $j } else { "" }
        }
    }
    if ($Domain) { $rows = $rows | Where-Object { $_.Domain -like "*$Domain*" } }
    $rows | Sort-Object Domain
}

function Get-CrawlJobId([string]$Domain) {
    $job = Find-LatestJob $Domain
    if ($job) { $job.JobId }
}

function Show-Crawl([string]$Domain, [switch]$Watch, [int]$Every = 30) {
    do {
        if ($Watch) { Clear-Host }
        $rows = @(Get-DashboardRows $Domain)
        if (-not $rows) { Write-Host "No matching domains."; return }
        # JobId (full) stays on the rows for the other commands but is not shown here; "Job" is its first 8 characters.
        $rows | Format-Table Domain, Status, Limit, Found, Done, Skip, Fail, Pend, Progress, Chunks, Updated, Job -AutoSize | Out-Host
        $running = @($rows | Where-Object { $_.Status -in "queued", "running", "in_progress" }).Count
        $failing = @($rows | Where-Object { [int]$_.Fail -gt 0 }).Count
        $never   = @($rows | Where-Object { $_.Status -eq "never crawled" }).Count
        Write-Host ("{0} domains | {1} active | {2} with failed pages | {3} never crawled | {4} UTC" -f `
            $rows.Count, $running, $failing, $never, (Get-Date).ToUniversalTime().ToString("HH:mm:ss"))
        if ($Watch) { Start-Sleep -Seconds $Every }
    } while ($Watch)
}

function Find-LatestJob([string]$Domain) {
    $rows = @(Get-DashboardRows $Domain | Where-Object { $_.JobId })
    if ($rows.Count -eq 0) { Write-Warning "No crawl job found for '$Domain'."; return $null }
    if ($rows.Count -gt 1) { Write-Warning "'$Domain' matches $($rows.Count) domains ($($rows.Domain -join ', ')). Be more specific."; return $null }
    $rows[0]
}

function Show-CrawlPages([string]$Domain) {
    $job = Find-LatestJob $Domain
    if (-not $job) { return }
    Write-Host "$($job.Domain)  job $($job.JobId)  status $($job.Status)"
    $r = Call GET "/api/crawl/$($job.JobId)/pages"
    if (-not $r.pages) { Write-Host "No page records."; return }
    Write-Host "`nPages by outcome ($($r.count) total):"
    $r.pages | ForEach-Object {
        [pscustomobject]@{ Status = $_.status; Reason = if ($_.reason) { $_.reason } else { "" } }
    } | Group-Object Status, Reason | Sort-Object Count -Descending |
        Select-Object Count, @{ n = "Outcome (status, reason)"; e = { $_.Name } } | Format-Table -AutoSize | Out-Host
    $bad = @($r.pages | Where-Object { $_.status -in "failed", "publish_failed" })
    if ($bad.Count) {
        Write-Host "First failed pages:"
        $bad | Select-Object -First 10 url, attempts, @{ n = "error"; e = { "$($_.error)".Substring(0, [Math]::Min(80, "$($_.error)".Length)) } } | Format-Table -AutoSize -Wrap | Out-Host
    }
}

function Wait-Crawl([string]$Domain, [int]$Every = 15, [int]$StallMinutes = 10) {
    $first = Find-LatestJob $Domain
    if (-not $first) { return }
    $begin = Get-Date
    $baseEnded = [int]$first.Ended
    $lastEnded = $baseEnded
    $lastMove = Get-Date
    $warned = $false
    Write-Host "Following $($first.Domain), job $($first.Job). Ctrl+C stops watching; the crawl keeps running."
    while ($true) {
        $row = Find-LatestJob $Domain
        if (-not $row) { return }
        $ended = [int]$row.Ended
        $found = [int]$row.Found
        if ($ended -gt $lastEnded) { $lastEnded = $ended; $lastMove = Get-Date; $warned = $false }
        $minutes = [Math]::Max(0.1, ((Get-Date) - $begin).TotalMinutes)
        $rate = ($ended - $baseEnded) / $minutes
        $remaining = [Math]::Max(0, $found - $ended)
        $eta = if ($rate -gt 0 -and $remaining -gt 0) { "{0:N0} min" -f [Math]::Ceiling($remaining / $rate) } else { "-" }
        $pct = if ($found -gt 0) { [int][Math]::Min(100, 100 * $ended / $found) } else { 0 }
        $status = "$($row.Status): $ended of $found pages ($($row.Done) done, $($row.Skip) skipped, $($row.Fail) failed, $($row.Pend) pending)"
        Write-Progress -Activity "Crawling $($row.Domain)" -Status $status -PercentComplete $pct
        Write-Host ("[{0:HH:mm:ss}] {1,-12} {2}  {3,5:N1} pages/min  ETA {4}" -f (Get-Date), $row.Status, (Format-Bar $ended $found 20), $rate, $eta)
        if ($row.Status -in $script:TerminalStatuses) {
            Write-Progress -Activity "Crawling $($row.Domain)" -Completed
            Write-Host ("Finished: {0}. {1} done, {2} skipped, {3} failed, {4} chunks indexed." -f $row.Status, $row.Done, $row.Skip, $row.Fail, $row.Chunks) -ForegroundColor Green
            if ([int]$row.Fail -gt 0) { Write-Host "Failed pages: Show-CrawlPages $Domain  then  Resume-Crawl $Domain" -ForegroundColor Yellow }
            return
        }
        if (-not $warned -and ((Get-Date) - $lastMove).TotalMinutes -ge $StallMinutes) {
            $warned = $true
            Write-Warning "No page has finished in $StallMinutes minutes. The queue may be stalled or a site may be refusing requests. Check Show-CrawlPages $Domain, the poison queue, and consider Resume-Crawl $Domain."
        }
        Start-Sleep -Seconds $Every
    }
}

function Start-Crawl([string]$Domain, [switch]$Wait) {
    if (-not $Domain) { Write-Warning "Give the full domain, e.g. Start-Crawl www.themuse.com"; return }
    $r = Call POST "/api/crawl" @{ domains = @($Domain) }
    if (-not $r.jobs) { Write-Warning "No job was created. Response: $($r | ConvertTo-Json -Depth 3 -Compress)"; return }
    $r.jobs | ForEach-Object {
        [pscustomobject]@{ Domain = $_.domain; Status = $_.status; PageLimit = $_.page_limit; MaxDepth = $_.max_depth; JobId = $_.job_id }
    } | Format-Table -AutoSize -Wrap | Out-Host
    Write-Host "Check PageLimit: it should match MAX_PAGES_BY_DOMAIN for this domain (e.g. 500 for The Muse)."
    if ($Wait) { Start-Sleep -Seconds 3; Wait-Crawl $Domain }
}

function Resume-Crawl([string]$Domain) {
    $job = Find-LatestJob $Domain
    if (-not $job) { return }
    Write-Host "Resuming failed tasks for $($job.Domain), job $($job.JobId) ..."
    Call POST "/api/crawl/$($job.JobId)/resume" | ConvertTo-Json -Depth 3
}

Write-Host "Loaded. Try: Show-Crawl | Wait-Crawl muse | Show-CrawlPages muse | Start-Crawl <domain> -Wait | Resume-Crawl <domain>"