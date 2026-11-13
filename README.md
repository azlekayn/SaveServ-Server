# SaveServ-Server

The standalone headless SaveServ server. Compatible games and SaveServ Desktop
can connect using the same discovery, server selection and PIN pairing flow.
It does not require the desktop app and the existing API/configuration is unchanged.

## Docker (existing deployment)

```sh
docker compose up -d
```

Run this from `server/`. See the root README for environment variables, storage,
reverse-proxy setup, accounts and the save-management web UI.

## Without Docker

Python 3.10+ and its standard library are sufficient:

```sh
SAVESERV_DATA="$HOME/.local/share/saveserv-server" python3 server/saveserv.py
```

Windows PowerShell, from the repository root:

```powershell
$env:SAVESERV_DATA = "$env:LOCALAPPDATA\SaveServ-Server"
py server/saveserv.py
```

The desktop bundle can also run the same headless server with
`SaveServ --server` (or `SaveServ.exe --server`), honoring the same environment
variables. Desktop's **Enable server** option starts this implementation with
its data in `server-data/` beside the shared client configuration.

Default port: **8787 TCP** (HTTP) and **8787 UDP** (discovery). Allow these through
the host firewall if other devices should connect. Pair one game per client
device; other compatible games share that device's credentials. No public cloud
or desktop relay is involved.
