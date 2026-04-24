# pCloud Token Renewal via rclone (OAuth Method)

This guide explains how to renew/refresh your pCloud OAuth token using rclone when working from a remote/headless machine (like a Raspberry Pi) via SSH.

## Prerequisites

- **Remote Machine:** Raspberry Pi / Linux server with rclone installed
- **Local Machine:** Windows PC with SSH client (MobaXterm recommended)
- **Existing rclone remote:** Already configured pCloud remote

## Method: rclone authorize (Windows PC + SSH to Remote)

This is the **recommended method** when you don't have rclone installed on your local Windows machine.

### Step 1: Download rclone Portable (Windows)

```powershell
# In PowerShell on your Windows PC:
cd $env:TEMP
Invoke-WebRequest -Uri "https://downloads.rclone.org/rclone-current-windows-amd64.zip" -OutFile "rclone.zip"
Expand-Archive -Path "rclone.zip" -DestinationPath "."
cd rclone-*-windows-amd64
```

**Alternative:** Download manually from https://rclone.org/downloads/ and extract.

### Step 2: Start rclone config on Remote Machine

**On the Raspberry Pi via SSH:**

```bash
rclone config

# Choose:
e) Edit existing remote
n) New remote

# Select your pCloud remote (e.g., "1")

# Press Enter for all options until you reach:
# "Already have a token - refresh?"
y) Yes

# At "Use web browser to automatically authenticate?"
n) No  # Important! Say NO

# rclone will then show:
# Option config_token.
# Execute the following on the machine with the web browser:
#     rclone authorize "pcloud" "ey....vbSJ9"
# Then paste the result.
# config_token>
```

**Important:** Leave this terminal session open and waiting at `config_token>`

### Step 3: Run rclone authorize on Windows

**Copy the exact command from the SSH session**, then run it in PowerShell:

```powershell
# In the Temp\rclone-*-windows-amd64 directory:
.\rclone.exe authorize "pcloud" "eyJo.......EXACT_STRING_FROM_SSH"
```

**What happens:**
1. Browser opens automatically
2. You log into pCloud
3. You authorize rclone
4. PowerShell shows:

```
Paste the following into your remote machine --->
eyJh.....9  LONG_TOKEN_STRING
<---End paste
```

### Step 4: Complete Configuration on Remote

1. **Copy the entire token** (everything between the arrows)
2. **Go back to your SSH session** (where `config_token>` is waiting)
3. **Paste the token** and press Enter

```bash
config_token> eyJ..J9 LONG_TOKEN_STRING

# Rclone will then ask:
Keep this "pcloud_remote" remote?
y) Yes this is OK (default)
e) Edit this remote
d) Delete this remote
y/e/d> y

# Then quit:
q) Quit config
```

### Step 5: Verify the New Token

```bash
# Test that rclone can access pCloud with the new token:
rclone lsd pcloud_remote:

# Should list your pCloud folders
```

## Troubleshooting

### Browser doesn't open automatically
- Make sure you're in the correct directory (`rclone-*-windows-amd64`)
- Try running as Administrator
- Check if a firewall blocks localhost connections

### "Invalid format" error with SSH keys
- This means your SSH key is in PuTTY PPK format
- Use MobaXterm (supports PPK natively) instead of Windows OpenSSH
- Or convert the key with PuTTYgen: Load PPK → Conversions → Export OpenSSH key

### Multiple rclone entries in pCloud Security Settings
- Each `rclone authorize` creates a new OAuth authorization
- Go to https://my.pcloud.com/ → Settings → Security → Authorized Applications
- Revoke old "rclone.org" entries, keep only the newest

### Token still shows as old in running scripts
- Scripts load tokens from `.env` file at startup
- Update your `.env` file with the new token:

```bash
# Extract token from rclone config:
rclone config show pcloud_remote | grep access_token

# Copy only the access_token value (without quotes/JSON)
# Update .env:
nano /opt/apps/pcloud-tools/main/.env
# Change: PCLOUD_TOKEN=NEW_TOKEN_HERE
```

- Running scripts will continue with old token (still valid until revoked)
- Next script start will use the new token

## Alternative Method: SSH Port Forwarding (Advanced)

If you have rclone installed locally and want to use SSH tunneling:

```powershell
# From Windows PowerShell:
ssh -L localhost:53682:localhost:53682 user@remote-pi

# Then on the Pi in that SSH session:
rclone config
# Answer "y" to "Use web browser automatically"
# Browser on your PC opens via the tunnel
```

## Security Notes

- Never share your OAuth token / access_token
- Tokens are long-lived but can be revoked
- Review active applications regularly: https://my.pcloud.com/#page=settings&settings=tab-security
- Each `rclone authorize` creates an independent session (revoke old ones!)

## Integration with pCloud-Tools

After refreshing the rclone token, update your pCloud-Tools configuration:

```bash
# Option 1: Update .env file
nano /opt/apps/pcloud-tools/main/.env
PCLOUD_TOKEN=<new_token_from_rclone_config>

# Option 2: Extract from rclone
rclone config show pcloud_office_santinel
# Copy the access_token value
```

## References

- [rclone Remote Setup Documentation](https://rclone.org/remote_setup/)
- [pCloud OAuth Documentation](https://docs.pcloud.com/methods/oauth_2.0/)
- [rclone Downloads](https://rclone.org/downloads/)
