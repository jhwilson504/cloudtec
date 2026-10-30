<img src="branding/kastec-logo.svg" width="88" alt="KasTec logo">

# KasTec Center

> Formerly **CloudTec**. Settings now use `KASTEC_…` names; the older `CLOUDTEC_…` names still work, and existing data, connections and sign-ins carry over unchanged.

One web console for your **Active Directory** domains, **VMware vSphere** (vCenter) servers and **Dell iDRAC** server hardware.

- **Dashboard**: users, lockouts, computers, VMs, hosts and storage across every connection at a glance.
- **Active Directory**: browse and search users, groups and computers. Unlock accounts, enable or disable them, and reset passwords.
- **Reset password**: a page (and dashboard tile) to find a user, set or generate a new password, unlock the account, and require a change at next sign-in.
- **Domain controllers**: every DC with its site, Global Catalog / read-only flags and FSMO roles, plus a check from the KasTec computer to ports 389, 636, 3268 and 9389 (AD Web Services, needed by the PowerShell AD cmdlets).
- **vSphere**: browse VMs, hosts, datastores and clusters. Power on, shut down or restart the guest, power off, and view VM details (guest OS, IP, disks, NICs).
- **Servers (iDRAC)**: hardware inventory and health from each Dell server's iDRAC: model, service tag, BIOS and iDRAC firmware, CPUs, memory, and each power supply, with any that has lost input power called out on the page and the dashboard. Read-only: KasTec never changes anything on a server.
- **Cross-check**: lines up AD computer accounts with VMs by hostname, so you can see VMs that never joined the domain and computer accounts with no VM behind them.
- **PowerShell**: a PowerShell window inside KasTec (also on the dashboard). The session keeps variables, the current folder and loaded modules between commands, so you can `Import-Module ActiveDirectory` once and keep working.
- **Global search**: one search box across every domain and vCenter, with actions available straight from the results.
- **Demo mode**: add sample connections and try everything without a lab.

You can add as many domains and vCenters as you like.

## Quick start

**Windows:** double-click **`Start KasTec.bat`**. It installs what's needed the first time (you need [Python](https://www.python.org/downloads/) with "Add python.exe to PATH" ticked), starts KasTec and opens it in your browser. Keep its window open while you use KasTec; closing it stops KasTec.

**Any platform, from a terminal:**

```bash
pip install -r requirements.txt
KASTEC_ADMIN_PASSWORD='choose-a-strong-one' uvicorn app.main:create_app --factory --port 8000
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

| iDRAC | |
|---|---|
| iDRAC addresses | one per line, e.g. `10.0.10.11` … `10.0.10.16`. One connection can hold every server that shares the same iDRAC account. |
| Username | `root`, or better, an iDRAC user with the *Read Only* role |

Use **Test connection** before saving. Untick **Verify certificate** only if your DC or vCenter uses a self-signed certificate you can't add to the trust store.

### Docker

```bash
KASTEC_ADMIN_PASSWORD='choose-a-strong-one' docker compose up -d --build
```

Data (connections, encryption key) lives in the `cloudtec-data` volume.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `KASTEC_ADMIN_PASSWORD` | unset | Fixes the console sign-in password. While set, the password can't be changed from the Settings page. |
| `KASTEC_DATA_DIR` | `./data` | Where the SQLite database and encryption key are stored. |
| `KASTEC_SECRET_KEY` | generated | Fernet key used to encrypt saved passwords. If unset, one is generated into `<data dir>/secret.key`. **Back this up**: without it, saved passwords can't be decrypted. |
| `KASTEC_SECURE_COOKIES` | `false` | Set to `true` when serving over HTTPS. |
| `KASTEC_SESSION_HOURS` | `8` | How long a sign-in lasts. |
| `KASTEC_SHELL` | `local` | The PowerShell page: `local` allows it only in a browser on the same computer (`localhost`), `on` allows it from anywhere, `off` disables it. |
| `KASTEC_POWERSHELL` | auto | Path to the PowerShell to use. By default `pwsh` (PowerShell 7) if installed, otherwise `powershell` (Windows PowerShell). |
| `KASTEC_SMTP_HOST` | unset | Mail server for certificate expiry emails. Without it, alerts only show on the dashboard. |
| `KASTEC_SMTP_PORT` | `25` | Mail server port. `465` uses SSL; with `587` also set `KASTEC_SMTP_STARTTLS=true`. |
| `KASTEC_SMTP_STARTTLS` | `false` | Upgrade the mail connection with STARTTLS. |
| `KASTEC_SMTP_USER`, `KASTEC_SMTP_PASSWORD` | unset | Sign in to the mail server, if it needs it. |
| `KASTEC_ALERT_FROM` | SMTP user | The From address of alert emails. |
| `KASTEC_CERT_PROXY` | auto | Web proxy for certificate checks, as `host:port`, or `off`. By default KasTec uses `HTTPS_PROXY` or the proxy set in Windows Internet Options, then tries directly. Set this if your PC gets its proxy from an automatic configuration script. |
| `KASTEC_CERT_CHECK_HOURS` | `12` | How often certificates are checked. `0` turns the background check off. |

## Letting colleagues use it (HTTPS on your network)

By default KasTec only answers on the PC it runs on (`http://localhost:8000`). To let colleagues sign in from their own PCs, double-click **`Set up network access.bat`** (it asks for Administrator so it can add the firewall rule) and choose how KasTec gets its HTTPS certificate:

