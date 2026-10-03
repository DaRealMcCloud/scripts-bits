<#
.SYNOPSIS
	Generates numeric variants for every line in an input file.

.DESCRIPTION
	Reads each input line as-is and appends 0000 through 9999 to it. The
	generated values are written to standard output, one value per line.

.PARAMETER InputFile
	Input file to read. Defaults to input.txt beside this script.

.EXAMPLE
	.\stringGenerator.ps1 -InputFile .\strings.txt > .\generated.txt
#>

[CmdletBinding()]
param(
	[string]$InputFile = (Join-Path $PSScriptRoot "input.txt")
)

if (-not [System.IO.File]::Exists($InputFile)) {
	throw "Input file not found: $InputFile"
}

$reader = [System.IO.StreamReader]::new($InputFile)
$output = [System.Console]::OpenStandardOutput()
$writer = [System.IO.StreamWriter]::new(
	$output,
	[System.Text.UTF8Encoding]::new($false),
	1MB
)
$testMode = $false
$generatedCount = 0

try {
	while ($null -ne ($line = $reader.ReadLine())) {
		$builder = [System.Text.StringBuilder]::new($line.Length + 10)

		for ($number = 0; $number -le 9999; $number++) {
			if ($testMode) {
				$generatedCount++
				if ($generatedCount -eq 10) {
					$null = $builder.Append("paulbirgit`n")
					continue
				}
			}
			$null = $builder.Append($line)
			$null = $builder.Append($number.ToString('D4'))
			$null = $builder.Append("`n")
		}

		$writer.Write($builder.ToString())
	}
}
finally {
	$reader.Dispose()
	$writer.Dispose()
	$output.Dispose()
}
