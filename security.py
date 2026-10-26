"""Security assessment: local checks first (Defender, firewall, UAC,
SmartScreen, Smart App Control, updates, autoruns, listening ports, local
admins, RDP, PowerShell policy, shares), AppControl's own catalogue second
(unsigned or brand-new binaries that ran recently, its block events), the
elevated helper's checks third (BitLocker, Secure Boot, SMB1, LSA, TPM),
then rule-based scoring. Claude, if configured, turns the compact JSON into a
readable report; without Claude the rule-based findings are the report.
"""
import datetime as dt
import json
import logging
import os
import sqlite3
import subprocess

log = logging.getLogger("lightsout.security")
NOWIN = 0x08000000
APPCONTROL_DB = r"C:\ProgramData\AppControl\app_data.db"

LOCAL_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$o = [ordered]@{}
$mp = Get-MpComputerStatus
if ($mp) { $o.defender = [ordered]@{ realtime=$mp.RealTimeProtectionEnabled; antispyware=$mp.AntispywareEnabled; sig_age_days=$mp.AntivirusSignatureAge; quick_scan_age_days=$mp.QuickScanAge; full_scan_age_days=$mp.FullScanAge; mode=$mp.AMRunningMode; tamper=$mp.IsTamperProtected } }
$o.threats_30d = @(Get-MpThreatDetection | ? { $_.InitialDetectionTime -gt (Get-Date).AddDays(-30) } | select -First 10 | % { [ordered]@{ when=[string]$_.InitialDetectionTime; threat=$_.ThreatID; action=$_.ActionSuccess; res=$_.Resources -join ';' } })
$o.firewall = @(Get-NetFirewallProfile | % { [ordered]@{ profile=$_.Name; enabled=[bool]$_.Enabled; inbound_default=[string]$_.DefaultInboundAction } })
$o.net_profiles = @(Get-NetConnectionProfile | % { [ordered]@{ name=$_.Name; category=[string]$_.NetworkCategory; iface=$_.InterfaceAlias } })
$p = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
$o.uac = [ordered]@{ enabled=$p.EnableLUA; admin_prompt=$p.ConsentPromptBehaviorAdmin; secure_desktop=$p.PromptOnSecureDesktop }
$o.smartscreen = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer').SmartScreenEnabled
$o.smart_app_control = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState
$o.rdp_enabled = ((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server').fDenyTSConnections -eq 0)
$o.ps_execution_policy = [string](Get-ExecutionPolicy)
$o.last_hotfix = [string]((Get-HotFix | sort InstalledOn -Descending | select -First 1).InstalledOn)
$o.pending_reboot = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
$o.os = [ordered]@{ build=[System.Environment]::OSVersion.Version.ToString(); edition=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').ProductName }
$o.local_admins = @(Get-LocalGroupMember -Group Administrators | % { $_.Name })
$o.guest_enabled = [bool](Get-LocalUser -Name Guest).Enabled
$o.users_no_password_expiry = @(Get-LocalUser | ? { $_.Enabled -and -not $_.PasswordRequired } | % { $_.Name })
$o.shares = @(Get-SmbShare | ? { $_.Name -notmatch '^(ADMIN\$|C\$|D\$|IPC\$|print\$)$' } | % { [ordered]@{ name=$_.Name; path=$_.Path } })
$procs = @{}; Get-Process | % { $procs[$_.Id] = $_.ProcessName }
$o.listening = @(Get-NetTCPConnection -State Listen | ? { $_.LocalAddress -notin @('127.0.0.1','::1') } | sort LocalPort -Unique | % { [ordered]@{ port=$_.LocalPort; addr=$_.LocalAddress; proc=$procs[[int]$_.OwningProcess] } } | select -First 40)
$runs = @()
foreach ($k in 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run','HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run') {
  $v = Get-ItemProperty $k; if ($v) { $v.PSObject.Properties | ? { $_.Name -notmatch '^PS' } | % { $runs += [ordered]@{ where=$k.Split('\')[0]; name=$_.Name; cmd=([string]$_.Value).Substring(0,[Math]::Min(120,([string]$_.Value).Length)) } } }
}
$o.autoruns = $runs
$o.startup_folder = @(Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup" | % { $_.Name })
$o.tasks_non_ms = @(Get-ScheduledTask | ? { $_.TaskPath -notlike '\Microsoft*' -and $_.State -ne 'Disabled' } | % { [ordered]@{ name=$_.TaskName; path=$_.TaskPath; run=([string]($_.Actions | % { $_.Execute + ' ' + $_.Arguments })).Substring(0, [Math]::Min(100, ([string]($_.Actions | % { $_.Execute + ' ' + $_.Arguments })).Length)) } } | select -First 40)
$o | ConvertTo-Json -Compress -Depth 5
"""


def local_checks():
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", LOCAL_PS],
                           capture_output=True, text=True, timeout=180, creationflags=NOWIN, encoding="utf-8",
                           errors="replace")
        return json.loads(r.stdout.strip() or "{}")
    except Exception as e:
        log.exception("local checks")
        return {"error": str(e)}


def appcontrol_checks(days=7):
    """What AppControl has seen lately, from its SQLite catalogue (read-only)."""
    if not os.path.exists(APPCONTROL_DB):
        return {"installed": False}
    out = {"installed": True}
    try:
        c = sqlite3.connect(f"file:{APPCONTROL_DB}?mode=ro&immutable=1", uri=True)
        since = int((dt.datetime.now() - dt.timedelta(days=days)).timestamp() * 1000)
        out["binaries_total"] = c.execute("select count(*) from binary").fetchone()[0]
        rows = c.execute("""select b.name, b.company, b.filename, b.added, p.name
                            from binary b left join publishers p on p.id = b.publisher
                            where b.added >= ? order by b.added desc limit 60""", (since,)).fetchall()
        new = []
        for name, company, path, added, pub in rows:
            new.append({"name": name, "company": company or "", "path": (path or "")[:90],
                        "signed_by": pub or "", "unsigned": not pub,
                        "first_seen": dt.datetime.fromtimestamp(added / 1000).strftime("%Y-%m-%d")})
        out["new_binaries"] = new
        out["new_unsigned"] = [n for n in new if n["unsigned"]][:25]
        ev = c.execute("select event_type, count(*) from history where timestamp >= ? group by event_type", (since,)).fetchall()
        out["events_by_type"] = {str(k): v for k, v in ev}
        out["rules"] = c.execute("select count(*) from rule").fetchone()[0]
        c.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def score(data):
    """Rule-based findings: list of (severity, title, detail, fix)."""
    f = []
    d = data.get("local", {})
    dfd = d.get("defender") or {}
    if dfd and not dfd.get("realtime"):
        f.append(("high", "Defender real-time protection is off", "Nothing is scanning new files.",
                  "Windows Security > Virus & threat protection > Manage settings > Real-time protection: On"))
    if dfd and (dfd.get("sig_age_days") or 0) > 7:
        f.append(("medium", f"Antivirus signatures are {dfd.get('sig_age_days')} days old", "Updates are not arriving.",
                  "Windows Security > Virus & threat protection > Protection updates > Check for updates"))
    if dfd and (dfd.get("quick_scan_age_days") or 0) > 14 and (dfd.get("full_scan_age_days") or 0) > 30:
        f.append(("low", "No recent Defender scan", "Quick scan older than two weeks.",
                  "Windows Security > Virus & threat protection > Quick scan"))
    if dfd and dfd.get("tamper") is False:
        f.append(("medium", "Defender tamper protection is off", "Malware can switch Defender off silently.",
                  "Windows Security > Virus & threat protection > Manage settings > Tamper Protection: On"))
    if d.get("threats_30d"):
        f.append(("high", f"{len(d['threats_30d'])} threat detections in the last 30 days", json.dumps(d["threats_30d"][:3])[:300],
                  "Review Windows Security > Protection history"))
    for p in d.get("firewall") or []:
        if not p.get("enabled"):
            f.append(("high", f"Firewall off for the {p['profile']} profile", "Inbound connections are not filtered.",
                      "Windows Security > Firewall & network protection > turn it on"))
    for n in d.get("net_profiles") or []:
        if n.get("category") == "Public" and "Wi-Fi" not in (n.get("iface") or ""):
            pass
    uac = d.get("uac") or {}
    if uac.get("enabled") == 0:
        f.append(("high", "UAC is disabled", "Every program runs with full admin rights.",
                  "Control Panel > User Accounts > Change User Account Control settings > second notch from top"))
    elif uac.get("admin_prompt") == 0:
        f.append(("medium", "UAC never prompts", "Admin elevation happens silently.", "Raise the UAC slider to the default"))
    if d.get("smartscreen") in ("Off", "off"):
        f.append(("medium", "SmartScreen is off", "Downloaded malware is not reputation-checked.",
                  "Windows Security > App & browser control > Reputation-based protection"))
    if d.get("rdp_enabled"):
        f.append(("medium", "Remote Desktop is enabled", "An exposed RDP service is the top ransomware entry point.",
                  "Settings > System > Remote Desktop: Off (unless you use it; then keep NLA on)"))
    if (d.get("ps_execution_policy") or "").lower() == "unrestricted":
        f.append(("low", "PowerShell execution policy is Unrestricted", "Any script runs without a prompt.",
                  "Run as admin: Set-ExecutionPolicy RemoteSigned"))
    if d.get("pending_reboot"):
        f.append(("medium", "Windows Update is waiting for a restart", "Patches are not active until you reboot.", "Restart the PC"))
    lh = d.get("last_hotfix") or ""
    try:
        age = (dt.datetime.now() - dt.datetime.strptime(lh[:10], "%m/%d/%Y")).days if lh else None
    except Exception:
        age = None
    if age is not None and age > 45:
        f.append(("medium", f"Last Windows update installed {age} days ago", "Security patches are missing.",
                  "Settings > Windows Update > Check for updates"))
    if d.get("guest_enabled"):
        f.append(("medium", "Guest account is enabled", "Anyone at the keyboard can sign in.", "Disable the Guest account"))
    if d.get("users_no_password_expiry"):
        f.append(("high", f"Accounts without a password: {', '.join(d['users_no_password_expiry'])}", "Anyone can sign in.",
                  "Set a password for those accounts"))
    admins = [a for a in (d.get("local_admins") or []) if not a.lower().endswith(("\\administrator",))]
    if len(admins) > 2:
        f.append(("low", f"{len(admins)} accounts in Administrators", ", ".join(admins)[:200],
                  "Keep daily-use accounts as standard users where you can"))
    if d.get("shares"):
        f.append(("low", f"{len(d['shares'])} network shares", json.dumps(d["shares"])[:200], "Remove shares you do not use"))
    listening = [l for l in (d.get("listening") or []) if l.get("addr") in ("0.0.0.0", "::")]
    if listening:
        risky = [l for l in listening if l.get("port") in (3389, 445, 139, 135, 5900, 21, 23, 22)]
        sev = "medium" if risky else "low"
        f.append((sev, f"{len(listening)} services listening on all interfaces",
                  ", ".join(f"{l['port']}/{l.get('proc')}" for l in listening[:12]),
                  "Close what you do not recognise; the firewall should block them on public networks"))
    pv = data.get("privileged", {})
    bl = pv.get("bitlocker")
    if isinstance(bl, list) and any(str(v.get("status")) in ("0", "Off") for v in bl):
        f.append(("medium", "A drive is not encrypted with BitLocker", "A stolen laptop gives up every file.",
                  "Settings > Privacy & security > Device encryption / BitLocker"))
    if pv.get("secure_boot") is False:
        f.append(("medium", "Secure Boot is off", "Bootkits can load before Windows.", "Enable Secure Boot in the UEFI setup"))
    if pv.get("smb1") is True:
        f.append(("high", "SMBv1 is enabled", "The protocol WannaCry used.", "Run as admin: Disable-WindowsOptionalFeature -Online -FeatureName SMB1Protocol"))
    if pv.get("lsa_ppl") in (0, None) and pv:
        f.append(("low", "LSA protection (RunAsPPL) is not on", "Credential dumping is easier.",
                  "Windows Security > Device security > Core isolation > Local Security Authority protection: On"))
    ac = data.get("appcontrol", {})
    if ac.get("installed"):
        if ac.get("new_unsigned"):
            names = ", ".join(sorted({n["name"] for n in ac["new_unsigned"]}))[:300]
            f.append(("low", f"{len(ac['new_unsigned'])} unsigned programs first ran this week (AppControl)", names,
                      "Fine if you installed them; anything you do not recognise deserves a look"))
    order = {"high": 0, "medium": 1, "low": 2}
    f.sort(key=lambda x: order[x[0]])
    return f


def assess(helper_call=None, claude=None, report_dir=None):
    """Run everything. Returns dict with 'findings', 'summary', 'report' (markdown) and 'path'."""
    data = {"when": dt.datetime.now().isoformat(timespec="minutes"), "local": local_checks(),
            "appcontrol": appcontrol_checks()}
    if helper_call:
        data["privileged"] = helper_call("/security", timeout=120) or {"error": "helper not running"}
    findings = score(data)
    counts = {s: sum(1 for x in findings if x[0] == s) for s in ("high", "medium", "low")}
    summary = (f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low" if findings else "no issues found")
    lines = [f"# Security assessment, {data['when']}", "", f"**Result:** {summary}", ""]
    for sev, title, detail, fix in findings:
        lines += [f"## [{sev.upper()}] {title}", f"{detail}", f"**Fix:** {fix}", ""]
    narrative, err = "", ""
    if claude is not None:
        compact = {"findings": [{"severity": s, "title": t, "detail": d_[:200]} for s, t, d_, _ in findings],
                   "defender": data["local"].get("defender"), "firewall": data["local"].get("firewall"),
                   "listening": data["local"].get("listening", [])[:15], "autoruns": data["local"].get("autoruns", [])[:25],
                   "tasks_non_ms": data["local"].get("tasks_non_ms", [])[:20],
                   "appcontrol_new_unsigned": data.get("appcontrol", {}).get("new_unsigned", [])[:15],
                   "privileged": data.get("privileged")}
        narrative, err = claude.security_narrative(json.dumps(compact, default=str))
        if narrative:
            lines = [f"# Security assessment, {data['when']}", "", narrative, "", "---", "", "## Local checks"] + lines[2:]
    report = "\n".join(lines)
    path = None
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, f"security-{dt.date.today().isoformat()}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report)
        with open(os.path.join(report_dir, "security-latest.json"), "w", encoding="utf-8") as fh:
            json.dump({"when": data["when"], "summary": summary, "counts": counts, "claude_error": err,
                       "findings": findings}, fh, indent=1, default=str)
    return {"findings": findings, "summary": summary, "counts": counts, "report": report, "path": path,
            "claude_error": err, "data": data}


if __name__ == "__main__":
    import time
    t = time.time()
    r = assess()
    print(f"{time.time() - t:.1f}s", r["summary"])
    print(r["report"][:3000])
