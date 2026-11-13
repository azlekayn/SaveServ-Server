# SaveServ-Server

The standalone headless SaveServ server. Compatible games and SaveServ Desktop
can connect using the same discovery, server selection and PIN pairing flow.
It does not require the desktop app and the existing API/configuration is unchanged.

## Docker (existing deployment)

```sh
docker compose up -d
```

Run this from the SaveServ-Server repository root (or `server/` in a recursive
SaveServ desktop checkout). Data stays in `data/`; upgrading the application
does not replace your database or save blobs.

## Without Docker

Python 3.10+ and its standard library are sufficient:

```sh
SAVESERV_DATA="$HOME/.local/share/saveserv-server" python3 saveserv.py
```

Windows PowerShell, from the repository root:

```powershell
$env:SAVESERV_DATA = "$env:LOCALAPPDATA\SaveServ-Server"
py saveserv.py
```

The desktop bundle can also run the same headless server with
`SaveServ --server` (or `SaveServ.exe --server`), honoring the same environment
variables. Desktop's **Enable server** option starts this implementation with
its data in `server-data/` beside the shared client configuration.

Default port: **8787 TCP** (HTTP) and **8787 UDP** (discovery). Allow these through
the host firewall if other devices should connect. Pair one game per client
device; other compatible games share that device's credentials. No public cloud
or desktop relay is involved.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SAVESERV_DATA` | `/data` | SQLite database and save blobs |
| `SAVESERV_PORT` | `8787` | HTTP and UDP discovery port |
| `SAVESERV_URL` | unset | Address advertised to other devices |
| `SAVESERV_REGISTER` | `first` | First account only; `open` enables self-registration |
| `SAVESERV_KEEP` | `50` | Revisions kept per save |
| `SAVESERV_THUMBNAILS` | `1` | Set `0` to disable online cover lookups |
| `SAVESERV_STEAMGRIDDB_KEY` | unset | Optional SteamGridDB cover lookup |
| `SAVESERV_IGDB_ID`, `SAVESERV_IGDB_SECRET` | unset | Optional IGDB cover lookup |

Open the web UI to create the first account, then approve devices using their
six-digit PIN. The website manages saves, retained revisions, covers, devices,
accounts and browser sessions. Every upload keeps a revision; restoring an old
revision makes it current without discarding the newer history.

Use plain HTTP only on a trusted LAN or VPN. For Internet access, put the server
behind an HTTPS reverse proxy. Allow uploads of at least 16 MiB (nginx:
`client_max_body_size 16m;`). Set `SAVESERV_URL` to the externally reachable HTTPS
address so discovery advertises the right destination.

## Repository layout

- `saveserv.py`: standard-library-only server and discovery responder.
- `web/`: the server's save-management web UI.
- `Dockerfile`, `compose.yml`: standalone container deployment.

SaveServ Desktop and SaveServ-Plugin remain separate repositories. The desktop
includes this repository as its `server/` submodule, so both deployments run the
same server implementation. Client/server integration tests live in SaveServ's
`tests/` directory.

## License

MIT OR Apache-2.0, as in the original SaveServ repository. See `LICENSE-MIT` and
`LICENSE-APACHE`.
