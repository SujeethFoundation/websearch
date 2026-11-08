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
#     Show-CrawlPaths tealhq     the latest job's pages grouped by URL section (/post, /job ...) and outcome
#     Show-CrawlTimeline tealhq -Path /job   when pages under a path were discovered, done and rejected (UTC)
#     Start-Crawl www.themuse.com   start a crawl and show the page limit it was given
#     Start-Crawl www.themuse.com -Wait   ...then follow it to the end with a progress bar
#     Wait-Crawl muse            follow the latest crawl of a domain: progress bar, pages/min, ETA, stall warning
#     Resume-Crawl muse          re-run the failed tasks of the domain's latest job
#     Recover-Crawl muse         put the latest job's stuck pages back on the queue (stale for 5+ minutes; -StaleMinutes 25 to change)
#     Get-CrawlJobId muse        the full job id of the domain's latest job
#     Stop-Crawl muse            cancel every open job of the domain (asks first; -Force skips the question)
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
        # TOTAL row: sums over the rows shown. Limit and Job belong to one domain each, so they stay blank.
        $sum = { param($name) [int](($rows | ForEach-Object { [int]$_.$name } | Measure-Object -Sum).Sum) }
        $foundAll = & $sum "Found"
        $endedAll = & $sum "Ended"
        $total = [pscustomobject]@{
            Domain = "TOTAL ($($rows.Count))"; Status = ""; Limit = ""; Found = $foundAll; Done = (& $sum "Done")
            Skip = (& $sum "Skip"); Fail = (& $sum "Fail"); Pend = (& $sum "Pend"); Progress = (Format-Bar $endedAll $foundAll)
            Chunks = (& $sum "Chunks"); Updated = ""; Job = ""
        }
        $shown = @($rows) + $total
        $shown | Format-Table Domain, Status, Limit, Found, Done, Skip, Fail, Pend, Progress, Chunks, Updated, Job -AutoSize | Out-Host
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
    # The app counts the pages by status and reason, so a 10,000-page job is not downloaded to be counted here.
    $r = Call GET "/api/crawl/$($job.JobId)/pages?summary=true"
    $groups = $r.groups
    if (-not $groups -and $r.pages) {
        # An app from before the summary route returned every page: group them here instead.
        $groups = @($r.pages | ForEach-Object { [pscustomobject]@{ status = $_.status; reason = if ($_.reason) { $_.reason } else { "" } } } |
            Group-Object status, reason | ForEach-Object { [pscustomobject]@{ status = $_.Group[0].status; reason = $_.Group[0].reason; count = $_.Count } })
    }
    if (-not $groups) { Write-Host "No page records."; return }
    Write-Host "`nPages by outcome ($($r.count) total):"
    $groups | Sort-Object count -Descending | ForEach-Object {
        [pscustomobject]@{ Count = $_.count; "Outcome (status, reason)" = "$($_.status), $($_.reason)" }
    } | Format-Table -AutoSize | Out-Host
    # Only the failed pages are fetched, up to ten of each kind.
    $bad = @()
    foreach ($state in "failed", "publish_failed") {
        $more = Call GET "/api/crawl/$($job.JobId)/pages?status=$state&limit=10"
        if ($more.pages) { $bad += @($more.pages | Where-Object { $_.status -eq $state }) }
    }
    if ($bad.Count) {
        Write-Host "First failed pages:"
        $bad | Select-Object -First 10 url, attempts, @{ n = "error"; e = { "$($_.error)".Substring(0, [Math]::Min(80, "$($_.error)".Length)) } } | Format-Table -AutoSize -Wrap | Out-Host
    }
    # Pages that are waiting or in progress, with their tries so far and the last error. A stuck crawl shows up here.
    $waiting = @()
    foreach ($state in "queued", "processing") {
        $more = Call GET "/api/crawl/$($job.JobId)/pages?status=$state&limit=10"
        if ($more.pages) { $waiting += @($more.pages | Where-Object { $_.status -eq $state }) }
    }
    if ($waiting.Count) {
        Write-Host "Pages still waiting or in progress (first 10):"
        $waiting | Select-Object -First 10 url, status, attempts, @{ n = "last error"; e = { "$($_.last_error)".Substring(0, [Math]::Min(80, "$($_.last_error)".Length)) } }, updated_utc | Format-Table -AutoSize -Wrap | Out-Host
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

function Get-OpenJobs([string]$Domain) {
    # Every job that is not finished, newest first, for domains containing the text. Reads the latest 100 jobs.
    $r = Call GET "/api/crawl-status"
    $finished = "completed", "completed_with_errors", "failed", "cancelled"
    @($r.jobs | Where-Object { $_.domain -like "*$Domain*" -and $_.status -notin $finished })
}

function Stop-Crawl([string]$Domain, [switch]$Force) {
    # Cancels every open job of the domain, not only the latest one: an older job that is still open stops a new crawl from starting.
    $jobs = @(Get-OpenJobs $Domain)
    if (-not $jobs.Count) { Write-Host "No open crawl job matches '$Domain'; nothing to cancel."; return }
    $names = @($jobs.domain | Sort-Object -Unique)
    if ($names.Count -gt 1) { Write-Warning "'$Domain' matches $($names.Count) domains ($($names -join ', ')). Be more specific."; return }
    $jobs | Format-Table job_id, domain, status, created_utc, updated_utc, pages_discovered, pages_processed, pending -AutoSize | Out-Host
    if (-not $Force) {
        $answer = Read-Host "Cancel $($jobs.Count) job(s)? Pages already crawled stay in the index; queued pages are dropped. (yes/no)"
        if ($answer -ne "yes") { Write-Host "Not cancelled."; return }
    }
    foreach ($j in $jobs) { Call POST "/api/crawl/$($j.job_id)/cancel" | Out-Null; Write-Host "Cancelled $($j.job_id)." }
}

function Show-CrawlPaths([string]$Domain, [int]$Depth = 1, [int]$Top = 20) {
    $job = Find-LatestJob $Domain
    if (-not $job) { return }
    Write-Host "$($job.Domain)  job $($job.JobId)  status $($job.Status)  - reading page records, this can take a while for big jobs ..."
    $r = Call GET "/api/crawl/$($job.JobId)/pages"
    if (-not $r.pages) { Write-Host "No page records."; return }
    $rows = foreach ($pg in $r.pages) {
        $path = ""
        try { $path = ([uri]$pg.url).AbsolutePath.Trim("/") } catch { }
        $seg = ($path -split "/" | Where-Object { $_ } | Select-Object -First $Depth) -join "/"
        [pscustomobject]@{ Path = if ($seg) { "/$seg" } else { "(home)" }; Status = $pg.status; Reason = $pg.reason }
    }
    $table = $rows | Group-Object Path | ForEach-Object {
        $g = $_.Group
        [pscustomobject]@{
            Path     = $_.Name
            Total    = $_.Count
            Done     = @($g | Where-Object { $_.Status -in "processed", "unchanged" }).Count
            Excluded = @($g | Where-Object { $_.Reason -eq "path_excluded" }).Count
            Skipped  = @($g | Where-Object { $_.Status -eq "skipped" -and $_.Reason -ne "path_excluded" }).Count
            Failed   = @($g | Where-Object { $_.Status -in "failed", "publish_failed" }).Count
            Queued   = @($g | Where-Object { $_.Status -in "queued", "processing" }).Count
        }
    } | Sort-Object Total -Descending
    Write-Host "`n$($r.count) page records. Excluded = skipped because the path rules (include_paths / exclude_paths) did not allow them."
    # TOTAL row: counts every section, including any hidden by -Top.
    $all = @($table)
    $sum = { param($name) [int](($all | Measure-Object -Property $name -Sum).Sum) }
    $totalRow = [pscustomobject]@{ Path = "TOTAL"; Total = (& $sum "Total"); Done = (& $sum "Done"); Excluded = (& $sum "Excluded")
                                   Skipped = (& $sum "Skipped"); Failed = (& $sum "Failed"); Queued = (& $sum "Queued") }
    $shown = @($all | Select-Object -First $Top) + $totalRow
    $shown | Format-Table -AutoSize | Out-Host
    if (@($table).Count -gt $Top) { Write-Host "... and $(@($table).Count - $Top) more sections. Use -Top to show more, -Depth 2 for finer paths." }
}

function Show-CrawlTimeline([string]$Domain, [string]$Path = "/job", [int]$BucketMinutes = 60) {
    $job = Find-LatestJob $Domain
    if (-not $job) { return }
    Write-Host "$($job.Domain)  job $($job.JobId)  status $($job.Status)  - reading page records ..."
    $r = Call GET "/api/crawl/$($job.JobId)/pages"
    if (-not $r.pages) { Write-Host "No page records."; return }
    $Path = "/" + $Path.Trim("/")
    $bucket = [long]($BucketMinutes * 600000000)      # ticks per bucket
    $floor = { param($t) [datetime]::new([long]([Math]::Floor($t.ToUniversalTime().Ticks / $bucket) * $bucket), [DateTimeKind]::Utc) }
    $pages = foreach ($pg in $r.pages) {
        $p = ""; try { $p = ([uri]$pg.url).AbsolutePath } catch { }
        $trim = $p.TrimEnd("/")
        [pscustomobject]@{
            Under   = ($trim -eq $Path) -or $trim.StartsWith("$Path/")
            Status  = $pg.status
            Reason  = $pg.reason
            Created = if ($pg.created_utc) { [datetime]$pg.created_utc } else { $null }
            Updated = if ($pg.updated_utc) { [datetime]$pg.updated_utc } else { $null }
        }
    }
    $keys = @($pages | ForEach-Object { if ($_.Created) { & $floor $_.Created } }) + @($pages | ForEach-Object { if ($_.Updated) { & $floor $_.Updated } }) | Sort-Object -Unique
    $table = foreach ($k in $keys) {
        $inB = { param($t) $t -and ((& $floor $t) -eq $k) }
        [pscustomobject]@{
            "UTC"            = $k.ToString("MM-dd HH:mm")
            "Found(all)"     = @($pages | Where-Object { & $inB $_.Created }).Count
            "Found($Path)"   = @($pages | Where-Object { $_.Under -and (& $inB $_.Created) }).Count
            "Done($Path)"    = @($pages | Where-Object { $_.Under -and $_.Status -in "processed", "unchanged" -and (& $inB $_.Updated) }).Count
            "Rejected($Path)" = @($pages | Where-Object { $_.Under -and $_.Reason -eq "path_excluded" -and (& $inB $_.Updated) }).Count
        }
    }
    $table | Format-Table -AutoSize | Out-Host
    $rejected = @($pages | Where-Object { $_.Reason -eq "path_excluded" -and $_.Updated } | Sort-Object Updated)
    $underFound = @($pages | Where-Object { $_.Under -and $_.Created } | Sort-Object Created)
    if ($rejected.Count) {
        $first = $rejected[0].Updated
        $after = @($underFound | Where-Object { $_.Created -gt $first }).Count
        Write-Host ("First page rejected by the path rules: {0:MM-dd HH:mm} UTC" -f $first.ToUniversalTime())
        Write-Host ("{0} page(s) under {1} were discovered AFTER that moment." -f $after, $Path)
        if ($after -gt 0) { Write-Host "  -> Links were still being queued after the rules took effect: that points to a gap in link filtering or to instances running older rules." -ForegroundColor Yellow }
        else { Write-Host "  -> Discovery stopped when the rules took effect; the backlog queued earlier was rejected as it came up." -ForegroundColor Green }
    } else {
        Write-Host "No page was rejected by the path rules in this job."
    }
    if ($underFound.Count) { Write-Host ("{0} found {1:MM-dd HH:mm} to {2:MM-dd HH:mm} UTC: {3} pages." -f $Path, $underFound[0].Created.ToUniversalTime(), $underFound[-1].Created.ToUniversalTime(), $underFound.Count) }
}

function Start-Crawl([string]$Domain, [switch]$Wait) {
    if (-not $Domain) { Write-Warning "Give the full domain, e.g. Start-Crawl www.themuse.com"; return }
    $r = Call POST "/api/crawl" @{ domains = @($Domain) }
    # The app does not start a second job for a domain that already has an open one; it says so in "skipped".
    foreach ($s in @($r.skipped)) { if ($s) { Write-Warning "Skipped $($s.domain): $($s.reason)" } }
    if (@($r.skipped | Where-Object { $_.reason -like "*active crawl*" }).Count) {
        $open = @(Get-OpenJobs $Domain)
        if ($open.Count) {
            Write-Host "Open job(s) blocking it:"
            $open | Format-Table job_id, status, created_utc, updated_utc, pages_discovered, pages_processed, pending -AutoSize | Out-Host
        }
        Write-Host "Follow the running crawl with: Wait-Crawl $Domain   (or cancel it first with: Stop-Crawl $Domain)"
    }
    if (-not $r.jobs) { Write-Warning "No job was created. Response: $($r | ConvertTo-Json -Depth 3 -Compress)"; return }
    $r.jobs | ForEach-Object {
        [pscustomobject]@{ Domain = $_.domain; Status = $_.status; PageLimit = $_.page_limit; MaxDepth = $_.max_depth; JobId = $_.job_id }
    } | Format-Table -AutoSize -Wrap | Out-Host
    Write-Host "Check PageLimit: it should be MAX_PAGES_PER_DOMAIN. A job keeps the limit it started with, so after changing that setting, start a new crawl."
    if ($Wait) { Start-Sleep -Seconds 3; Wait-Crawl $Domain }
}

function Resume-Crawl([string]$Domain) {
    $job = Find-LatestJob $Domain
    if (-not $job) { return }
    Write-Host "Resuming failed tasks for $($job.Domain), job $($job.JobId) ..."
    Call POST "/api/crawl/$($job.JobId)/resume" | ConvertTo-Json -Depth 3
}

function Recover-Crawl([string]$Domain, [int]$StaleMinutes = 5) {
    $job = Find-LatestJob $Domain
    if (-not $job) { return }
    Write-Host "Putting stuck pages back on the queue for $($job.Domain), job $($job.JobId) ..."
    $r = Call POST "/api/crawl/$($job.JobId)/recover" @{ stale_minutes = $StaleMinutes }
    if ($null -ne $r.requeued) { Write-Host "Requeued $($r.requeued) page(s)." }
    if ($r.publish_errors) { Write-Warning "Some pages could not be queued:"; $r.publish_errors | Format-Table -AutoSize | Out-Host }
    if ($null -eq $r.requeued) { $r | ConvertTo-Json -Depth 3 }
}

Write-Host "Loaded. Try: Show-Crawl | Wait-Crawl muse | Show-CrawlPages muse | Start-Crawl <domain> -Wait | Resume-Crawl <domain>"