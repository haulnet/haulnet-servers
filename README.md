# Haulnet servers

The public list of community-run Haulnet servers.

[Haulnet](https://github.com/haulnet) lets R.E.P.O. players host games on servers they control instead of
Photon Cloud. When friends cannot connect to the host's PC directly, the host can create the game on a
community server listed here. The Haulnet mod downloads this list, measures the ping to every server and shows
one entry per city in the game's region menu.

Haulnet is a fan project. It is not affiliated with or endorsed by semiwork (R.E.P.O.) or Exit Games (Photon).

## The published list

After every merge to `main`, CI publishes the list to GitHub Pages at `servers.haulnet.cc`
(the old `haulnet.github.io/haulnet-servers/` addresses redirect there):

| URL | Contents |
|---|---|
| `https://servers.haulnet.cc/v1/servers.json` | the list, schema version 1 |
| `https://servers.haulnet.cc/` | the same list as a web page |
| `https://servers.haulnet.cc/schema/servers-v1.schema.json` | JSON Schema of the list |

```json
{
  "schema_version": 1,
  "updated_at": "2026-11-14T12:00:00Z",
  "source_commit": "<40 hex characters>",
  "compatibility": { "luxon": ["v1.5"] },
  "regions": [{ "id": "europe", "name": "Europe" }],
  "cities": [{ "id": "moscow", "name": "Moscow", "country": "RU", "region": "europe",
               "display_name": "Europe [Moscow]" }],
  "servers": [{
    "id": "europe-moscow-1",
    "display_name": "Europe [Moscow] #1",
    "region": "europe", "city": "moscow", "number": 1,
    "host": "msk1.example.org", "port": 27000, "status_port": 27080,
    "operator": { "name": "Example Team", "github": "example-user", "contact": "https://example.org/" }
  }]
}
```

- `regions` lists every region in display order; `cities` lists only cities that have servers.
- `servers` are ordered by region, city name and number. Use `id` to refer to a server, not `display_name`.
- Live load and version: `GET http://<host>:<status_port>/status` (IPv6 hosts in brackets) answers the
  `haulnet-server` status. A server whose `luxon` version is not in `compatibility.luxon` does not work with
  the current game.
- Version 1 may gain new optional fields; readers must ignore fields they do not know. A change that would
  break existing readers is published under a new path (`/v2/`) while `/v1/` keeps being updated.

## Live status

The page `https://servers.haulnet.cc/` also shows whether each server answers, its load, version and uptime.
The data comes from the Haulnet registry monitor ([`monitor/`](monitor/)), which asks every listed server's
status endpoint once a minute from Moscow over IPv4 and keeps the results for 90 days:

| URL | Contents |
|---|---|
| `https://monitor.haulnet.cc/v1/status.json` | state (`online`, `offline` after 3 failed checks in a row, `pending`), versions, rooms and players, uptime for 24 hours, 7 and 30 days |
| `https://monitor.haulnet.cc/v1/history/<id>.json` | daily totals of one server for the last 30 days |

The page also draws one bar per day for the last 30 days. When a listed server stays unreachable for an hour,
reports a LuxonServer version the game does not support, or runs an old `haulnet-server` release for a long time
(two weeks; two days when a minor version behind), the monitor opens an issue here that mentions the operator's
`operator.github` account, and closes it when the problem is gone.

The monitor shows whether a server answers from one place; it is not the ping of a player. It never changes
`servers.json`: a server is removed only by a pull request. The mod does not use the monitor and checks servers
itself.

## Server names

Every server is named `<Region> [<City>] #<N>`, for example `Europe [Moscow] #1` or `Asia [Tokyo] #2`.
Nobody types the name: CI builds it from the entry, so it is always consistent.

- **Region** comes from the city. Regions are fixed: Europe, Asia, North America, South America, Oceania,
  Africa, Middle East. A city belongs to the region of its geography, not of its country
  (Moscow is in Europe, Novosibirsk in Asia).
- **City** is an id from [`data/locations.json`](data/locations.json).
- **N** is the server's number in its city. A new server takes the **smallest number not used** in that
  city: if Moscow has #1 and #3, the next server is #2. Existing servers keep their number.

## Adding a server

One pull request per server. Your server must run the `haulnet-server` package and be reachable from the
internet: UDP 27000–27002 for players and TCP 27080 (the status endpoint) for the mod and for CI.

1. Fork this repository.
2. Find your city in [`data/locations.json`](data/locations.json). If it is missing, add it:
   - `name`: the common English name in plain ASCII (`Saint Petersburg`, `Sao Paulo`);
   - `id`: the name in lowercase with spaces replaced by `-` (`saint-petersburg`);
   - `country`: the ISO 3166-1 alpha-2 code (`RU`); `region`: one of the region ids;
   - keep the list grouped by region (in the order of `regions`) and sorted by `id` inside each region.
3. Pick the number: the smallest number not yet used by a file in `servers/<region>/<city>-*.json`.
4. Create `servers/<region>/<city>-<N>.json`:

   ```json
   {
     "$schema": "../../schema/server.schema.json",
     "city": "moscow",
     "number": 2,
     "host": "msk2.example.org",
     "port": 27000,
     "operator": {
       "name": "Example Team",
       "github": "example-user",
       "contact": "https://example.org/"
     }
   }
   ```

   | Field | Rule |
   |---|---|
   | `city` | city id from `data/locations.json` |
   | `number` | the number from step 3, same as in the file name |
   | `host` | public DNS name in lowercase, or a public IPv4/IPv6 address without brackets |
   | `port` | UDP port of the server's NameServer |
   | `status_port` | optional: TCP port of the status endpoint, only if you changed `STATUS_ADDRESS` (default 27080). Never LuxonServer's own web API (5088) |
   | `operator.name` | person or team shown to players, 2–48 characters, no `<` or `>` |
   | `operator.github` | GitHub account that answers reports about the server |
   | `operator.contact` | optional: an `https://` link or a `mailto:` address |

   No other fields are allowed.
5. Open a pull request and fill in the template. CI checks the entry; a maintainer reviews and merges it.

### What CI checks

- the entry and `data/locations.json` match their JSON Schemas;
- the file is `servers/<region of the city>/<city>-<number>.json`, and nothing else is in `servers/`;
- the city exists, its id is derived from its name, and the city list is in order;
- numbers are unique per city, and a new server takes the smallest free number;
- `host` is a valid DNS name or public IP address (no private, loopback, CGNAT, documentation or reserved
  ranges and names), and `host` + `port` is not listed twice;
- DNS names resolve to public addresses, and no two entries point at the same address and port;
- for every added or changed entry: `GET http://<host>:<status_port>/status` answers within 5 seconds like
  `haulnet-server` does, and its `luxon` version is listed in [`data/compatibility.json`](data/compatibility.json).
  Bring the server up before you open the pull request.

Run the same checks locally (Python 3.10+):

```bash
pip install -r tools/requirements.txt
python tools/registry.py validate --base origin/main --probe
python tools/registry.py build --out site
```

## Rules for operators

- Run the official `haulnet-server` package and keep it updated; the mod hides servers that run an
  unsupported version.
- Keep the server reachable. Servers that stay unreachable for a week may be removed; open a new pull
  request when the server is back.
- Do not inspect, record or sell players' game traffic beyond what is needed to run and protect the server.
  No ads, payments or account requirements for players.
- Answer reports about your server through the GitHub account in `operator.github`.
- Update or remove your entry by pull request when the address changes or the server shuts down.

Maintainers may remove any server that breaks these rules or harms players. Report a problem with a server
through an [issue](../../issues/new/choose).

Everything in an entry is public, including the operator name and GitHub account.

## License

[MIT](LICENSE). The list itself is published under the same license.
