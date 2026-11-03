<#
.SYNOPSIS
  Adds, replaces or removes one page in websearch by URL, using text you supply.

.DESCRIPTION
  For pages the crawler cannot fetch. Copy the page text in your browser (Ctrl+A, Ctrl+C), then run
  this script with the page's URL. It posts to POST /api/index/page. Running it again with the same
  URL replaces the page's chunks. Use -Delete to remove a page.

  The URL's domain must be search-enabled in the ontology. Domains marked "manual_only" are not
  crawled and their pages surface as best bets. For a crawled domain, add -AllowCrawledDomain; a
  later crawl may then replace the page.

.EXAMPLE
  .\Add-ManualPage.ps1 -Url "https://app.candid.org/profile/15027428/the-sujeeth-foundation-93-1506908" `
      -Title "The Sujeeth Foundation | EIN 93-1506908 | Candid"
.EXAMPLE
  .\Add-ManualPage.ps1 -Url https://example.org/page -TextFile .\page.txt -Concepts sujeeth_foundation
.EXAMPLE
  .\Add-ManualPage.ps1 -Url https://example.org/page -DryRun
.EXAMPLE
  .\Add-ManualPage.ps1 -Url https://example.org/page -Delete
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Url,
  [string]$Title,
  [string]$TextFile,
  [string[]]$Concepts,
  [switch]$AllowCrawledDomain,
  [switch]$Delete,
  [switch]$DryRun,
  [string]$BaseUrl = "https://websearch-h9arcweeftfrd8e0.eastus-01.azurewebsites.net",
  [string]$FunctionKey = $env:SUJEETH_WEBSEARCH_KEY
)
$ErrorActionPreference = "Stop"

if ($Url -notmatch '^https://[^/\s]+') { throw "-Url must be an absolute https URL." }

function Get-Key {
  if ($script:FunctionKey) { return $script:FunctionKey }
  $secure = Read-Host "Websearch function key" -AsSecureString
  return [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}

function Send-Request($path, $payload) {
  $json  = $payload | ConvertTo-Json -Depth 4
  $bytes = [Text.Encoding]::UTF8.GetBytes($json)
  try {
    return Invoke-RestMethod -Method Post -Uri "$BaseUrl$path" `
      -Headers @{ "x-functions-key" = (Get-Key) } `
      -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec 120
  } catch {
    $detail = $_.ErrorDetails.Message
    if (-not $detail -and $_.Exception.Response) {
      try { $detail = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd() } catch {}
    }
    throw "Request failed: $($_.Exception.Message)`n$detail"
  }
}

# Delete mode
if ($Delete) {
  if ($DryRun) { Write-Host "Dry run: would remove $Url"; return }
  Send-Request "/api/index/page/delete" @{ url = $Url } | ConvertTo-Json -Depth 4
  return
}

# Get the page text from a file or the clipboard.
if ($TextFile) { $text = Get-Content -LiteralPath $TextFile -Raw -Encoding UTF8 } else { $text = Get-Clipboard -Raw }
$text = ($text -replace '\s+', ' ').Trim()
if ([string]::IsNullOrWhiteSpace($text)) { throw "No text found. Copy the page text first, or pass -TextFile." }
Write-Host ("Page text: {0:N0} characters" -f $text.Length)
if ($text.Length -lt 500) { Write-Warning "That is very short. Check that you copied the whole page and not an error or verification page." }
if ($text -match 'verify you are human|access denied|captcha') { Write-Warning "The text looks like a bot-check page. Copy the page itself." }

# A title is required. Ask for one if it was not given.
if ([string]::IsNullOrWhiteSpace($Title)) { $Title = Read-Host "Page title" }
if ([string]::IsNullOrWhiteSpace($Title)) { throw "A title is required." }

if ($DryRun) {
  Write-Host "Dry run: nothing sent."
  Write-Host "URL:   $Url"
  Write-Host "Title: $Title"
  if ($Concepts) { Write-Host ("Concepts: " + ($Concepts -join ", ")) }
  Write-Host "First 200 characters:"; Write-Host $text.Substring(0, [Math]::Min(200, $text.Length))
  return
}

$body = @{ url = $Url; title = $Title; content = $text }
if ($Concepts) { $body.concepts = @($Concepts) }
if ($AllowCrawledDomain) { $body.allow_crawled_domain = $true }

$result = Send-Request "/api/index/page" $body
$result | ConvertTo-Json -Depth 4
if ($result.status -eq "stored_pending_index") {
  Write-Warning "Stored, but the search index was busy. Run POST /api/index/backfill with {`"mode`":`"missing`"}."
}
