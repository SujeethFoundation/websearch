<#
.SYNOPSIS
  Adds, replaces or removes one page in websearch by URL. The server crawls the page and infers its title and description.

.DESCRIPTION
  Give it a URL. The websearch app crawls the page the way its crawler does (crawler user agent,
  robots.txt respected) and works out the title and description from what it finds. A description
  you pass is never used. Running it again with the same URL replaces the page's chunks.

  If the site blocks the crawler, the server says so and nothing is stored. Then copy the page text
  in your browser and add -FromClipboard (or -TextFile). A title is required in that case, and the
  description is inferred from the text. -Preview crawls and shows what would be stored, without
  writing. -Delete removes a page.

  The URL's domain must be search-enabled in the ontology. Domains marked "manual_only" are not
  crawled and their pages surface as best bets. For a crawled domain, add -AllowCrawledDomain; a
  later crawl may then replace the page.

.EXAMPLE
  .\Add-ManualPage.ps1 -Url https://example.org/page -Preview
.EXAMPLE
  .\Add-ManualPage.ps1 -Url https://example.org/page
.EXAMPLE
  .\Add-ManualPage.ps1 -Url https://app.candid.org/profile/15027428/the-sujeeth-foundation-93-1506908 `
      -FromClipboard -Title "The Sujeeth Foundation | EIN 93-1506908 | Candid"
.EXAMPLE
  .\Add-ManualPage.ps1 -Url https://example.org/page -Delete
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Url,
  [string]$Title,
  [string]$TextFile,
  [switch]$FromClipboard,
  [switch]$Preview,
  [string[]]$Concepts,
  [switch]$AllowCrawledDomain,
  [switch]$Delete,
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
  Send-Request "/api/index/page/delete" @{ url = $Url } | ConvertTo-Json -Depth 4
  return
}

$body = @{ url = $Url }
if ($Concepts) { $body.concepts = @($Concepts) }
if ($AllowCrawledDomain) { $body.allow_crawled_domain = $true }
if ($Title) { $body.title = $Title }
if ($Preview) { $body.preview = $true }

# Default: the server crawls the page. Text is sent only when you ask for it.
if ($TextFile -or $FromClipboard) {
  if ($TextFile) { $text = Get-Content -LiteralPath $TextFile -Raw -Encoding UTF8 } else { $text = Get-Clipboard -Raw }
  $text = ($text -replace '\s+', ' ').Trim()
  if ([string]::IsNullOrWhiteSpace($text)) { throw "No text found. Copy the page text first, or pass -TextFile." }
  Write-Host ("Page text: {0:N0} characters" -f $text.Length)
  if ($text.Length -lt 500) { Write-Warning "That is very short. Check that you copied the whole page and not an error or verification page." }
  if ($text -match 'verify you are human|access denied|captcha') { Write-Warning "The text looks like a bot-check page. Copy the page itself." }
  if ([string]::IsNullOrWhiteSpace($Title)) { $Title = Read-Host "Page title" }
  if ([string]::IsNullOrWhiteSpace($Title)) { throw "A title is required when you send text." }
  $body.title = $Title
  $body.content = $text
}

$result = Send-Request "/api/index/page" $body
$result | ConvertTo-Json -Depth 4
if ($result.status -eq "stored_pending_index") {
  Write-Warning "Stored, but the search index was busy. Run POST /api/index/backfill with {`"mode`":`"missing`"}."
}
