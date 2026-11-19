# A stand-in for VMware PowerCLI, for KasTec's tests: answers the calls the network map makes with
# objects shaped like vCenter's (one host in cluster JFK, a standard vSwitch, a distributed switch, two VMs).

Add-Type -TypeDefinition @'
namespace VMware.Vim {
  public class PortRef { public string PortgroupKey; }
  public class NicBacking { public string DeviceName; public PortRef Port; }
  public class NicConnectable { public bool Connected; }
  public class VirtualEthernetCard { public NicBacking Backing; public NicConnectable Connectable; public string MacAddress; }
  public class VirtualDisk { public string Label; }
}
'@

function o($h) { [pscustomobject]$h }

function Set-PowerCLIConfiguration { param($Scope, $InvalidCertificateAction, $ParticipateInCeip, [switch]$Confirm) }

function Connect-VIServer {
  param($Server, $Port, $Credential)
  if ($Credential.GetNetworkCredential().Password -ne 'right') { throw 'Cannot complete login due to an incorrect user name or password.' }
  $global:FakeConnects++
  o @{ Name = $Server; IsConnected = $true }
}

function Get-View {
  param([Parameter(Position = 0)]$Id, $Server, $ViewType, $Property)
  if ($Id) {
    $ns = o @{ Name = 'networkSystem' }
    $ns | Add-Member -MemberType ScriptMethod -Name QueryNetworkHint -Value {
      param($x)
      @((o @{ Device = 'vmnic0'; ConnectedSwitchPort = (o @{ DevId = 'SW-JFK-CORE'; PortId = 'Gi1/0/7' }) }),
        (o @{ Device = 'vmnic2'; LldpInfo = (o @{ PortId = 'Eth1/12'; Parameter = @((o @{ Key = 'System Name'; Value = 'NX-JFK-01' })) }) }))
    }
    return $ns
  }
  switch ($ViewType) {
    'ClusterComputeResource' { o @{ Name = 'JFK'; MoRef = (o @{ Value = 'domain-c1' }) } }
    'DistributedVirtualSwitch' { o @{ Name = 'dvs-JFK'; Uuid = 'uuid-1'; MoRef = (o @{ Value = 'dvs-1' }) } }
    'DistributedVirtualPortgroup' {
      o @{ Name = 'Servers-VLAN20'; Key = 'dvportgroup-5'; Config = (o @{ Uplink = $false; DistributedVirtualSwitch = (o @{ Value = 'dvs-1' })
        DefaultPortConfig = (o @{ Vlan = (o @{ VlanId = 20 }) }) }) }
      o @{ Name = 'dvs-JFK-Uplinks'; Key = 'dvportgroup-4'; Config = (o @{ Uplink = $true; DistributedVirtualSwitch = (o @{ Value = 'dvs-1' })
        DefaultPortConfig = (o @{ Vlan = (o @{ VlanId = @((o @{ Start = 0; End = 4094 })) }) }) }) }
    }
    'HostSystem' {
      $pn = { param($k, $d, $s) o @{ Key = $k; Device = $d; Mac = "00:00:00:00:00:0$($d[-1])"; LinkSpeed = $(if ($s) { o @{ SpeedMb = $s } } else { $null }) } }
      o @{ Name = 'jfkvh10.btcbahamas.com'; MoRef = (o @{ Value = 'host-10' }); Parent = (o @{ Value = 'domain-c1' })
        Runtime = (o @{ ConnectionState = 'connected' }); ConfigManager = (o @{ NetworkSystem = 'networkSystem-10' })
        Config = (o @{ Product = (o @{ Version = '6.7.0' })
          VirtualNicManagerInfo = (o @{ NetConfig = @(
            (o @{ NicType = 'management'; SelectedVnic = @('m-vmk0'); CandidateVnic = @((o @{ Key = 'm-vmk0'; Device = 'vmk0' })) }),
            (o @{ NicType = 'vmotion'; SelectedVnic = @('v-vmk1'); CandidateVnic = @((o @{ Key = 'v-vmk1'; Device = 'vmk1' })) })) })
          Network = (o @{
            Pnic = @((& $pn 'k0' 'vmnic0' 1000), (& $pn 'k1' 'vmnic1' 0), (& $pn 'k2' 'vmnic2' 10000))
            Vswitch = @((o @{ Name = 'vSwitch0'; Pnic = @('k0', 'k1') }), (o @{ Name = 'vSwitch-Internal'; Pnic = $null }))
            Portgroup = @((o @{ Spec = (o @{ Name = 'Management Network'; VlanId = 10; VswitchName = 'vSwitch0' }) }),
                          (o @{ Spec = (o @{ Name = 'VCMS-Only'; VlanId = 0; VswitchName = 'vSwitch0' }) }))
            ProxySwitch = @((o @{ DvsUuid = 'uuid-1'; DvsName = 'dvs-JFK'; Pnic = @('k2') }))
            Vnic = @((o @{ Device = 'vmk0'; Portgroup = 'Management Network'; Spec = (o @{ Ip = (o @{ IpAddress = '10.0.19.10' }); DistributedVirtualPort = $null }) }),
                     (o @{ Device = 'vmk1'; Portgroup = $null; Spec = (o @{ Ip = (o @{ IpAddress = '10.0.40.10' }); DistributedVirtualPort = (o @{ PortgroupKey = 'dvportgroup-5' }) }) }))
          }) }) }
    }
    'VirtualMachine' {
      $std = New-Object VMware.Vim.VirtualEthernetCard
      $std.Backing = New-Object VMware.Vim.NicBacking; $std.Backing.DeviceName = 'VCMS-Only'
      $std.Connectable = New-Object VMware.Vim.NicConnectable; $std.Connectable.Connected = $true; $std.MacAddress = '00:50:56:aa:00:01'
      $dv = New-Object VMware.Vim.VirtualEthernetCard
      $dv.Backing = New-Object VMware.Vim.NicBacking; $dv.Backing.Port = New-Object VMware.Vim.PortRef; $dv.Backing.Port.PortgroupKey = 'dvportgroup-5'
      $dv.Connectable = New-Object VMware.Vim.NicConnectable; $dv.Connectable.Connected = $false
      $disk = New-Object VMware.Vim.VirtualDisk
      o @{ Name = 'BTCPRODDC1'; Runtime = (o @{ Host = (o @{ Value = 'host-10' }); PowerState = 'poweredOn' })
        Config = (o @{ Template = $false; Hardware = (o @{ Device = @($disk, $std) }) }) }
      o @{ Name = 'APP02'; Runtime = (o @{ Host = (o @{ Value = 'host-10' }); PowerState = 'poweredOff' })
        Config = (o @{ Template = $false; Hardware = (o @{ Device = @($dv) }) }) }
      o @{ Name = '%2fvmfs%2fvolumes%2f5793c116%2fPELATROTEST_03'; Runtime = (o @{ Host = $null; PowerState = 'poweredOff' })
        Config = $null }
      o @{ Name = 'TEMPLATE-W2022'; Runtime = (o @{ Host = (o @{ Value = 'host-10' }); PowerState = 'poweredOff' })
        Config = (o @{ Template = $true; Hardware = (o @{ Device = @() }) }) }
    }
  }
}

