[CmdletBinding()]
param(
	[Parameter(Mandatory = $true, Position = 0)]
	[ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
	[string] $InputFile,

	[Parameter(Position = 1)]
	[string] $OutputFile
)

if ([string]::IsNullOrWhiteSpace($OutputFile)) {
	$inputItem = Get-Item -LiteralPath $InputFile
	$OutputFile = Join-Path $inputItem.DirectoryName "$($inputItem.BaseName)-6.txt"
}

if ([StringComparer]::OrdinalIgnoreCase.Equals((Resolve-Path -LiteralPath $InputFile).Path, (Resolve-Path -LiteralPath $OutputFile -ErrorAction SilentlyContinue).Path)) {
	throw "The output file must be different from the input file."
}

Get-Content -LiteralPath $InputFile |
	Where-Object { $_.Length -eq 6 } |
	Set-Content -LiteralPath $OutputFile

Write-Host "Wrote six-character lines to $OutputFile"
