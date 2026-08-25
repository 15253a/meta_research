Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($MetaResearchMode)) {
    throw "guardian mode is required"
}
if ($MetaResearchHolderToken -notmatch "^[0-9a-f]{24}$") {
    throw "guardian holder token is invalid"
}
$controlEventName = "Local\MetaResearchPowerGuardian-$MetaResearchHolderToken-control"
$readyEventName = "Local\MetaResearchPowerGuardian-$MetaResearchHolderToken-ready"

if ($MetaResearchMode -eq "Query") {
    try {
        $queryEvent = [System.Threading.EventWaitHandle]::OpenExisting($readyEventName)
    }
    catch [System.Threading.WaitHandleCannotBeOpenedException] {
        exit 3
    }
    try {
        Write-Output (
            "META_RESEARCH_WINDOWS_GUARDIAN_CONFIRMED:$MetaResearchHolderToken"
        )
    }
    finally {
        $queryEvent.Dispose()
    }
    exit 0
}

if ($MetaResearchMode -eq "Release") {
    try {
        $releaseEvent = [System.Threading.EventWaitHandle]::OpenExisting($controlEventName)
    }
    catch [System.Threading.WaitHandleCannotBeOpenedException] {
        exit 3
    }
    try {
        [void]$releaseEvent.Set()
        Write-Output (
            "META_RESEARCH_WINDOWS_GUARDIAN_RELEASED:$MetaResearchHolderToken"
        )
    }
    finally {
        $releaseEvent.Dispose()
    }
    exit 0
}

if ($MetaResearchMode -ne "Hold") {
    throw "guardian mode is invalid"
}

$createdControl = $false
$controlEvent = [System.Threading.EventWaitHandle]::new(
    $false,
    [System.Threading.EventResetMode]::ManualReset,
    $controlEventName,
    [ref]$createdControl
)
if (-not $createdControl) {
    $controlEvent.Dispose()
    exit 4
}

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace MetaResearch.PowerGuardian {
    public enum PowerRequestType : int {
        PowerRequestDisplayRequired = 0,
        PowerRequestSystemRequired = 1,
        PowerRequestAwayModeRequired = 2,
        PowerRequestExecutionRequired = 3
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct ReasonContext {
        public UInt32 Version;
        public UInt32 Flags;
        public IntPtr SimpleReasonString;
    }

    public static class NativePower {
        public static readonly IntPtr InvalidHandleValue = new IntPtr(-1);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr PowerCreateRequest(ref ReasonContext context);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool PowerSetRequest(
            IntPtr powerRequestHandle,
            PowerRequestType requestType
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool PowerClearRequest(
            IntPtr powerRequestHandle,
            PowerRequestType requestType
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool CloseHandle(IntPtr handle);
    }
}
"@

$reasonPointer = [Runtime.InteropServices.Marshal]::StringToHGlobalUni(
    "Meta-research managed operation"
)
$context = [MetaResearch.PowerGuardian.ReasonContext]::new()
$context.Version = 0
$context.Flags = 1
$context.SimpleReasonString = $reasonPointer
$powerHandle = [IntPtr]::Zero
$requestSet = $false
$readyEvent = $null

try {
    $powerHandle = [MetaResearch.PowerGuardian.NativePower]::PowerCreateRequest(
        [ref]$context
    )
    if (
        $powerHandle -eq [IntPtr]::Zero -or
        $powerHandle -eq [MetaResearch.PowerGuardian.NativePower]::InvalidHandleValue
    ) {
        throw "PowerCreateRequest failed"
    }
    $requestSet = [MetaResearch.PowerGuardian.NativePower]::PowerSetRequest(
        $powerHandle,
        [MetaResearch.PowerGuardian.PowerRequestType]::PowerRequestSystemRequired
    )
    if (-not $requestSet) {
        throw "PowerSetRequest failed"
    }

    $createdReady = $false
    $readyEvent = [System.Threading.EventWaitHandle]::new(
        $false,
        [System.Threading.EventResetMode]::ManualReset,
        $readyEventName,
        [ref]$createdReady
    )
    if (-not $createdReady) {
        throw "guardian ready identity already exists"
    }

    Write-Output "META_RESEARCH_WINDOWS_GUARDIAN_READY:$PID`:$MetaResearchHolderToken"
    [Console]::Out.Flush()
    [void]$controlEvent.WaitOne()
}
finally {
    if ($requestSet -and $powerHandle -ne [IntPtr]::Zero) {
        [void][MetaResearch.PowerGuardian.NativePower]::PowerClearRequest(
            $powerHandle,
            [MetaResearch.PowerGuardian.PowerRequestType]::PowerRequestSystemRequired
        )
    }
    if ($powerHandle -ne [IntPtr]::Zero) {
        [void][MetaResearch.PowerGuardian.NativePower]::CloseHandle($powerHandle)
    }
    if ($null -ne $readyEvent) {
        $readyEvent.Dispose()
    }
    [Runtime.InteropServices.Marshal]::FreeHGlobal($reasonPointer)
    $controlEvent.Dispose()
}