function Get-VMHost { param($Server) o @{ Name = 'jfkvh10.btcbahamas.com'; ConnectionState = 'Connected' } }
function Get-VM {
  param($Server, $Id)
  $all = (o @{ Name = 'BTCPRODDC1'; PowerState = 'PoweredOn'; Id = 'VirtualMachine-vm-1' }), (o @{ Name = 'APP02'; PowerState = 'PoweredOff'; Id = 'VirtualMachine-vm-2' })
  if ($Id) { $all | Where-Object { $_.Id -eq $Id } } else { $all }
}

function Invoke-VMScript {
  param($Server, $VM, $ScriptText, $GuestUser, $GuestPassword, $ScriptType)
  if (-not $VM) { throw 'Could not find VirtualMachine with given Id.' }
  if ($VM.PowerState -ne 'PoweredOn') { throw "Operation is not valid due to the current state of the object." }
  if ($GuestPassword -ne 'guestpw') { throw 'Failed to authenticate with the guest operating system using the supplied credentials.' }
  o @{ ExitCode = $(if ($ScriptText -like '*fail*') { 3 } else { 0 })
       ScriptOutput = "ran on $($VM.Name) as $GuestUser with ${ScriptType}:`n$ScriptText`nlast line ünïcode" }
}

function Get-Stat {
  param($Server, $Entity, $Stat, [switch]$Realtime, $MaxSamples)
  foreach ($e in $Entity) {
    foreach ($m in $Stat) {
      $v = if ($m -like '*received*') { 1250 } else { 250 }
      if ($e.Name -like 'jfk*') {
        o @{ Entity = $e; Instance = 'vmnic0'; MetricId = $m; Value = $v }
        o @{ Entity = $e; Instance = ''; MetricId = $m; Value = $v * 2 }
      } else {
        o @{ Entity = $e; Instance = ''; MetricId = $m; Value = $v / 5 }
        o @{ Entity = $e; Instance = '4000'; MetricId = $m; Value = $v / 5 }
      }
    }
  }
}
