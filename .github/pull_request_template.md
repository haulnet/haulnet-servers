<!-- One pull request per server. Read the README section "Adding a server" first. -->

## Server

- Name (derived, for review): `<Region> [<City>] #<N>`
- File: `servers/<region>/<city>-<N>.json`

## Checklist

- [ ] The server runs the official `haulnet-server` package and is reachable from the internet (UDP 27000–27002, status endpoint TCP 27080).
- [ ] The number is the smallest free number in the city (CI checks this).
- [ ] If the city is new: it is added to `data/locations.json` in region order, sorted by id, with its common
      English name and ISO country code.
- [ ] I run this server (or I am authorised by the team named in `operator`) and the GitHub account in
      `operator.github` will answer reports about it.
- [ ] I have read the operator rules in the README and accept that maintainers may remove the server.

## Notes

<!-- Hosting provider and bandwidth, anything reviewers should know. Optional. -->
