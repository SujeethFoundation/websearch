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
#     Show-CrawlHours            which crawl jobs were running in each UTC hour, to match Azure AI Search query spikes (-Hours 48, -SpikeHours 4,11, -Domain muse)
#     Show-CrawlTags             chunks per ontology tag, plus untagged chunks and chunks per domain
#     Show-CrawlTags muse        the same for one domain
#     Show-CrawlTags -Tag salary_negotiation -Domain muse   the pages that carry one tag
#     Show-CrawlTags -Url https://www.themuse.com/advice/some-page   the tags on each chunk of one page
#     Start-Crawl www.themuse.com   start a crawl and show the page limit it was given
#     Start-Crawl www.themuse.com -Wait   ...then follow it to the end with a progress bar
#     Wait-Crawl muse            follow the latest crawl of a domain: progress bar, pages/min, ETA, stall warning
#     Resume-Crawl muse          re-run the failed tasks of the domain's latest job
#     Recover-Crawl muse         put the latest job's stuck pages back on the queue (stale for 5+ minutes; -StaleMinutes 25 to change)
#     Get-CrawlJobId muse        the full job id of the domain's latest job
#     Stop-Crawl muse            cancel every open job of the domain (asks first; -Force skips the question)
#     Call GET "/api/health"     any raw call
#
# Local search (the index kept in Table Storage; needs the local search patch on the app):
#     Show-LocalIndex            settings in force and what one instance has loaded (-Count adds the table rows per domain and the queue backlog; -CountBlobs adds the stored chunks)
#     Start-LocalBackfill        copy every stored chunk into the table; asks first (Start-LocalBackfill muse for one domain; -Follow watches it fill)
#     Watch-LocalBackfill        follow a backfill: rows, rows added, rows per minute, queue backlog (-CountBlobs for a progress bar and ETA)
#     Watch-Websearch            health watcher: per instance uptime and restarts, index loaded and size, memory, searches and timings, shadow comparison, cache; a summary with checks on Ctrl+C
#     Watch-Websearch -Every 5 -Polls 120 -InstanceMb 2048 -MinCompared 300 -MinOverlap 0.6 -MinTop 0.5     (the bars for the checks are yours to choose)

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