1. **Company CA (recommended).** KasTec writes `data\tls\request.csr`. Have your CA sign it, e.g. with AD Certificate Services:
   `certreq -submit -attrib "CertificateTemplate:WebServer" data\tls\request.csr cloudtec.cer`
   then run the setup again, choose **2** and point it at the issued certificate. Domain PCs already trust your CA, so there are no browser warnings.
2. **Import a `.pfx`** (certificate with private key) you already have.
3. **KasTec's own CA.** Quick, but each colleague's PC must trust `data\tls\KasTec-CA.cer` once: deploy it by Group Policy (*Computer Configuration > Policies > Windows Settings > Security Settings > Public Key Policies > Trusted Root Certification Authorities*) or double-click it on each PC and install it to *Local Machine > Trusted Root Certification Authorities*.

The setup opens TCP **8443** in Windows Firewall (Domain and Private networks) and switches KasTec to HTTPS on the network. Restart it with **Start KasTec**; colleagues then open `https://<your PC's name>:8443`. In this mode sign-in cookies are marked Secure. The PowerShell page still only works in a browser on the KasTec PC itself. To go back to "this PC only", run the setup again and choose **5**. You can also script it: `python -m app.tls --help`.

KasTec only runs while its window is open on that PC, so for a team a small always-on server or VM is a better home for it.

## Certificate expiry alerts

KasTec watches certificates and warns **30 days** and again **21 days** before each one expires, and once more if it does expire: a banner on everyone's dashboard, and an email when `KASTEC_SMTP_HOST` is set. KasTec only alerts; it never installs or changes a certificate.

The **Certificates** page in the sidebar is the certificate dashboard: how many are expired, inside 21 days, inside 30 days or fine, and every watched certificate sorted by days left, coloured red (expired or 21 days), amber (30 days) or green.

It always watches its own HTTPS certificate. Admins add others at the bottom of the **Certificates** page, one per line:

```
btcme1.btcbahamas.com      javaughn.davis@btcbahamas.com   # mail server
10.255.215.121             # OCS dashboard
jfkvcs.btcbahamas.com:443  # vCenter
"C:\certs\trail mail.cer"  owner@example.com
```

Each line is a host name or IP address (port 443 unless you add `:port`) or a certificate file (`.cer`, `.crt`, `.pem`, `.der`, `.p7b`), then the email addresses of whoever owns it, then an optional `# note`. KasTec reads whatever certificate the address presents, even untrusted or expired ones, so internal servers and ESXi hosts work too. For a site behind a proxy such as Cloudflare, that's the proxy's certificate; to watch the origin's, add the origin's address.

**When a site can't be reached from your PC** (a firewall resets the connection, as happens to Cloudflare-fronted sites from some office networks), KasTec looks a public name up in the public certificate logs at [crt.sh](https://crt.sh) instead. Every trusted certificate is logged when it's issued. A name can have several at once, for example Cloudflare's automatic edge certificate and a GoDaddy one on the origin. For each issuer only the newest counts, and KasTec alerts on the issuer whose newest certificate expires first, which is the one nobody is renewing automatically. Start a line with `ct:` (`ct:btcme1.btcbahamas.com`) to always use the logs. Internal names and IP addresses aren't in the logs, so they need a direct connection or a certificate file.

Every alert email goes to the **Always email alerts to** addresses plus that certificate's owners, once per stage (30 days, 21 days, expired). A renewed certificate starts over. The list is saved in `<data dir>/cert_watch.json`, and what has been emailed in `cert_alerts.json`.

