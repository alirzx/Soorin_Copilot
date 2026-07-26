# Soorin Copilot Deployment

## Configuration ownership

- `app/.env`: application behavior, credentials, provider settings, and portable repository-relative application paths.
- `compose.env`: image tag, restart policy, host bind addresses and ports, UID/GID, host data root, and host Hugging Face cache root.

Create both private files from their committed examples. Never commit them.

## Persistent host data

The configured `SOORIN_DATA_HOST_PATH` must already contain:

```text
raw/topology_raw.json
processed/topology_graph.pkl
processed/topology_stats.json
qdrant-local/meta.json
runtime/
```

Compose bind-mounts this root at `/workspace/data`. API access is read/write so
Graph refresh and runtime storage remain functional. UI access is read-only.
Embedded Qdrant is opened only by API. `create_host_path: false` prevents a bad
path from silently masking real data with an empty directory.

The BGE cache is mounted read-only from `SOORIN_HF_CACHE_HOST_PATH`. Preflight
requires revision `a5beb1e3e68b9ab74eb54cfd186867f64f240e1a` and validates a hidden
size of 768. No model files, Graph artifacts, or Qdrant data are baked into the
image.

The original SOC source corpus is not required for normal retrieval. It is used
only by the separate indexing maintenance flow and is not mounted into the API
or UI containers.

## Local deployment

Use repository-local data and your existing Hugging Face cache in
`compose.env`. Keep host bindings on `127.0.0.1` unless LAN exposure is
intentional. Ensure `APP_UID` and `APP_GID` can traverse and write the data root
and can read the model cache.

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

## Remote image-import deployment

1. Set the image tag to `dev-<git-short-sha>` before build.
2. After a validated build, run `make export` and transfer the tar and checksum.
3. On the server, verify the checksum and run `docker load -i <image>.tar`.
4. Create `app/.env` and `compose.env` from the same committed examples.
5. Set `SOORIN_DATA_HOST_PATH=/srv/soorin-copilot/data` and
   `SOORIN_HF_CACHE_HOST_PATH=/srv/soorin-copilot/huggingface` (or equivalent).
6. Match `APP_UID`/`APP_GID` to host ownership and copy the validated data/cache
   trees before running `make preflight`.
7. Use `0.0.0.0` bind addresses only when LAN access is intentionally required.
8. Run `make up` only after preflight passes.

Compose uses `pull_policy: never`; the exact tagged image must already be
present on the destination host. Dockerfile, Compose, Makefile, and both
configuration examples are identical between local and server deployments.