function ConvertTo-UtcTime($value) {
    # Windows PowerShell returns timestamps as text, PowerShell 7 as DateTime. A time with no offset counts as UTC.
    if ($null -eq $value -or "$value" -eq "") { return $null }
    if ($value -is [datetime]) { return $value.ToUniversalTime() }
    try { [DateTimeOffset]::Parse("$value", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime }
    catch { $null }
}

function Show-CrawlHours([int]$Hours = 24, [int[]]$SpikeHours = @(4, 11), [string]$Domain = "") {
    # Which crawl jobs were running in each UTC hour, to compare with Azure AI Search query spikes.
    # A job counts as running from created_utc to updated_utc (its last write), so one that sat idle in the middle still shows. Reads the latest 100 jobs.
    $r = Call GET "/api/crawl-status"
    if (-not $r -or -not $r.jobs) { Write-Warning "No jobs came back. Raw response: $($r | ConvertTo-Json -Depth 3 -Compress)"; return }
    $now   = (Get-Date).ToUniversalTime()
    $since = $now.AddHours(-$Hours)
    $jobs = @(foreach ($j in $r.jobs) {
        if ($Domain -and $j.domain -notlike "*$Domain*") { continue }
        $created = ConvertTo-UtcTime $j.created_utc
        $updated = ConvertTo-UtcTime $j.updated_utc
        if (-not $created) { continue }
        if (-not $updated) { $updated = $created }
        [pscustomobject]@{
            Domain = $j.domain; Status = (Short-Status $j.status); Created = $created; Updated = $updated
            Done = $j.pages_processed; Chunks = $j.chunks_indexed; Pend = $j.pending; Job = (Get-JobId $j).Substring(0, 8)
        }
    })
    $recent = @($jobs | Where-Object { $_.Updated -ge $since } | Sort-Object Created)
    if (-not $recent.Count) { Write-Host "No crawl job was active in the last $Hours hours."; return }
    Write-Host "Crawl jobs active in the last $Hours hours (UTC; now $($now.ToString('MM-dd HH:mm')))"
    $recent | Format-Table Domain, Status,
        @{ n = "Created"; e = { $_.Created.ToString("MM-dd HH:mm") } },
        @{ n = "Updated"; e = { $_.Updated.ToString("MM-dd HH:mm") } },
        Done, Chunks, Pend, Job -AutoSize | Out-Host
    $first = [datetime]::new($since.Year, $since.Month, $since.Day, $since.Hour, 0, 0, [DateTimeKind]::Utc)
    $table = for ($h = $first; $h -lt $now; $h = $h.AddHours(1)) {
        $end    = $h.AddHours(1)
        $active = @($jobs | Where-Object { $_.Created -lt $end -and $_.Updated -ge $h })
        [pscustomobject]@{
            "UTC hour" = $h.ToString("MM-dd HH:00")
            Jobs       = $active.Count
            Domains    = (@($active | ForEach-Object { $_.Domain } | Sort-Object -Unique) -join ", ")
            Note       = if ($SpikeHours -contains $h.Hour) { "<-- spike hour" } else { "" }
        }
    }
    Write-Host "Active crawl jobs by UTC hour:"
    $table | Format-Table -AutoSize -Wrap | Out-Host
    Write-Host "No job in a spike hour means the crawl did not cause it. Look at the Application Insights 'Queue disposition' traces for retag or backfill tasks instead."
}

function Show-CrawlTags([string]$Domain, [string]$Tag, [string]$Url, [int]$Top = 40, [int]$Pages = 25, [int]$Scan = 3000) {
    # What the ontology tagged. Needs the /api/index/tags endpoint (websearch app version with Show-CrawlTags support).
    #   Show-CrawlTags [domain]            chunks per tag, untagged chunks, and chunks per domain (-Top limits the tag rows)
    #   Show-CrawlTags -Tag <tag> [domain] the pages that carry that tag, most tagged chunks first (-Pages limits the rows)
    #   Show-CrawlTags -Url <page url>     the tags on each chunk of one page
    # Reads the search index: one count query for the counts (plus one for a domain), one query for each of the other views.
    # sf-knowledge-v2 cannot count by the concepts field (it is not facetable), so the counts are estimated from a sample of
    # the stored chunks, scaled to the index size, with a margin of error shown. -Scan sets the sample (3000, at most 5000;
    # a domain always uses 5000).
    $full = ""
    if ($Domain) {
        $found = @(Get-DashboardRows $Domain)
        if ($found.Count -eq 1) { $full = $found[0].Domain }
        elseif ($found.Count -gt 1) { Write-Warning "'$Domain' matches $($found.Count) domains ($($found.Domain -join ', ')). Be more specific."; return }
        elseif ($Domain -like "*.*") { $full = $Domain.ToLower() }
        else { Write-Warning "No domain matches '$Domain'. Use the full domain, e.g. www.themuse.com"; return }
    }
    $enc = { param($v) [uri]::EscapeDataString($v) }
    if ($Url) {
        $r = Call GET "/api/index/tags?url=$(& $enc $Url)"
        if (-not $r -or $r -is [string] -or $null -eq $r.chunks) { Write-Warning "No usable answer for that page. Response: $r"; return }
        if (-not @($r.chunks).Count) { Write-Host "No chunks of $Url are in the index."; return }
        Write-Host "$($r.title)  ($($r.domain))"
        Write-Host $r.url
        @($r.chunks) | ForEach-Object {
            [pscustomobject]@{ Chunk = $_.chunk; Tags = $(if (@($_.tags).Count) { @($_.tags) -join ", " } else { "(untagged)" }) }
        } | Format-Table -AutoSize -Wrap | Out-Host
        $all = @(@($r.chunks) | ForEach-Object { @($_.tags) } | Where-Object { $_ })
        if ($all.Count) {
            Write-Host "Tags on this page:"
            $all | Group-Object | Sort-Object Count -Descending |
                ForEach-Object { [pscustomobject]@{ Tag = $_.Name; Chunks = $_.Count } } | Format-Table -AutoSize | Out-Host
        }
        return
    }
    if ($Tag) {
        $q = "/api/index/tags?tag=$(& $enc $Tag)&limit=$Pages"
        if ($full) { $q += "&domain=$(& $enc $full)" }
        $r = Call GET $q
        if (-not $r -or $r -is [string] -or $null -eq $r.pages) { Write-Warning "No usable answer. Response: $r"; return }
        $scope = if ($full) { " in $full" } else { "" }
        Write-Host ("Tag '{0}'{1}: {2} chunks match, on {3} pages (read {4} chunks)." -f $r.tag, $scope, $r.chunks_matched, $r.pages_found, $r.chunks_read)
        if ([int]$r.chunks_matched -gt [int]$r.chunks_read) { Write-Warning "More chunks match than were read ($($r.chunks_read)); the page list is partial. Add a domain to narrow it." }
        @($r.pages) | ForEach-Object {
            [pscustomobject]@{ Chunks = $_.chunks; Url = $_.url; Title = $(if ($_.title) { "$($_.title)".Substring(0, [Math]::Min(50, "$($_.title)".Length)) } else { "" }) }
        } | Format-Table -AutoSize -Wrap | Out-Host
        if ([int]$r.pages_found -gt @($r.pages).Count) { Write-Host "... and $($r.pages_found - @($r.pages).Count) more pages. Use -Pages to show more." }
        return
    }
    $q = "/api/index/tags?scan=$Scan"
    if ($full) { $q += "&domain=$(& $enc $full)" }
    $r = Call GET $q
    if (-not $r -or $r -is [string] -or $null -eq $r.tags) { Write-Warning "No usable answer. Is the websearch app deployed with /api/index/tags? Response: $r"; return }
    $total = [int]$r.total_chunks
    $scope = if ($full) { $full } else { "all domains" }
    $sampled = ($r.source -eq "blob_sample")
    $label = if ($sampled) { "Est. chunks" } else { "Chunks" }
    Write-Host ("Tags on {0}: {1}{2:N0} chunks, {3}{4:N0} untagged ({5:N1}%)" -f $scope, $(if ($sampled) { "about " } else { "" }), $total, $(if ($sampled) { "about " } else { "" }), [int]$r.untagged_chunks, $(if ($total) { 100 * [int]$r.untagged_chunks / $total } else { 0 }))
    if ($sampled) {
        Write-Host ("Estimated from a sample of {0} stored chunks ({1} in scope), scaled to the index. Shares are good to about +/-{2} points; chunk counts are estimates." -f $r.scanned, $r.sample_chunks, $r.margin_points) -ForegroundColor Yellow
    }
    $tagRows = @($r.tags | Sort-Object { [int]$_.chunks } -Descending)
    $tagRows | Select-Object -First $Top | ForEach-Object {
        $row = [ordered]@{ Tag = $_.tag }
        $row[$label] = $_.chunks
        $row["Share"] = Format-Bar $_.chunks $total 12
        [pscustomobject]$row
    } | Format-Table -AutoSize | Out-Host
    if ($tagRows.Count -gt $Top) { Write-Host "... and $($tagRows.Count - $Top) more tags. Use -Top to show more." }
    Write-Host "A chunk can carry several tags, so the tag counts add up to more than the chunk total."
    if (-not $full -and @($r.domains).Count) {
        Write-Host "Chunks per domain:"
        @($r.domains) | ForEach-Object {
            $row = [ordered]@{ Domain = $_.domain }
            $row[$label] = $_.chunks
            [pscustomobject]$row
        } | Format-Table -AutoSize | Out-Host
        Write-Host "Drill in with: Show-CrawlTags <domain>   or   Show-CrawlTags -Tag <tag> <domain>"
    }
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

# ---------------------------------------------------------------------------------------------------------------
# Local search (the index kept in Table Storage): backfill, and a health watcher.
# ---------------------------------------------------------------------------------------------------------------

function Get-Num($v) { if ($null -eq $v -or "$v" -eq "") { 0.0 } else { [double]$v } }

function Format-Cell($v, [string]$fmt = "N0") {
    if ($null -eq $v -or "$v" -eq "") { return "-" }
    try { "{0:$fmt}" -f [double]$v } catch { "$v" }
}

function Short-Instance($name) {
    $s = "$name"
    if ($s.Length -gt 12) { $s.Substring(0, 6) + ".." + $s.Substring($s.Length - 4) } else { $s }
}

function Show-LocalIndex([switch]$Count, [switch]$CountBlobs) {
    # The local index at a glance. Each call reaches one instance, so the "loaded" figures belong to that instance.
    #   Show-LocalIndex                 settings in force and what this instance has loaded
    #   Show-LocalIndex -Count          also what the table holds now (rows in all and per domain) and the queue backlog
    #   Show-LocalIndex -Count -CountBlobs   also the stored chunks in Blob Storage, to see how much is copied (slow: lists every blob)
    $q = "/api/local-index/status" + $(if ($Count) { "?count=true" } else { "" })
    $r = Call GET $q
    if (-not $r -or $r -is [string]) { Write-Warning "No usable answer from $q. Is the websearch app deployed with the local search patch? Response: $r"; return }
    $w = $r.writes
    Write-Host ("Backend: {0}   LOCAL_INDEX_WRITE: {1}   AI_SEARCH_WRITES: {2}" -f $r.backend, $w.local_index_write, $w.ai_search_writes)
    if ($r.loaded) {
        $readSeconds = (Get-Num $r.load_seconds) - (Get-Num $r.build_seconds)
        Write-Host ("This instance: {0:N0} chunks on {1:N0} pages, {2} MB index, process {3} MB, loaded in {4}s (read {5}s, build {6}s), {7}s ago, {8} load(s)" -f `
            $r.chunks, $r.pages, $r.memory_mb, $r.process_memory_mb, $r.load_seconds, (Format-Cell $readSeconds "N1"), $r.build_seconds, $r.age_seconds, $r.loads)
    } else {
        Write-Host "This instance has not loaded the index yet." -ForegroundColor Yellow
    }
    if ($r.last_error) { Write-Host "Last error: $($r.last_error)" -ForegroundColor Red }
    $rows = $null
    if ($Count) {
        if ($r.table_error) { Write-Warning "The table could not be counted: $($r.table_error)" }
        elseif ($null -ne $r.table_rows) {
            $rows = [int]$r.table_rows
            Write-Host ("`nThe table holds {0:N0} chunks. Queue backlog: {1}" -f $rows, $(if ($null -ne $r.queue_messages) { "{0:N0} messages" -f [int]$r.queue_messages } else { "unknown" }))
            @($r.table_by_domain) | ForEach-Object { [pscustomobject]@{ Domain = $_.domain; Chunks = $_.chunks; Share = (Format-Bar $_.chunks $rows 12) } } | Format-Table -AutoSize | Out-Host
        }
    }
    if ($CountBlobs) {
        Write-Host "Counting stored chunks in Blob Storage (this lists every blob and can take minutes) ..."
        $b = Call GET "/api/index-status?count_blobs=true"
        $stored = if ($b -and $b.blob_storage) { $b.blob_storage.document_count } else { $null }
        if ($null -ne $stored) {
            Write-Host ("Blob Storage holds {0:N0} chunks." -f [int]$stored)
            if ($null -ne $rows -and [int]$stored -gt 0) { Write-Host ("Copied to the table: {0}" -f (Format-Bar $rows $stored 20)) }
            return [pscustomobject]@{ TableRows = $rows; StoredChunks = [int]$stored }
        }
    }
    if ($null -ne $rows) { [pscustomobject]@{ TableRows = $rows; StoredChunks = $null } }
}

function Watch-LocalBackfill([int]$Every = 20, [int]$StableRounds = 3, [switch]$CountBlobs, [int]$Polls = 0) {
    # Follows a local backfill: rows in the table, rows added since the last look, rows per minute, and the queue backlog.
    # With -CountBlobs it first counts the stored chunks (slow, once) and then shows a progress bar and an ETA.
    # It stops by itself after $StableRounds looks with no new rows and an empty queue. Ctrl+C stops watching; the backfill keeps running.
    $target = $null
    if ($CountBlobs) {
        Write-Host "Counting stored chunks in Blob Storage first (once; this lists every blob) ..."
        $b = Call GET "/api/index-status?count_blobs=true"
        if ($b -and $b.blob_storage -and $null -ne $b.blob_storage.document_count) { $target = [int]$b.blob_storage.document_count; Write-Host ("{0:N0} stored chunks to copy." -f $target) }
        else { Write-Warning "Could not count the stored chunks; following without a target." }
    }
    $began = Get-Date; $prev = $null; $firstRows = $null; $stable = 0; $stuck = 0; $n = 0
    Write-Host "Following the backfill every $Every s. Ctrl+C stops watching; the backfill keeps running."
    Write-Host ("{0,-8} {1,9} {2,8} {3,9} {4,8}  {5}" -f "Time(UTC)", "Rows", "Added", "Rows/min", "Queue", "Progress")
    while ($true) {
        $r = Call GET "/api/local-index/status?count=true"
        $n++
        if (-not $r -or $r -is [string] -or $null -eq $r.table_rows) {
            Write-Warning ("No row count this time. " + $(if ($r.table_error) { $r.table_error } else { "Response: $r" }))
        } else {
            $rows = [int]$r.table_rows
            if ($null -eq $firstRows) { $firstRows = $rows }
            $added = if ($null -ne $prev) { $rows - $prev } else { 0 }
            $minutes = [Math]::Max(0.1, ((Get-Date) - $began).TotalMinutes)
            $rate = if ($n -gt 1) { ($rows - $firstRows) / $minutes } else { 0 }
            $queue = $r.queue_messages
            $bar = ""
            if ($target) {
                $bar = Format-Bar $rows $target 20
                $left = $target - $rows
                if ($rate -gt 1 -and $left -gt 0) { $bar += ("  ETA {0:N0} min" -f ($left / $rate)) }
            }
            Write-Host ("{0,-8} {1,9:N0} {2,8:N0} {3,9:N0} {4,8}  {5}" -f (Get-Date).ToUniversalTime().ToString("HH:mm:ss"), $rows, $added, $rate, $(if ($null -ne $queue) { "{0:N0}" -f [int]$queue } else { "?" }), $bar)
            $queueEmpty = ($null -eq $queue) -or ([int]$queue -eq 0)
            if ($null -ne $prev -and $rows -eq $prev) {
                if ($queueEmpty) { $stable++ } else { $stuck++ }
            } else { $stable = 0; $stuck = 0 }
            if ($stuck -ge 5) { Write-Warning "The queue is not empty but no rows were added for $stuck looks. Check the app's logs for index_blob_batch errors." ; $stuck = 0 }
            if ($stable -ge $StableRounds) {
                Write-Host ("Done: no new rows and an empty queue for {0} looks. The table holds {1:N0} chunks." -f $StableRounds, $rows) -ForegroundColor Green
                if ($target -and $rows -lt $target) { Write-Warning ("The table holds {0:N0} fewer chunks than Blob Storage. Chunks without text are skipped, so a small gap is normal; run Start-LocalBackfill again to be sure." -f ($target - $rows)) }
                return
            }
            $prev = $rows
        }
        if ($Polls -gt 0 -and $n -ge $Polls) { return }
        Start-Sleep -Seconds $Every
    }
}

function Start-LocalBackfill([string]$Domain, [switch]$Follow, [switch]$CountBlobs, [switch]$Force, [int]$Every = 20) {
    # Queues a copy of every stored chunk into the local index table (POST /api/index/backfill with mode local).
    # It runs on the crawl queue, resumes by itself, reuses stored vectors, does not touch Azure AI Search, and embeds only
    # chunks that have no stored vector. Safe to run again. Needs LOCAL_INDEX_WRITE=true on the app.
    #   Start-LocalBackfill                 everything
    #   Start-LocalBackfill muse            only domains matching "muse"
    #   Start-LocalBackfill -Follow         ...and watch the table fill (add -CountBlobs for a progress bar and ETA)
    $s = Call GET "/api/local-index/status"
    if (-not $s -or $s -is [string]) { Write-Warning "Could not read /api/local-index/status. Is the websearch app deployed with the local search patch? Response: $s"; return }
    Write-Host ("Backend: {0}   LOCAL_INDEX_WRITE: {1}   AI_SEARCH_WRITES: {2}" -f $s.backend, $s.writes.local_index_write, $s.writes.ai_search_writes)
    if (-not $s.enabled) { Write-Warning "The app has no table connection. Set LOCAL_INDEX_CONNECTION_STRING (or BLOB_STORAGE_CONNECTION_STRING) first."; return }
    if (-not $s.writes.local_index_write) {
        Write-Warning "LOCAL_INDEX_WRITE is not true on the app. Set it first, so chunks crawled during the backfill reach the table too, then run this again."
        return
    }
    $body = @{ mode = "local" }
    $scope = "every stored chunk"
    if ($Domain) {
        $found = @(Get-DashboardRows $Domain)
        if ($found.Count -eq 1) { $full = $found[0].Domain }
        elseif ($found.Count -gt 1) { Write-Warning "'$Domain' matches $($found.Count) domains ($($found.Domain -join ', ')). Be more specific."; return }
        elseif ($Domain -like "*.*") { $full = $Domain.ToLower() }
        else { Write-Warning "No domain matches '$Domain'. Use the full domain, e.g. www.themuse.com"; return }
        $body.domains = @($full)
        $scope = "chunks of $full"
    }
    if (-not $Force) {
        $answer = Read-Host "Copy $scope into the local index table? Chunks with no stored vector are embedded, which uses embedding quota. (y/N)"
        if ($answer -notmatch '^(y|yes)$') { Write-Host "Cancelled."; return }
    }
    $r = Call POST "/api/index/backfill" $body
    if (-not $r -or $r -is [string]) { Write-Warning "The backfill was not queued. Response: $r"; return }
    Write-Host ("{0}: {1}" -f $r.status, $r.message) -ForegroundColor Green
    if ($Follow) { Watch-LocalBackfill -Every $Every -CountBlobs:$CountBlobs }
    else { Write-Host "Follow it with: Watch-LocalBackfill   (add -CountBlobs for a progress bar)" }
}

function Show-WatchSummary($seen, [int]$InstanceMb, [int]$MinCompared, [double]$MinOverlap, [double]$MinTop, $backend, [int]$restarts, $restartLog) {
    $list = @($seen.Values | ForEach-Object { [pscustomobject]$_ } | Sort-Object First)
    if (-not $list.Count) { Write-Host "`nNo instance answered."; return }
    Write-Host "`n=== Summary ==="
    # One line per instance, so nothing is hidden on a narrow console.
    foreach ($i in $list) {
        $err = if ($i.Err) { "  ERROR: " + $i.Err.Substring(0, [Math]::Min(60, $i.Err.Length)) } else { "" }
        $line = ("{0,-13} polls {1,3}  restarts {2}  up {3:N0}s  loaded {4}  chunks {5:N0}  process {6:N0} MB (highest)  loads {7} ({8}s)  searches {9:N0}  fallbacks {10}  shadow {11:N0}{12}" -f `
            (Short-Instance $i.Name), $i.Polls, $i.Restarts, $i.Up, $(if ($i.Loaded) { "yes" } else { "NO" }), $i.Chunks, $i.MaxProc, $i.Loads, (Format-Cell $i.LoadSecs "N1"), $i.Searches, $i.Fallbacks, $i.ShadowN, $err)
        if ($i.Restarts -or $i.Err -or -not $i.Loaded) { Write-Host $line -ForegroundColor Red } else { Write-Host $line }
    }
    # Counters are per process and only grow, so the last value seen from each instance is that instance's total.
    $sum = { param($name) [double](($list | ForEach-Object { Get-Num $_.$name } | Measure-Object -Sum).Sum) }
    $shadowN = & $sum "ShadowN"
    $searches = & $sum "Searches"
    if ($searches -gt 0) {
        $emb = ($list | ForEach-Object { (Get-Num $_.EmbMs) * (Get-Num $_.Searches) } | Measure-Object -Sum).Sum / $searches
        $idx = ($list | ForEach-Object { (Get-Num $_.IdxMs) * (Get-Num $_.Searches) } | Measure-Object -Sum).Sum / $searches
        Write-Host ("Local searches: {0:N0}   average embedding call {1:N0} ms   average search in memory {2:N2} ms" -f $searches, $emb, $idx)
    }
    $overlap = $null; $top = $null
    if ($shadowN -gt 0) {
        $overlap = ($list | ForEach-Object { (Get-Num $_.Overlap) * (Get-Num $_.ShadowN) } | Measure-Object -Sum).Sum / $shadowN
        $top = ($list | ForEach-Object { (Get-Num $_.Top1) * (Get-Num $_.ShadowN) } | Measure-Object -Sum).Sum / $shadowN
        Write-Host ("Shadow comparison against Azure AI Search: {0:N0} searches   average overlap of the top results {1:N3}   top result agrees {2:N3}" -f $shadowN, $overlap, $top)
        $failed = & $sum "ShadowFailed"
        if ($failed -gt 0) { Write-Host ("  {0:N0} comparison(s) failed to run locally." -f $failed) -ForegroundColor Yellow }
    }
    $azReq = & $sum "AzReq"; $azTot = & $sum "AzTotal"
    if ($azReq -gt 0) { Write-Host ("Azure AI Search queries answered by these instances: {0:N0}   average {1:N2} Compute Units each ({2:N0} in all)" -f $azReq, ($azTot / $azReq), $azTot) }
    $hits = & $sum "QHits"; $miss = & $sum "QMiss"
    if (($hits + $miss) -gt 0) { Write-Host ("Result cache: {0:N0} hits, {1:N0} misses ({2:P0} hit rate)" -f $hits, $miss, ($hits / ($hits + $miss))) }
    Write-Host "`nChecks (each poll reaches one instance, so watch long enough to see them all; $($list.Count) seen):"
    $mark = { param($ok, $text) if ($null -eq $ok) { Write-Host "  [ - ] $text" } elseif ($ok) { Write-Host "  [ OK ] $text" -ForegroundColor Green } else { Write-Host "  [FAIL] $text" -ForegroundColor Red } }
    & $mark (-not ($list | Where-Object { -not $_.Loaded })) "every instance seen has the index loaded"
    & $mark (-not ($list | Where-Object { $_.Err })) "no instance reports a load or refresh error"
    & $mark ($restarts -eq 0) $(if ($restarts) { "no restarts while watching: $restarts seen (" + (($restartLog | Select-Object -Last 3) -join "; ") + ")" } else { "no restarts while watching" })
    $maxProc = [double](($list | ForEach-Object { Get-Num $_.MaxProc } | Measure-Object -Maximum).Maximum)
    if ($maxProc -gt 0 -and $InstanceMb -gt 0) { & $mark (($maxProc / $InstanceMb) -lt 0.75) ("highest process memory {0:N0} MB of {1:N0} MB ({2:P0}); under 75% leaves room for a rebuild" -f $maxProc, $InstanceMb, ($maxProc / $InstanceMb)) }
    if ($backend -eq "shadow") {
        & $mark ($shadowN -ge $MinCompared) ("at least {0:N0} shadow comparisons (have {1:N0})" -f $MinCompared, $shadowN)
        if ($MinOverlap -gt 0 -and $null -ne $overlap) { & $mark ($overlap -ge $MinOverlap) ("average overlap at least {0:N2} (is {1:N3})" -f $MinOverlap, $overlap) }
        if ($MinTop -gt 0 -and $null -ne $top) { & $mark ($top -ge $MinTop) ("top result agrees at least {0:N2} (is {1:N3})" -f $MinTop, $top) }
        if (-not $MinOverlap -and $null -ne $overlap) { Write-Host "  [ - ] overlap and top-result agreement are shown above; add -MinOverlap and -MinTop to check them against your own bar" }
    }
    if ($backend -eq "local") { & $mark ((& $sum "Fallbacks") -eq 0) "no searches fell back to Azure AI Search" }
}

function Watch-Websearch([int]$Every = 10, [int]$Polls = 0, [int]$InstanceMb = 2048, [int]$MinCompared = 200, [double]$MinOverlap = 0, [double]$MinTop = 0) {
    # Polls /api/health and shows, for every instance that answers: uptime and process id (a restart shows as a new id or uptime dropping),
    # whether the local index is loaded and how big, the process memory, searches and the average embedding and in-memory search time,
    # fallbacks to Azure AI Search, the shadow comparison with Azure AI Search, and the result cache hit rate.
    # Each poll reaches one instance. Ctrl+C (or -Polls N) stops it and prints a summary with checks.
    #   Watch-Websearch                         every 10 seconds
    #   Watch-Websearch -Every 5 -Polls 120     ten minutes, then the summary
    #   Watch-Websearch -InstanceMb 2048 -MinCompared 300 -MinOverlap 0.6 -MinTop 0.5   the bars for the checks (you choose them)
    $seen = @{}; $n = 0; $attempts = 0; $rows = 0; $restarts = 0; $restartLog = @(); $backend = ""
    Write-Host "Watching $script:AzureUrl every $Every s. Ctrl+C stops and prints a summary."
    $header = ("{0,-8} {1,-13} {2,5} {3,7} {4,-6} {5,8} {6,6} {7,6} {8,6} {9,6} {10,4} {11,7} {12,7} {13,5} {14,6}  {15}" -f `
        "Time", "Instance", "Pid", "Up(s)", "Loaded", "Chunks", "ProcMB", "Srch", "EmbMs", "IdxMs", "Fall", "ShadowN", "Overlap", "Top1", "QCache", "Notes")
    try {
        while ($true) {
            $attempts++
            $h = Call GET "/api/health"
            if (-not $h -or $h -is [string] -or -not $h.process) {
                Write-Host ("{0,-8} poll failed or the app has no process section yet (deploy the health patch)" -f (Get-Date).ToUniversalTime().ToString("HH:mm:ss")) -ForegroundColor Yellow
            } else {
                $n++
                $p = $h.process; $l = $h.local_index; $sh = $l.shadow; $qc = $h.query_cache; $az = $h.ai_search_compute.query
                $backend = "$($h.search_backend)"
                $name = "$($p.instance)"; $notes = @()
                if (-not $seen.ContainsKey($name)) {
                    $seen[$name] = @{ Name = $name; First = $n; Polls = 0; Restarts = 0; Pid = $p.pid; Up = [int]$p.uptime_seconds; MaxProc = 0 }
                    $notes += "first seen"
                } else {
                    $rec = $seen[$name]
                    if ($p.pid -ne $rec.Pid -or [int]$p.uptime_seconds -lt $rec.Up) {
                        $rec.Restarts++; $restarts++
                        $why = if ($p.pid -ne $rec.Pid) { "process id changed" } else { "uptime went back down" }
                        $restartLog += "$(Short-Instance $name) $why at $((Get-Date).ToUniversalTime().ToString('HH:mm'))"
                        $notes += "RESTART ($why)"
                    }
                }
                $rec = $seen[$name]
                $rec.Polls++; $rec.Pid = $p.pid; $rec.Up = [int]$p.uptime_seconds
                $rec.Loaded = [bool]$l.loaded; $rec.Chunks = [int](Get-Num $l.chunks)
                if ((Get-Num $l.process_memory_mb) -gt $rec.MaxProc) { $rec.MaxProc = [int](Get-Num $l.process_memory_mb) }
                $rec.Loads = [int](Get-Num $l.loads); $rec.LoadSecs = $l.load_seconds
                $rec.Searches = [int](Get-Num $l.searches); $rec.EmbMs = $l.avg_embed_ms; $rec.IdxMs = $l.avg_search_ms
                $rec.Fallbacks = [int](Get-Num $l.fallbacks_to_ai_search)
                $rec.ShadowN = [int](Get-Num $sh.compared); $rec.ShadowFailed = [int](Get-Num $sh.failed)
                $rec.Overlap = $sh.average_overlap; $rec.Top1 = $sh.top_result_agrees
                $rec.Err = "$($l.last_error)"
                $rec.AzReq = [int](Get-Num $az.requests); $rec.AzTotal = Get-Num $az.total
                $rec.QHits = [int](Get-Num $qc.hits); $rec.QMiss = [int](Get-Num $qc.misses)
                if ($rec.Err) { $notes += "ERROR: " + $rec.Err.Substring(0, [Math]::Min(50, $rec.Err.Length)) }
                if ($rows % 20 -eq 0) { Write-Host $header }
                $rows++
                $qrate = if ($null -ne $qc.hit_rate) { "{0:P0}" -f [double]$qc.hit_rate } else { "-" }
                $line = ("{0,-8} {1,-13} {2,5} {3,7} {4,-6} {5,8} {6,6} {7,6} {8,6} {9,6} {10,4} {11,7} {12,7} {13,5} {14,6}  {15}" -f `
                    (Get-Date).ToUniversalTime().ToString("HH:mm:ss"), (Short-Instance $name), $p.pid, (Format-Cell $p.uptime_seconds), $(if ($l.loaded) { "yes" } else { "no" }),
                    (Format-Cell $l.chunks), (Format-Cell $l.process_memory_mb), (Format-Cell $l.searches), (Format-Cell $l.avg_embed_ms), (Format-Cell $l.avg_search_ms "N2"),
                    (Format-Cell $l.fallbacks_to_ai_search), (Format-Cell $sh.compared), (Format-Cell $sh.average_overlap "N2"), (Format-Cell $sh.top_result_agrees "N2"), $qrate, ($notes -join "; "))
                if ($notes -match "RESTART|ERROR") { Write-Host $line -ForegroundColor Red } else { Write-Host $line }
            }
            if ($Polls -gt 0 -and $attempts -ge $Polls) { break }
            Start-Sleep -Seconds $Every
        }
    } finally {
        Show-WatchSummary $seen $InstanceMb $MinCompared $MinOverlap $MinTop $backend $restarts $restartLog
    }
}

Write-Host "Loaded. Try: Show-Crawl | Show-CrawlHours | Show-CrawlTags | Wait-Crawl muse | Show-CrawlPages muse | Start-Crawl <domain> -Wait | Resume-Crawl <domain> | Show-LocalIndex -Count | Start-LocalBackfill -Follow | Watch-Websearch"