To check from the command line, or from Task Scheduler when KasTec isn't running: `python -m app.certwatch [--email]`. It exits with code 2 when a certificate needs attention.

## Users and roles

Sign in as **admin** with the admin password, then add people under **Users**. Each person signs in with their own username and password.

| Role | Can |
|---|---|
| Administrator | Everything, including Connections, Users and the PowerShell page |
| Operator | Dashboard, search, Active Directory (unlock, enable/disable, reset passwords), Domain controllers, vSphere (VM power), Cross-check, and changing their own password |

- Passwords are stored as salted scrypt hashes. Disabling, deleting or resetting a user's password signs them out everywhere.
- After 5 wrong passwords for a username from one address within 10 minutes, sign-in is refused for that username and address for the rest of the 10 minutes.
- Every sign-in (and failed attempt), AD change, VM power action, connection change and user change is written to `<data dir>/audit.log` (one JSON object per line), and the latest are shown under **Users → Recent activity**.

## Admin password

- **Change it** under **Settings** after you sign in. The new password is stored as a salted hash in `<data dir>/admin_password.hash`, and any other signed-in browsers are signed out.
- **First start:** if `KASTEC_ADMIN_PASSWORD` isn't set and no password has been saved yet, one is generated and written in plain text to `<data dir>/admin_password` so you can read it. It's replaced by the hash file the first time you change the password.
- **Forgot it?** Stop KasTec, delete `<data dir>/admin_password.hash`, put the password you want in `<data dir>/admin_password`, and start it again. Then change it under Settings so it's stored hashed.

## PowerShell page

Commands run on the computer KasTec runs on, as the Windows user who started KasTec, with that user's permissions. Anyone who can sign in to KasTec can run them, so:

- By default the page only works in a browser on the same computer. Leave `KASTEC_SHELL` at `local` unless you really need remote access, and if you do, serve KasTec over HTTPS.
- Every command is recorded with a timestamp in `<data dir>/shell_history.log`.
- **Connect** (above the terminal) signs the session in to a saved connection with its stored account and password, without showing or logging them:
  - *Active Directory*: AD cmdlets (`Get-ADUser`, `Set-ADUser`, …) then use that domain controller and account automatically. Needs the ActiveDirectory module (RSAT): `Add-WindowsCapability -Online -Name Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0` from an Administrator window.
  - *vCenter*: runs `Connect-VIServer`, so PowerCLI cmdlets (`Get-VM`, `Start-VM`, …) work. Needs PowerCLI: `Install-Module VCF.PowerCLI -Scope CurrentUser`.
  - The credential is also left in `$KasTec['<connection name>'].Credential` for commands that take `-Credential`.
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

**iDRAC**
- iDRAC9 or later (uses the Redfish API on port 443). The PC running KasTec must be able to reach each iDRAC address.
- A *Read Only* iDRAC user is enough: KasTec only reads.
- iDRACs ship with a self-signed certificate, so leave *Verify certificate* unticked unless you've given them trusted ones.

## Security notes

- The console holds privileged credentials, so treat it like a jump host. Keep it on a management network, put it behind HTTPS (for example a reverse proxy), and set `KASTEC_SECURE_COOKIES=true`.
- Saved passwords are encrypted at rest and never returned by the API.
- AD changes are limited to objects inside each connection's base DN.
- There is a single admin account. Per-user sign-in (for example via AD/LDAP or SSO) and an audit log are natural next steps.

## Logo

The KasTec mark is a K whose arms end in two nodes: one console joining your directory, virtualisation and hardware. Files are in `branding/`:

| File | Use |
|---|---|
| `kastec-logo.svg` | The mark, any size (also used by the app as its icon) |
| `kastec-logo-512.png`, `kastec-logo-256.png` | The mark as PNG, e.g. Teams, intranet tiles |
| `kastec-lockup-light.png` / `kastec-lockup-dark.png` | Mark and name, for light / dark backgrounds |
| `kastec.ico` | Windows icon, e.g. for the Start KasTec shortcut |
| `concepts.html` | The three concepts that were considered |

Colours: blue `#3b82f6` to `#1e40af` (gradient), accent `#2563eb`, light node `#7dd3fc`.

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
    idrac.py           Dell iDRAC Redfish API (httpx), read-only
    demo.py            sample-data stand-ins for all three
  static/              web UI (plain HTML/CSS/JS, no build step)
tests/
```
