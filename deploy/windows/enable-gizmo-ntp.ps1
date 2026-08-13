#Requires -RunAsAdministrator

param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$UpstreamPeers,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$GizmoAddress
)

$ErrorActionPreference = "Stop"

$provider = "HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer"
$ruleName = "GIZMo-NTP-PrivateLink"
$manualPeers = ($UpstreamPeers | ForEach-Object { "$_,0x9" }) -join " "

Set-ItemProperty -Path $provider -Name Enabled -Type DWord -Value 1
Set-Service -Name W32Time -StartupType Automatic
& w32tm.exe `
    /config `
    /manualpeerlist:"$manualPeers" `
    /syncfromflags:manual `
    /reliable:yes `
    /update | Out-Null

$rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($null -ne $rule) {
    Remove-NetFirewallRule -Name $ruleName
}
New-NetFirewallRule `
    -Name $ruleName `
    -DisplayName "GIZMo NTP on private maintenance link" `
    -Description "Allow only the configured GIZMo address to query this workstation for NTP." `
    -Enabled True `
    -Direction Inbound `
    -Action Allow `
    -Protocol UDP `
    -LocalPort 123 `
    -RemoteAddress $GizmoAddress `
    -Profile Any | Out-Null

Restart-Service -Name W32Time
& w32tm.exe /resync /rediscover | Out-Null

$enabled = Get-ItemPropertyValue -Path $provider -Name Enabled
$firewall = Get-NetFirewallRule -Name $ruleName

[pscustomobject]@{
    NtpServerProviderEnabled = [bool]$enabled
    WindowsTimeStatus = (Get-Service -Name W32Time).Status
    FirewallRuleEnabled = $firewall.Enabled
    AllowedRemoteAddress = (
        Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $firewall
    ).RemoteAddress -join ","
} | Format-List
