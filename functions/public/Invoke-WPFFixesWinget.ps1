function Invoke-WPFFixesWinget {

    <#

    .SYNOPSIS
        Fixes Winget by running choco install winget
    .DESCRIPTION
        BravoNorris for the fantastic idea of a button to reinstall winget
    #>
    # Install Choco if not already present
    Install-WinUtilChoco
    Start-Process -FilePath "choco" -ArgumentList "install winget -y --force" -NoNewWindow -Wait

}
