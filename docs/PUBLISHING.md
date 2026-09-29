# Publishing and discoverability

How an agent looking for "a way to talk to my Bambu Lab printer" ends up here.
Some of this is committed in the repo; the rest needs credentials or repository
admin, so it is listed as a checklist rather than done silently.

## Done in the repo

| Item | Where |
| --- | --- |
| Registry manifest | [`server.json`](../server.json) — validates against the official `2025-12-11` schema |
| PyPI ownership marker | `<!-- mcp-name: io.github.zhenwei2972/mhs-printer -->` in README.md |
| Package keywords | `pyproject.toml` → `keywords` |
| Hardware and client names in the README opening | model names, ports, and "MCP server" in the first screen |

The registry name is **`io.github.zhenwei2972/mhs-printer`**. With GitHub
authentication the namespace must start with `io.github.<your-username>/`, so
this only works published from the `zhenwei2972` account. The `name` in
`server.json`, the `mcp-name:` marker in the README, and the PyPI package's
README must all agree, or publishing fails validation.

## Checklist — needs your credentials

### 1. Merge to `main` — done

`main` carries the full tree as of `7113568`: drivers, slicing, stage
monitoring, both device kinds and all seven docs. GitHub search, the repository
preview, every crawler and every LLM that reads a repo look at the default
branch, so this was the prerequisite for everything below.

### 2. Repository description and topics

Neither can be set from a file, from the GitHub MCP tools, or over the REST API
from inside a Claude Code session — repository-settings writes are refused by
the agent proxy (`403 Repository settings writes are not permitted`). This is a
manual step by design.

On the repo page, click the gear next to "About":

> **Description:** MCP server for Bambu Lab 3D printers (A1 mini, A1, P1, X1) and Roborock vacuums — LAN control, camera, slicing, scheduling, print-stage monitoring. MHS-aligned.

> **Topics:** `mcp` `mcp-server` `model-context-protocol` `claude` `bambu-lab` `bambulab` `a1-mini` `3d-printing` `3d-printer` `roborock` `robot-vacuum` `mhs` `model-hardware-standard` `mqtt` `slicer` `home-automation` `python`

Topics are what GitHub's own topic pages and most third-party MCP crawlers index
on. This is the single highest-value step after the merge.

Two deliberate choices in that list. `anthropic` is left out: this implements
Anthropic's standard but is not an Anthropic project, and the tag invites that
confusion for very little reach — `claude`, `mhs` and `model-hardware-standard`
already cover the searches. `octoprint` and `klipper` are left out because the
repo does not drive them; tagging hardware it cannot control is the kind of
thing that earns an issue rather than a star.

Why this matters, measured rather than assumed: with an empty topic list and a
description that did not contain the word "MCP", a GitHub search for
`bambu mcp user:zhenwei2972` returned **zero results** — the repository did not
match its own subject even when the search was scoped to the account.

### 3. Publish to PyPI

The MCP Registry hosts metadata only, not artifacts, so the package has to exist
first:

```bash
pip install build twine
python -m build
twine upload dist/*
```

The uploaded package's README carries the `mcp-name:` marker, which is how the
registry verifies you own it. Bump `version` in **both** `pyproject.toml` and
`server.json` (top level *and* inside `packages[0]`) for every release — the
registry rejects a mismatch.

### 4. Publish to the MCP Registry

```bash
brew install mcp-publisher        # or the release binary, see the docs link below
mcp-publisher login github        # device-code flow in your browser
mcp-publisher publish             # reads ./server.json
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=mhs-printer"
```

The registry is in preview; breaking changes and data resets are possible.
Docs: <https://modelcontextprotocol.io/registry/quickstart>.

### 5. Third-party directories

These crawl GitHub topics and the official registry, so steps 1–4 usually cover
them within a few days. To submit directly:

| Directory | How |
| --- | --- |
| [PulseMCP](https://www.pulsemcp.com) | submission form |
| [Glama](https://glama.ai/mcp/servers) | crawls GitHub; claim the listing |
| [mcp.so](https://mcp.so) | submission form |
| [LobeHub](https://lobehub.com/mcp) | submission form |
| [awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers) | pull request adding one line |

### 6. Optional

* A GitHub release tagged `v0.1.0` — release feeds are themselves indexed.
* The [publishing GitHub Action](https://modelcontextprotocol.io/registry/github-actions),
  so a tag pushes to PyPI and the registry together and the versions cannot drift.
* A short demo GIF or the rendered preview image in the README. Directory
  listings that show a screenshot get noticeably more clicks.

## What people actually search

Worth keeping in the README verbatim, because these are the phrases that get
typed: *bambu lab mcp*, *bambu mcp server*, *a1 mini api*, *bambu lab api*,
*control bambu printer from claude*, *bambu lab home assistant alternative*,
*3d printer mcp server*, *bambu lab lan mode api*, *bambu printer camera stream*.

The README's opening lines already carry most of them. Avoid keyword-stuffing
beyond that: the directories rank on the description and topics, and a README
that reads like SEO spam costs more trust than the ranking is worth.
