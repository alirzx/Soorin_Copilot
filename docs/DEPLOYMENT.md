# Soorin Copilot Deployment

## Configuration

The repository-root `.env` is the single private configuration file consumed by
the application and the current Compose file. `.env.example` is the tracked,
secret-free schema. Create the private file once and configure its credentials
locally; do not create `app/.env` or `compose.env`.

```bash
cp .env.example .env
```

Compose reads the root `.env` for image tags, restart policy, host bindings,
Copilot authentication, observability profile values, and application settings.
The API and UI service definitions provide their container-specific paths and
runtime overrides directly. The root `.env` remains private and ignored.

## Persistent Data

The current Compose file bind-mounts the repository `data/` directory at
`/workspace/data`; the API has read/write access and the UI has read-only access.
That directory must already contain the validated Graph artifacts and, when
local Qdrant is enabled, the configured Qdrant data. The Hugging Face model cache
is mounted from the repository `huggingface/` directory and is read-only inside
the containers. No model, Graph artifact, Qdrant data, or SOC source corpus is
baked into the image.

The original SOC corpus is external to normal runtime retrieval. It is used by
the separate indexing maintenance flow and is not scanned during application
import or startup.

## Neo4j Projection Baseline

The graph projection uses Neo4j Community Edition 2026.07.1 as a single
instance with the persistent `neo4j-data` Docker volume. Community creates the
supported record-aligned store format. Product remains the topology authority,
so the projection can be rebuilt from a validated Product snapshot. Retain the
volume as operational state and use host-level/offline volume snapshots for
backup; this does not replace broader disaster-recovery planning.

Enterprise licensing, clustering, online backup, and the `block` store format
are not runtime requirements. They are optional future upgrade concerns, not
deployment prerequisites for Copilot.

## Local Compose Workflow

Keep host bindings on `127.0.0.1` unless LAN exposure is intentional. Validate
the root environment and current data/cache prerequisites before starting:

```bash
make preflight
make test-local
make build
make inspect-image
make inspect-size
make up
make health
```

`make down` removes containers and the network but does not remove host data.
The optional observability profile is documented in
[`docs/OBSERVABILITY.md`](OBSERVABILITY.md).

## Remote Image Import

1. Set `SOORIN_IMAGE_TAG` to the validated release tag before building.
2. Build and export the image with the existing Makefile workflow.
3. Transfer the image archive and checksum, then verify and load them on the
   destination host.
4. Create the root `.env` from the matching `.env.example` and configure the
   destination's private credentials and bind addresses.
5. Copy the validated `data/` and `huggingface/` trees before `make preflight`.
6. Use `0.0.0.0` bind addresses only when remote access is intentionally required.
7. Run `make up` only after preflight passes.

Compose uses `pull_policy: never`; the exact tagged image must already exist on
the destination host. Dockerfile, Compose, and Makefile behavior are unchanged
by the environment normalization documented here.
