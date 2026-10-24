# CloudTec Center

One web console for your **Active Directory** domains and **VMware vSphere** (vCenter) servers.

- **Dashboard**: users, lockouts, computers, VMs, hosts and storage across every connection at a glance.
- **Active Directory**: browse and search users, groups and computers. Unlock accounts, enable or disable them, and reset passwords.
- **Reset password**: a page (and dashboard tile) to find a user, set or generate a new password, unlock the account, and require a change at next sign-in.
- **Domain controllers**: every DC with its site, Global Catalog / read-only flags and FSMO roles, plus a check from the CloudTec computer to ports 389, 636, 3268 and 9389 (AD Web Services, needed by the PowerShell AD cmdlets).
- **vSphere**: browse VMs, hosts, datastores and clusters. Power on, shut down or restart the guest, power off, and view VM details (guest OS, IP, disks, NICs).
- **Cross-check**: lines up AD computer accounts with VMs by hostname, so you can see VMs that never joined the domain and computer accounts with no VM behind them.
- **PowerShell**: a PowerShell window inside CloudTec (also on the dashboard). The session keeps variables, the current folder and loaded modules between commands, so you can `Import-Module ActiveDirectory` once and keep working.
- **Global search**: one search box across every domain and vCenter, with actions available straight from the results.
- **Demo mode**: add sample connections and try everything without a lab.

You can add as many domains and vCenters as you like.

## Quick start

**Windows:** double-click **`Start CloudTec.bat`**. It installs what's needed the first time (you need [Python](https://www.python.org/downloads/) with "Add python.exe to PATH" ticked), starts CloudTec and opens it in your browser. Keep its window open while you use CloudTec; closing it stops CloudTec.

**Any platform, from a terminal:**

```bash
pip install -r requirements.txt
CLOUDTEC_ADMIN_PASSWORD='choose-a-strong-one' uvicorn app.main:create_app --factory --port 8000
```

Open http://localhost:8000 and sign in, then go to **Connections**. Tick **Demo mode** to try it with sample data, or enter real details:

| Active Directory | |
|---|---|
| Domain controller | `dc01.corp.example.com` |
| Port | `636` (LDAPS) or `389` |
| Base DN | `DC=corp,DC=example,DC=com` |
| Bind account | a UPN (`svc-cloudtec@corp.example.com`) or full DN |

| vCenter | |
|---|---|
| vCenter host | `vcenter.corp.example.com` |
| Username | `administrator@vsphere.local` or a delegated account |

Use **Test connection** before saving. Untick **Verify certificate** only if your DC or vCenter uses a self-signed certificate you can't add to the trust store.

### Docker

```bash
CLOUDTEC_ADMIN_PASSWORD='choose-a-strong-one' docker compose up -d --build
```

Data (connections, encryption key) lives in the `cloudtec-data` volume.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLOUDTEC_ADMIN_PASSWORD` | unset | Fixes the console sign-in password. While set, the password can't be changed from the Settings page. |
| `CLOUDTEC_DATA_DIR` | `./data` | Where the SQLite database and encryption key are stored. |
| `CLOUDTEC_SECRET_KEY` | generated | Fernet key used to encrypt saved passwords. If unset, one is generated into `<data dir>/secret.key`. **Back this up**: without it, saved passwords can't be decrypted. |
| `CLOUDTEC_SECURE_COOKIES` | `false` | Set to `true` when serving over HTTPS. |
| `CLOUDTEC_SESSION_HOURS` | `8` | How long a sign-in lasts. |
| `CLOUDTEC_SHELL` | `local` | The PowerShell page: `local` allows it only in a browser on the same computer (`localhost`), `on` allows it from anywhere, `off` disables it. |
| `CLOUDTEC_POWERSHELL` | auto | Path to the PowerShell to use. By default `pwsh` (PowerShell 7) if installed, otherwise `powershell` (Windows PowerShell). |

## Admin password

- **Change it** under **Settings** after you sign in. The new password is stored as a salted hash in `<data dir>/admin_password.hash`, and any other signed-in browsers are signed out.
- **First start:** if `CLOUDTEC_ADMIN_PASSWORD` isn't set and no password has been saved yet, one is generated and written in plain text to `<data dir>/admin_password` so you can read it. It's replaced by the hash file the first time you change the password.
- **Forgot it?** Stop CloudTec, delete `<data dir>/admin_password.hash`, put the password you want in `<data dir>/admin_password`, and start it again. Then change it under Settings so it's stored hashed.

## PowerShell page

Commands run on the computer CloudTec runs on, as the Windows user who started CloudTec, with that user's permissions. Anyone who can sign in to CloudTec can run them, so:

- By default the page only works in a browser on the same computer. Leave `CLOUDTEC_SHELL` at `local` unless you really need remote access, and if you do, serve CloudTec over HTTPS.
- Every command is recorded with a timestamp in `<data dir>/shell_history.log`.
- **Connect** (above the terminal) signs the session in to a saved connection with its stored account and password, without showing or logging them:
  - *Active Directory*: AD cmdlets (`Get-ADUser`, `Set-ADUser`, …) then use that domain controller and account automatically. Needs the ActiveDirectory module (RSAT): `Add-WindowsCapability -Online -Name Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0` from an Administrator window.
  - *vCenter*: runs `Connect-VIServer`, so PowerCLI cmdlets (`Get-VM`, `Start-VM`, …) work. Needs PowerCLI: `Install-Module VCF.PowerCLI -Scope CurrentUser`.
  - The credential is also left in `$CloudTec['<connection name>'].Credential` for commands that take `-Credential`.
- Commands can't answer prompts (they run non-interactively), so add `-Force` or `-Confirm:$false` where needed. A command that runs longer than 5 minutes is stopped and the session restarts.

## Requirements on the other side

**Active Directory**
- Use a dedicated service account with only the rights you need, delegated on the relevant OUs: *Read* for browsing; *Write userAccountControl* for enable/disable; *Write lockoutTime* for unlock; *Reset password* for resets.
- Password resets only work over **LDAPS** (port 636). That's an Active Directory rule.
- Simple bind is used, so use LDAPS for anything beyond a lab.

**vCenter**
- vCenter 7.0 U2 or later (uses the `/api` REST endpoints).
- *Shut down* and *Restart* go through VMware Tools. *Power off* is a hard power-off.
- A read-only role is enough for browsing. Power actions need *Virtual machine > Interaction > Power on/off/Reset* and *Guest operations*.

## Security notes

- The console holds privileged credentials, so treat it like a jump host. Keep it on a management network, put it behind HTTPS (for example a reverse proxy), and set `CLOUDTEC_SECURE_COOKIES=true`.
- Saved passwords are encrypted at rest and never returned by the API.
- AD changes are limited to objects inside each connection's base DN.
- There is a single admin account. Per-user sign-in (for example via AD/LDAP or SSO) and an audit log are natural next steps.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests cover the API in demo mode, the vSphere connector against a mocked vCenter REST API, and the AD connector against ldap3's in-memory LDAP server. Interactive API docs are at `/api/docs`.

```
app/
  main.py              API routes
  auth.py              console sign-in
  store.py             encrypted connection storage (SQLite)
  connectors/
    ad.py              Active Directory (ldap3)
    vsphere.py         vCenter REST API (httpx)
    demo.py            sample-data stand-ins for both
  static/              web UI (plain HTML/CSS/JS, no build step)
tests/
```
