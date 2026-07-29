<#
  send_zpl_usb.ps1
  Sends a raw .zpl file straight to a USB-connected Zebra printer via the
  Windows print spooler -- no Zebra Setup Utilities / Nucleus Connector needed.

  USAGE:
    .\send_zpl_usb.ps1 -ZplFile "v32.zpl"
    .\send_zpl_usb.ps1 -ZplFile "v32.zpl" -PrinterName "ZDesigner ZD410-203dpi ZPL"

  If -PrinterName is omitted, the script looks for one installed printer whose
  name contains "ZDesigner" or "Zebra" and uses that.

  This writes the file's raw bytes directly to the printer via the Win32
  spooler API (OpenPrinter/StartDocPrinter/WritePrinter) with datatype "RAW".
  This avoids Out-Printer, which pipes data through PowerShell's text
  formatting layer and throws "Length cannot be less than zero" on very long
  single lines (e.g. the ^GFA graphic data lines in these labels).
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$ZplFile,

    [string]$PrinterName
)

if (-not (Test-Path $ZplFile)) {
    Write-Error "File not found: $ZplFile"
    exit 1
}

if (-not $PrinterName) {
    $zebraPrinters = Get-Printer | Where-Object { $_.Name -match "ZDesigner|Zebra" }
    if (-not $zebraPrinters) {
        Write-Error "No installed printer matching 'ZDesigner' or 'Zebra' was found. Pass -PrinterName explicitly (see exact name in Settings > Printers & Scanners)."
        exit 1
    } elseif (@($zebraPrinters).Count -gt 1) {
        Write-Host "Multiple Zebra printers found:"
        $zebraPrinters | ForEach-Object { Write-Host " - $($_.Name)" }
        Write-Error "Specify which one with -PrinterName."
        exit 1
    } else {
        $PrinterName = $zebraPrinters.Name
    }
}

Write-Host "Target printer: $PrinterName"
Write-Host "Sending: $ZplFile"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public class RawPrinterHelper
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public class DOCINFOA
    {
        [MarshalAs(UnmanagedType.LPStr)] public string pDocName;
        [MarshalAs(UnmanagedType.LPStr)] public string pOutputFile;
        [MarshalAs(UnmanagedType.LPStr)] public string pDataType;
    }

    [DllImport("winspool.Drv", EntryPoint = "OpenPrinterA", SetLastError = true, CharSet = CharSet.Ansi, ExactSpelling = true, CallingConvention = CallingConvention.StdCall)]
    public static extern bool OpenPrinter([MarshalAs(UnmanagedType.LPStr)] string szPrinter, out IntPtr hPrinter, IntPtr pd);

    [DllImport("winspool.Drv", EntryPoint = "ClosePrinter", SetLastError = true, ExactSpelling = true, CallingConvention = CallingConvention.StdCall)]
    public static extern bool ClosePrinter(IntPtr hPrinter);

    [DllImport("winspool.Drv", EntryPoint = "StartDocPrinterA", SetLastError = true, CharSet = CharSet.Ansi, ExactSpelling = true, CallingConvention = CallingConvention.StdCall)]
    public static extern bool StartDocPrinter(IntPtr hPrinter, Int32 level, [In, MarshalAs(UnmanagedType.LPStruct)] DOCINFOA di);

    [DllImport("winspool.Drv", EntryPoint = "EndDocPrinter", SetLastError = true, ExactSpelling = true, CallingConvention = CallingConvention.StdCall)]
    public static extern bool EndDocPrinter(IntPtr hPrinter);

    [DllImport("winspool.Drv", EntryPoint = "StartPagePrinter", SetLastError = true, ExactSpelling = true, CallingConvention = CallingConvention.StdCall)]
    public static extern bool StartPagePrinter(IntPtr hPrinter);

    [DllImport("winspool.Drv", EntryPoint = "EndPagePrinter", SetLastError = true, ExactSpelling = true, CallingConvention = CallingConvention.StdCall)]
    public static extern bool EndPagePrinter(IntPtr hPrinter);

    [DllImport("winspool.Drv", EntryPoint = "WritePrinter", SetLastError = true, ExactSpelling = true, CallingConvention = CallingConvention.StdCall)]
    public static extern bool WritePrinter(IntPtr hPrinter, IntPtr pBytes, Int32 dwCount, out Int32 dwWritten);

    public static bool SendBytesToPrinter(string szPrinterName, byte[] bytes)
    {
        IntPtr hPrinter;
        DOCINFOA di = new DOCINFOA();
        di.pDocName = "ZPL Raw Job";
        di.pDataType = "RAW";
        bool success = false;

        if (OpenPrinter(szPrinterName, out hPrinter, IntPtr.Zero))
        {
            if (StartDocPrinter(hPrinter, 1, di))
            {
                if (StartPagePrinter(hPrinter))
                {
                    IntPtr pUnmanagedBytes = Marshal.AllocCoTaskMem(bytes.Length);
                    Marshal.Copy(bytes, 0, pUnmanagedBytes, bytes.Length);
                    int dwWritten;
                    success = WritePrinter(hPrinter, pUnmanagedBytes, bytes.Length, out dwWritten);
                    Marshal.FreeCoTaskMem(pUnmanagedBytes);
                    EndPagePrinter(hPrinter);
                }
                EndDocPrinter(hPrinter);
            }
            ClosePrinter(hPrinter);
        }
        return success;
    }
}
"@

$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $ZplFile))
$ok = [RawPrinterHelper]::SendBytesToPrinter($PrinterName, $bytes)

if ($ok) {
    Write-Host "Done. $($bytes.Length) bytes sent."
} else {
    Write-Error "WritePrinter failed. Check the printer name matches exactly (Settings > Printers & Scanners), and that the printer is online."
}
