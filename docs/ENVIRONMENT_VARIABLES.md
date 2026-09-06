# Soorin Copilot Environment Variables

The repository-root `.env` is the private runtime configuration for Soorin Copilot and must remain ignored by Git. `.env.example` is the tracked configuration schema and safe reference template; the two files should contain the same current variable key set, while their values are intentionally different because `.env` contains machine-specific configuration and secrets. Process environment variables have precedence over dotenv-loaded values. The current settings loader reads the repository-root `.env` first and temporarily supports a non-merging fallback to legacy `app/.env` for migration compatibility.

Secrets such as API keys, passwords, Product tokens, captcha bypass values, and private service credentials must never be committed or printed in logs. Numeric budgets, timeouts, paths, model names, collection names, and feature flags are configuration rather than secrets unless an individual deployment treats them as sensitive. Disabled experimental or development-only features should normally remain disabled until their corresponding workflow is deliberately being tested.

---

## Deployment

### `SOORIN_IMAGE_TAG`

`SOORIN_IMAGE_TAG` selects the Docker image tag used by the deployment configuration, for example a release-specific Copilot image tag. Changing it tells Compose or the deployment workflow which previously built image version should be started; it does not change Python application behavior by itself. A tag must correspond to an image that actually exists locally or in the configured registry.

### `SOORIN_RESTART_POLICY`

`SOORIN_RESTART_POLICY` defines the container restart behavior consumed by the Docker/Compose deployment. Typical Docker policies include `no`, `always`, `on-failure`, and `unless-stopped`; the current deployment commonly uses `unless-stopped`, which restarts Copilot after failures or host restarts unless an operator explicitly stops it. Exact accepted values ultimately follow the current Compose/Docker contract. `(double-check against current deployment code)`

### `SOORIN_API_BIND_IP`

`SOORIN_API_BIND_IP` controls the host interface on which the containerized API port is published. `0.0.0.0` exposes the published port on all host interfaces, while `127.0.0.1` limits it to the local machine; a specific interface address restricts exposure to that address. This setting affects network accessibility and therefore has direct security implications, but it does not change FastAPI's internal application logic.

### `SOORIN_API_HOST_PORT`

`SOORIN_API_HOST_PORT` is the host TCP port mapped to the Copilot API service in deployment mode. Changing the number changes only where clients reach the API, for example `6998`; higher or lower port numbers do not increase capacity, latency, context size, or concurrency. The selected port must be free on the host and must match any Product/frontend configuration that calls Copilot.

### `SOORIN_UI_BIND_IP`

`SOORIN_UI_BIND_IP` controls which host interface exposes the Streamlit UI port in deployment mode. `0.0.0.0` makes the published UI reachable on all host interfaces, while `127.0.0.1` keeps it local to the machine. This is an exposure and access-control boundary rather than a performance setting.

### `SOORIN_UI_HOST_PORT`

`SOORIN_UI_HOST_PORT` is the host TCP port published for the Streamlit UI, commonly mapped to the container's Streamlit port. Changing it changes the URL used to reach the UI but has no effect on model quality, memory behavior, or throughput. The port must not conflict with another host service.

### `SOORIN_API_TIMEOUT_SECONDS`

`SOORIN_API_TIMEOUT_SECONDS` controls the client-side timeout used by the Streamlit/frontend side when waiting for Copilot API requests. A larger value allows long investigations or slow model calls to finish before the client gives up, while a smaller value fails the UI request sooner and frees the caller earlier; it does not override lower-level Router, Planner, Product, executor, or Synthesizer timeouts.

---

## Runtime, Metrics, and Observability

### `LOG_LEVEL`

`LOG_LEVEL` sets the minimum severity emitted by the main application logger. Supported levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`: `DEBUG` provides the most diagnostic detail and highest log volume, `INFO` records normal operational lifecycle events, `WARNING` focuses on unexpected but recoverable situations, `ERROR` records failures, and `CRITICAL` is reserved for severe failures. Raising the level reduces log volume but also removes troubleshooting detail.

### `SOORIN_LOG_FORMAT`

`SOORIN_LOG_FORMAT` controls terminal/stdout log representation. Current supported modes are `console` and `json`: `console` produces human-readable operational output suitable for local development and direct Docker log inspection, while `json` produces structured machine-oriented records suitable for log collection and automated parsing. This changes formatting only, not which workflow events occur.

### `SOORIN_LOG_COLOR`

`SOORIN_LOG_COLOR` controls ANSI color use in human-readable terminal logs. Supported values are `auto`, `always`, and `never`: `auto` enables color only when output is attached to a compatible terminal, `always` forces color, and `never` disables it. Redirected/file/JSON output should normally avoid color so stored logs do not contain escape sequences.

### `SOORIN_LOG_FILE_ENABLED`

`SOORIN_LOG_FILE_ENABLED` enables or disables the application's rotating local log file. `true` writes structured application and human-trace output to the configured log file in addition to terminal logging; `false` leaves terminal/stdout as the only normal runtime log destination. Enabling it improves local forensic/debugging persistence but consumes disk according to rotation settings.

### `SOORIN_LOG_FILE_LEVEL`

`SOORIN_LOG_FILE_LEVEL` sets the minimum severity written to the rotating file handler independently of the general terminal level. Supported severity values follow `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`; lower thresholds retain more troubleshooting information and consume more storage, while higher thresholds reduce retained detail and disk usage.

### `SOORIN_LOG_FILE_PATH`

`SOORIN_LOG_FILE_PATH` is the filesystem destination for the rotating application log, normally under the persistent runtime data tree such as `data/runtime/logs/`. Relative paths are interpreted in the application's runtime working-directory context, while container deployments may override them to paths under `/workspace/data`. The target directory must be writable by the API process.

### `SOORIN_LOG_FILE_MAX_BYTES`

`SOORIN_LOG_FILE_MAX_BYTES` sets the maximum size of the active rotating log file before rotation occurs. Increasing it keeps a larger continuous log segment but consumes more disk per file; decreasing it rotates more frequently and reduces the amount of contiguous history in each file. Total retained storage is approximately this value multiplied by the configured backup count, plus the active file.

### `SOORIN_LOG_FILE_BACKUP_COUNT`

`SOORIN_LOG_FILE_BACKUP_COUNT` controls how many rotated log files are retained in addition to the active log. A larger value provides a longer local troubleshooting history at greater disk cost; a smaller value reduces disk usage but removes historical logs sooner. This affects file retention only and does not change stdout/container logging.

### `SOORIN_METRICS_ENABLED`

`SOORIN_METRICS_ENABLED` controls whether the authenticated Prometheus metrics endpoint is registered and emits Copilot operational metrics. `true` enables metrics collection for request, workflow, provider, latency, token, and related low-cardinality measurements; `false` disables the endpoint/metrics path. Metrics are observability data and do not participate in evidence authority or model reasoning.

### `SOORIN_METRICS_PATH`

`SOORIN_METRICS_PATH` defines the HTTP API route where Prometheus-formatted metrics are exposed, normally `/metrics`. It is an HTTP route, not a filesystem path, and should remain a static absolute API path beginning with `/`. Changing it requires the Prometheus scrape configuration to use the same route; the endpoint remains behind the Copilot API authentication boundary according to the current architecture.

### `SOORIN_OBSERVABILITY_GRAFANA_BIND_IP`

`SOORIN_OBSERVABILITY_GRAFANA_BIND_IP` controls the host interface on which the optional Grafana service is published. `127.0.0.1` keeps Grafana private to the local host and is the safer development default; `0.0.0.0` exposes it on all interfaces and should only be used when network access is intentionally controlled elsewhere.

### `SOORIN_OBSERVABILITY_GRAFANA_PORT`

`SOORIN_OBSERVABILITY_GRAFANA_PORT` selects the host TCP port used to access Grafana, commonly `3000`. Changing the port only changes the access URL and does not alter metric retention, dashboard contents, or Copilot execution. The port must be available on the host.

### `SOORIN_OBSERVABILITY_PROMETHEUS_BIND_IP`

`SOORIN_OBSERVABILITY_PROMETHEUS_BIND_IP` controls which host interface exposes the optional Prometheus service. Binding to `127.0.0.1` keeps the metrics database private to the machine, while broader bindings expose it to the corresponding network and should be used only with deliberate network controls.

### `SOORIN_OBSERVABILITY_PROMETHEUS_PORT`

`SOORIN_OBSERVABILITY_PROMETHEUS_PORT` selects the host TCP port for Prometheus, commonly `9090`. It changes only where the Prometheus UI/API is reached; port magnitude has no performance meaning. The value must not collide with another local service.

### `SOORIN_OBSERVABILITY_LOKI_BIND_IP`

`SOORIN_OBSERVABILITY_LOKI_BIND_IP` selects the host interface used by the optional Loki log backend. `127.0.0.1` keeps Loki local to the development machine, while a broader bind makes its API remotely reachable. Because logs can contain operational metadata, exposure should remain intentionally constrained.

### `SOORIN_OBSERVABILITY_LOKI_PORT`

`SOORIN_OBSERVABILITY_LOKI_PORT` is the host TCP port for the optional Loki service, commonly `3100`. Changing it changes service addressing only and requires dependent Alloy/Grafana configuration to remain consistent; higher port numbers do not provide greater capacity.

### `SOORIN_OBSERVABILITY_RETENTION`

`SOORIN_OBSERVABILITY_RETENTION` configures Prometheus TSDB and Loki retention using Prometheus-compatible duration syntax, for example `168h`. A longer retention window preserves more historical metrics/logs and consumes more storage; a shorter window reduces disk use but limits retrospective investigation. The Compose profile passes the same value to both backends.

### `SOORIN_OBSERVABILITY_ENVIRONMENT`

`SOORIN_OBSERVABILITY_ENVIRONMENT` provides the environment label attached to scraped API metrics and Alloy-collected log streams, such as `development`, so dashboard filtering is consistent across Prometheus and Loki. Use a stable, low-cardinality deployment name. The value does not change evidence retrieval or workflow policy.

### `SOORIN_GRAFANA_ADMIN_USER`

`SOORIN_GRAFANA_ADMIN_USER` defines the bootstrap administrator username for the optional Grafana instance. It is identity/configuration material rather than a numeric tuning parameter; changing it changes the administrator login name. In shared environments it should not rely on a universally known default account name.

### `SOORIN_GRAFANA_ADMIN_PASSWORD`

`SOORIN_GRAFANA_ADMIN_PASSWORD` supplies the bootstrap administrator password for Grafana. It is a secret and must remain only in the private `.env` or a secure deployment secret mechanism, never in Git or logs. Placeholder values such as `CHANGE_ME` must be replaced before any shared deployment.

---

## API and UI

### `API_HOST`

`API_HOST` is the bind address used when the Python API is launched directly through `app/run.py`, independently of Docker host-port publishing. `0.0.0.0` allows the Uvicorn process to listen on all local interfaces, while `127.0.0.1` restricts it to loopback. It controls native process exposure, whereas `SOORIN_API_BIND_IP` belongs to container/host publishing.

### `API_PORT`

`API_PORT` is the TCP port on which the native FastAPI/Uvicorn process listens when launched directly. Changing it changes the API address only; it does not affect request budgets or concurrency. Any UI `SOORIN_API_BASE_URL`, health checks, or external clients must point to the same port.

### `API_RELOAD`

`API_RELOAD` controls Uvicorn development auto-reload behavior. `true` watches source files and restarts the development process when code changes, which is useful for local coding but adds watcher/restart overhead; `false` runs a stable process and is the appropriate behavior for normal server execution.

### `STREAMLIT_SERVER_PORT`

`STREAMLIT_SERVER_PORT` selects the TCP port used when Streamlit is launched natively through the project runner. Changing it changes the local UI address only. This should be distinguished from `SOORIN_UI_HOST_PORT`, which controls the externally published host port in container deployment.

### `SOORIN_API_BASE_URL`

`SOORIN_API_BASE_URL` is the base URL used by the Streamlit/frontend client to call the Copilot API, for example a local or deployed `http://host:port` address. It must resolve to the running FastAPI service and should not include credentials. An incorrect value causes UI/API connectivity failures without affecting the API process itself.

### `SOORIN_COPILOT_API_KEY`

`SOORIN_COPILOT_API_KEY` is the shared Copilot API credential used to protect non-public API routes. The API currently accepts it through the dedicated `Soorin_copilot_api_key` header or compatible Bearer authentication according to the dual-header contract, while `/health` remains public. It is a secret and must never be committed, displayed in documentation, or logged.

---

## LLM Shared Transport

### `SOORIN_LLM_ENABLED`

`SOORIN_LLM_ENABLED` globally enables or disables model-backed Copilot behavior. `true` initializes and validates configured Router/Planner/Synthesizer roles as required by the workflow; `false` disables the model layer and leaves only paths that can safely operate without LLM generation. Normal Copilot operation requires this to be enabled.

### `SOORIN_LLM_PROVIDER`

`SOORIN_LLM_PROVIDER` selects the provider implementation used by the provider-neutral LLM client; the current deployment uses `arvan`. It is a provider identifier, not a boolean. Only provider names implemented by the current code are valid, and changing it can alter transport/authentication behavior. `(double-check the exact current accepted provider enum in settings.py)`

### `SOORIN_LLM_MAX_TRANSIENT_RETRIES`

`SOORIN_LLM_MAX_TRANSIENT_RETRIES` limits generic retries for eligible transient final-model/provider failures. Increasing it can improve resilience to short network/provider errors but increases worst-case latency and duplicate provider requests; decreasing it fails faster. Router and Planner have stricter retry semantics and are not simply subjected to unrestricted generic retries.

### `SOORIN_LLM_RETRY_BASE_DELAY_SECONDS`

`SOORIN_LLM_RETRY_BASE_DELAY_SECONDS` defines the initial backoff delay between eligible transient LLM retries. A larger delay reduces retry pressure on a struggling provider but increases recovery latency; a smaller value retries sooner but can produce tighter retry bursts.

### `SOORIN_LLM_RETRY_MAX_DELAY_SECONDS`

`SOORIN_LLM_RETRY_MAX_DELAY_SECONDS` caps how large the retry backoff delay may become. Increasing it allows more conservative spacing during repeated transient failures at the cost of longer worst-case requests; decreasing it bounds waiting more aggressively.

### `SOORIN_LLM_EXPOSE_REASONING`

`SOORIN_LLM_EXPOSE_REASONING` controls whether provider-supplied reasoning content may be exposed by the application. `false` keeps hidden/provider reasoning private, which is the normal safe setting; `true` permits exposure only where current transport/application code explicitly supports it. This flag does not cause a model to reason more deeply by itself and should remain disabled for normal operation.

### `SOORIN_LLM_LOG_RAW_RESPONSE`

`SOORIN_LLM_LOG_RAW_RESPONSE` controls whether complete upstream model responses may be written to logs. `false` keeps logging limited to safe metadata such as model, latency, usage, status, and output size; `true` can substantially increase log volume and may expose user or model content, so it should normally remain disabled outside tightly controlled debugging.

### `SOORIN_LLM_CONTEXT_WINDOW_TOKENS`

`SOORIN_LLM_CONTEXT_WINDOW_TOKENS` is the configured total context-window budget used by the application's context guard for the selected LLM deployment. A larger value allows more combined prompt, evidence, history, and reserved output only if the actual provider/model supports that window; setting it above the real provider limit risks rejected requests. It is a capacity value, not a secret.

### `SOORIN_LLM_RESERVED_OUTPUT_TOKENS`

`SOORIN_LLM_RESERVED_OUTPUT_TOKENS` is the global upper reservation used when budgeting space for generated output. The current workflow can select smaller dynamic reservations for brief or standard responses, while this value acts as an upper configuration bound. Increasing it leaves less room for input evidence; decreasing it permits more input but can constrain deep responses. It is not a secret.

### `SOORIN_LLM_CONTEXT_SAFETY_MARGIN_TOKENS`

`SOORIN_LLM_CONTEXT_SAFETY_MARGIN_TOKENS` reserves additional unused space between estimated input/output usage and the configured model context limit. A larger margin reduces context-overflow risk but sacrifices usable context; a smaller margin increases utilization but leaves less protection against token-estimation error.

### `SOORIN_LLM_TOKEN_ESTIMATE_MULTIPLIER`

`SOORIN_LLM_TOKEN_ESTIMATE_MULTIPLIER` calibrates the application's approximate token estimate upward before enforcing context budgets. Values above `1.0` make budgeting more conservative, protecting against underestimation but reducing usable context; values closer to `1.0` permit more evidence/history but increase overflow risk when the character-based estimator is inaccurate. It does not affect actual provider tokenization.

### `SOORIN_LLM_AUTH_SCHEME`

`SOORIN_LLM_AUTH_SCHEME` defines the authentication scheme prepended to the configured role API key when the OpenAI-compatible provider request is built, with the current Arvan deployment using `apikey`. Changing it changes the `Authorization` header format and must match the upstream gateway contract. `(double-check exact accepted values in current provider validation)`

### `SOORIN_LLM_CHAT_PATH`

`SOORIN_LLM_CHAT_PATH` is the HTTP route appended to each role's base URL for OpenAI-compatible chat requests, currently `/chat/completions`. It is an HTTP API path, not a filesystem path. Changing it is appropriate only when the provider exposes a compatible endpoint at a different route.

### `SOORIN_LLM_CONNECT_TIMEOUT_SECONDS`

`SOORIN_LLM_CONNECT_TIMEOUT_SECONDS` limits how long an LLM transport waits to establish the network connection to the configured provider before failing the attempt. Increasing it tolerates slower connection establishment but extends failure detection; decreasing it fails unreachable providers faster. It does not limit the full generation duration, which is controlled by the role-specific read timeout.

---

## Router

### `SOORIN_ROUTER_BASE_URL`

`SOORIN_ROUTER_BASE_URL` specifies the OpenAI-compatible provider base URL used by the semantic Router role. The Router sends its structured classification request to this base URL plus `SOORIN_LLM_CHAT_PATH`; an empty or invalid URL prevents the role from becoming ready when LLM routing is enabled. The URL may be operationally sensitive even though it is not normally a credential.

### `SOORIN_ROUTER_MODEL`

`SOORIN_ROUTER_MODEL` identifies the upstream model used for semantic routing, currently configured privately as the approved Router model. It is a model/deployment identifier, not a boolean or enum unless the provider imposes one. Changing it can affect routing accuracy, latency, structured-output reliability, and token usage.

### `SOORIN_ROUTER_API_KEY`

`SOORIN_ROUTER_API_KEY` is the private provider credential used for Router requests. It must remain in private runtime configuration and must never be committed or logged. It can be the same credential as another role only if the upstream provider deployment deliberately uses shared credentials.

### `SOORIN_ROUTER_TIMEOUT_SECONDS`

`SOORIN_ROUTER_TIMEOUT_SECONDS` is the read timeout for the semantic Router request. Increasing it gives a slow Router more time to return valid structured output but increases routing latency when the provider stalls; decreasing it causes faster deterministic fallback when the model does not respond in time.

### `SOORIN_ROUTER_MAX_TOKENS`

`SOORIN_ROUTER_MAX_TOKENS` is the maximum completion budget for the normal Router structured-output call, including provider-accounted reasoning where applicable. Increasing it reduces the risk that structured JSON is cut off but can increase latency and output cost; decreasing it encourages faster bounded routing but can produce `finish_reason=length` or missing JSON if the model spends too much of the completion budget.

### `SOORIN_ROUTER_RETRY_MAX_TOKENS`

`SOORIN_ROUTER_RETRY_MAX_TOKENS` is the larger completion budget used by the Router's dedicated repair/retry path when enabled. It should normally be greater than or equal to the normal Router budget so malformed or incomplete structured output has enough room to recover; increasing it affects only that bounded retry path, not normal request context.

### `SOORIN_ROUTER_TEMPERATURE`

`SOORIN_ROUTER_TEMPERATURE` configures Router sampling temperature when the selected provider/model explicitly supports sending temperature. Lower values make classification more deterministic, which is desirable for routing; higher values increase output variability and are generally undesirable for strict JSON policy decisions. The value is omitted entirely when `SOORIN_ROUTER_SUPPORTS_TEMPERATURE=false`.

### `SOORIN_ROUTER_TOP_P`

`SOORIN_ROUTER_TOP_P` configures nucleus sampling for Router calls only when the provider supports the parameter. Lower values constrain sampling to a narrower probability mass and generally increase determinism; higher values allow more variation. It is omitted from the request when `SOORIN_ROUTER_SUPPORTS_TOP_P=false`.

### `SOORIN_ROUTER_SUPPORTS_TEMPERATURE`

`SOORIN_ROUTER_SUPPORTS_TEMPERATURE` tells the transport whether the selected Router endpoint accepts the `temperature` request field. `true` includes the configured Router temperature; `false` omits the field completely, which is necessary for providers/models that reject or ignore sampling controls in their current mode. It does not itself change temperature.

### `SOORIN_ROUTER_SUPPORTS_TOP_P`

`SOORIN_ROUTER_SUPPORTS_TOP_P` tells the transport whether the Router endpoint accepts `top_p`. `true` sends the configured Router `top_p`; `false` omits it. This compatibility flag prevents unsupported provider parameters from breaking otherwise valid Router calls.

### `SOORIN_INTENT_ROUTER_ENABLED`

`SOORIN_INTENT_ROUTER_ENABLED` controls whether the semantic LLM Router is the normal routing source. `true` performs model-based semantic classification followed by strict deterministic validation and normalization; `false` bypasses semantic routing and relies on deterministic fallback behavior. Keeping it enabled is required to evaluate the intended current architecture.

### `SOORIN_INTENT_ROUTER_SYSTEM_PROMPT_PATH`

`SOORIN_INTENT_ROUTER_SYSTEM_PROMPT_PATH` points to the filesystem Markdown file containing the semantic Router system prompt, normally under `app/prompts/`. The file is loaded at application initialization; changing the path changes routing instructions without changing Router code. A missing or unreadable file causes the application's configured fallback/loading behavior rather than providing valid new instructions.

### `SOORIN_INTENT_ROUTER_MIN_CONFIDENCE`

`SOORIN_INTENT_ROUTER_MIN_CONFIDENCE` is the minimum accepted semantic Router confidence before deterministic policy considers the result sufficiently reliable. Raising the threshold makes acceptance stricter and can increase fallback/normalization frequency; lowering it accepts more uncertain model classifications and can reduce fallback at the cost of weaker routing confidence. The meaningful range is normally `0.0` to `1.0`.

### `SOORIN_INTENT_ROUTER_RETRY_ENABLED`

`SOORIN_INTENT_ROUTER_RETRY_ENABLED` enables the Router's bounded dedicated repair/retry behavior for invalid structured responses. `true` permits the configured single recovery path where current code supports it; `false` moves directly to deterministic fallback after an invalid/failed semantic result. It does not create an unrestricted routing loop.

---

## Planner and Agent Runtime

### `SOORIN_PLANNER_BASE_URL`

`SOORIN_PLANNER_BASE_URL` specifies the OpenAI-compatible provider base URL used by the bounded Planner role. It is combined with the shared chat path when a multi-step TaskSpec requires planning. Invalid or missing configuration prevents Planner readiness when Planner mode is enabled.

### `SOORIN_PLANNER_MODEL`

`SOORIN_PLANNER_MODEL` identifies the model used to convert a validated TaskSpec into a bounded structured retrieval plan. Changing the model can affect JSON/schema adherence, latency, planning quality, and completion usage, but deterministic `PlanValidator` remains authoritative regardless of model choice.

### `SOORIN_PLANNER_API_KEY`

`SOORIN_PLANNER_API_KEY` is the private provider credential used for Planner calls. It must stay in `.env` or a secure deployment secret mechanism and must never appear in repository history or logs.

### `SOORIN_PLANNER_TIMEOUT_SECONDS`

`SOORIN_PLANNER_TIMEOUT_SECONDS` limits how long the application waits for the Planner model response. A larger value tolerates slow complex planning but can materially increase request latency when the provider stalls; a smaller value reaches deterministic fallback sooner.

### `SOORIN_PLANNER_MAX_TOKENS`

`SOORIN_PLANNER_MAX_TOKENS` is the completion budget for the normal Planner JSON proposal. Increasing it reduces truncation risk for multi-step plans but raises worst-case latency/cost; decreasing it keeps planning compact but may cause `finish_reason=length` before valid JSON is emitted, especially with models that spend completion budget on internal reasoning.

### `SOORIN_PLANNER_RETRY_MAX_TOKENS`

`SOORIN_PLANNER_RETRY_MAX_TOKENS` is the larger completion budget reserved for the Planner repair configuration where the current implementation uses a repair-purpose request. It should be sufficient for a complete schema-valid plan but remains bounded; increasing it does not expand the six-call application plan limit.

### `SOORIN_PLANNER_TEMPERATURE`

`SOORIN_PLANNER_TEMPERATURE` controls Planner sampling only when the configured model/provider supports sending temperature. Lower values favor repeatable structured plans, while higher values increase variability and are generally inappropriate for deterministic retrieval compilation. When support is disabled, this field is not sent.

### `SOORIN_PLANNER_TOP_P`

`SOORIN_PLANNER_TOP_P` controls Planner nucleus sampling only when supported by the provider. Lower values constrain variation and higher values broaden sampling; strict retrieval planning normally benefits from conservative settings. The parameter is omitted when Planner top-p support is disabled.

### `SOORIN_PLANNER_SUPPORTS_TEMPERATURE`

`SOORIN_PLANNER_SUPPORTS_TEMPERATURE` is a transport compatibility flag. `true` includes `SOORIN_PLANNER_TEMPERATURE` in Planner requests; `false` omits the field entirely so endpoints that do not accept it can still be used safely.

### `SOORIN_PLANNER_SUPPORTS_TOP_P`

`SOORIN_PLANNER_SUPPORTS_TOP_P` determines whether Planner requests include the configured `top_p`. `true` sends the parameter; `false` suppresses it. This changes request compatibility rather than the application's deterministic planning bounds.

### `SOORIN_PLANNER_ENABLED`

`SOORIN_PLANNER_ENABLED` controls whether validated multi-step tasks may call the LLM Planner. `true` allows one bounded Planner proposal for tasks classified as requiring planning, while direct/simple tasks still skip it; `false` uses deterministic plan compilation/fallback only. The Planner never becomes an unrestricted tool-calling agent.

### `SOORIN_PLANNER_REPAIR_ENABLED`

`SOORIN_PLANNER_REPAIR_ENABLED` controls the application's bounded repair behavior for structurally defective Planner output. `true` permits the currently implemented mechanical/repair path before deterministic fallback, while `false` rejects invalid plans and falls back immediately. Repair does not permit changes to entity authority, capability permissions, or hard limits.

### `SOORIN_PLANNER_SYSTEM_PROMPT_PATH`

`SOORIN_PLANNER_SYSTEM_PROMPT_PATH` is the filesystem path to the Planner system prompt loaded by the bounded Planner implementation, normally under `app/prompts/`. Changing it changes the Planner's planning instructions but does not weaken `PlanValidator`; a missing/unreadable prompt causes the configured safe loading/fallback behavior.

### `SOORIN_AGENT_MAX_SUPPLEMENTAL_RETRIEVALS`

`SOORIN_AGENT_MAX_SUPPLEMENTAL_RETRIEVALS` configures how many reviewer-approved supplemental evidence retrievals may be executed after the initial plan. Current architecture hard-caps this behavior at one; lowering it to zero disables supplemental retrieval, while attempting to configure a larger value must not create an autonomous retrieval loop because application validation retains the hard bound.

### `SOORIN_AGENT_MAX_CAPABILITY_CALLS`

`SOORIN_AGENT_MAX_CAPABILITY_CALLS` sets the maximum registered capability calls allowed in an execution plan/request, with the current architecture hard-capped at six. A larger configured allowance can support broader investigations only up to that enforced hard bound; a smaller value reduces cost and latency but can prevent broad multi-source tasks from satisfying all requested evidence requirements.

### `SOORIN_AGENT_MAX_ENTITIES`

`SOORIN_AGENT_MAX_ENTITIES` limits how many resolved target entities a validated investigation may operate on, with the current architecture hard-capped at two. Lowering it prevents pair comparison/path workflows; raising it above the hard limit does not authorize larger multi-entity investigations.

### `SOORIN_AGENT_MAX_GRAPH_DEPTH`

`SOORIN_AGENT_MAX_GRAPH_DEPTH` limits graph traversal depth that a validated plan may request, with the current architecture capped at two hops. Lower values reduce graph size/context pressure but prevent deeper neighborhood requests; larger configured values cannot override the deterministic hard limit.

### `SOORIN_AGENT_EXECUTOR_MAX_CONCURRENCY`

`SOORIN_AGENT_EXECUTOR_MAX_CONCURRENCY` limits how many independent capability steps the executor may run concurrently, currently bounded to a maximum of four. Increasing concurrency can reduce wall-clock time for independent Graph/Knowledge work but increases simultaneous resource/network pressure; Product Profile and Detection remain serialized by their shared Product-client concurrency policy.

### `SOORIN_AGENT_REQUEST_TIMEOUT_SECONDS`

`SOORIN_AGENT_REQUEST_TIMEOUT_SECONDS` bounds total capability-execution work for an investigation request. A larger value tolerates slower provider combinations but holds executor resources longer; a smaller value fails long retrieval phases earlier. Already-running synchronous provider threads cannot be forcibly terminated by this setting, so native Product/Qdrant transport timeouts remain important.

---

## Synthesizer

### `SOORIN_SYNTHESIZER_BASE_URL`

`SOORIN_SYNTHESIZER_BASE_URL` specifies the OpenAI-compatible upstream base URL for the final Synthesizer role. It is combined with the shared chat path when the final evidence-reviewed context is sent for answer generation. Changing it changes the upstream endpoint, not context policy.

### `SOORIN_SYNTHESIZER_MODEL`

`SOORIN_SYNTHESIZER_MODEL` identifies the model used for final evidence-grounded response generation. Model choice directly affects analytical quality, latency, style, context support, and token consumption; the model remains subordinate to evidence review, context budgeting, and operational-source authority.

### `SOORIN_SYNTHESIZER_API_KEY`

`SOORIN_SYNTHESIZER_API_KEY` is the private provider credential for final synthesis requests. It must remain outside Git and must never be printed in logs, documentation, trace output, or user-visible responses.

### `SOORIN_SYNTHESIZER_TIMEOUT_SECONDS`

`SOORIN_SYNTHESIZER_TIMEOUT_SECONDS` is the read timeout for the final model generation. Increasing it permits slow deep reports to finish but increases worst-case user wait time when the provider stalls; decreasing it fails final generation sooner and may cause more deterministic fallback/limited outcomes under provider slowness.

### `SOORIN_SYNTHESIZER_MAX_TOKENS`

`SOORIN_SYNTHESIZER_MAX_TOKENS` is the absolute completion ceiling for normal final synthesis. The normal request budget is selected from the detail-level output reservation below and cannot exceed this role ceiling. Larger values support longer reports at greater latency/cost and less potential context headroom.

### `SOORIN_SYNTHESIZER_BRIEF_OUTPUT_TOKENS`

`SOORIN_SYNTHESIZER_BRIEF_OUTPUT_TOKENS` is the normal requested output reservation for brief Synthesizer responses. It defaults to `1536`, remains capped by `SOORIN_SYNTHESIZER_MAX_TOKENS`, and is checked by the configured model-context guard.

### `SOORIN_SYNTHESIZER_STANDARD_OUTPUT_TOKENS`

`SOORIN_SYNTHESIZER_STANDARD_OUTPUT_TOKENS` is the normal requested output reservation for standard Synthesizer responses. It defaults to `4096`, remains capped by `SOORIN_SYNTHESIZER_MAX_TOKENS`, and is checked by the configured model-context guard.

### `SOORIN_SYNTHESIZER_DEEP_OUTPUT_TOKENS`

`SOORIN_SYNTHESIZER_DEEP_OUTPUT_TOKENS` is the normal requested output reservation for deep and report Synthesizer responses. It defaults to `6144`, remains capped by `SOORIN_SYNTHESIZER_MAX_TOKENS`, and is checked by the configured model-context guard.

### `SOORIN_SYNTHESIZER_RETRY_MAX_TOKENS`

`SOORIN_SYNTHESIZER_RETRY_MAX_TOKENS` is the completion ceiling for the one eligible final-synthesis recovery attempt. Recovery uses this setting instead of the normal detail-level reservation, while still being reduced when the recovery prompt would otherwise exceed the configured model context window. Increasing it does not expand the model context window and can increase worst-case recovery cost.

### `SOORIN_SYNTHESIZER_TEMPERATURE`

`SOORIN_SYNTHESIZER_TEMPERATURE` controls final-answer sampling when the selected endpoint supports temperature. Lower values generally produce more stable and repeatable analytical wording; higher values increase variation and creativity but can reduce deterministic consistency. It is omitted when Synthesizer temperature support is disabled.

### `SOORIN_SYNTHESIZER_TOP_P`

`SOORIN_SYNTHESIZER_TOP_P` controls nucleus sampling for final synthesis when the provider accepts it. Lower values narrow the candidate token distribution and generally reduce variation, while higher values permit broader generation. It is not sent when the corresponding support flag is false.

### `SOORIN_SYNTHESIZER_SUPPORTS_TEMPERATURE`

`SOORIN_SYNTHESIZER_SUPPORTS_TEMPERATURE` tells the transport whether to include the configured Synthesizer `temperature` field. `true` sends it; `false` omits it for provider/model modes that do not support sampling parameters. The flag itself does not alter answer randomness unless the parameter is actually transmitted.

### `SOORIN_SYNTHESIZER_SUPPORTS_TOP_P`

`SOORIN_SYNTHESIZER_SUPPORTS_TOP_P` tells the transport whether the final model endpoint accepts `top_p`. `true` includes the configured value; `false` omits it to preserve compatibility. It has no effect on context budgeting or evidence retrieval.

---

## Product and Detection

### `SOORIN_PRODUCT_API_BASE_URL`

`SOORIN_PRODUCT_API_BASE_URL` defines the base URL for the Soorin Product backend used by Profile, Detection, topology refresh, authentication, and related operational integration. It must point to the reachable Product API for the current environment. Because this endpoint carries authoritative live evidence, an incorrect URL can make operational providers unavailable even while general LLM/RAG requests continue to work.

### `SOORIN_PRODUCT_TOPOLOGY_PATH`

`SOORIN_PRODUCT_TOPOLOGY_PATH` is the HTTP endpoint path appended to the Product base URL to retrieve unique network-connection IP pairs used for Graph refresh. It is an API route, not a filesystem path. Changing it changes the source contract for the NetworkX topology and must match the Product backend endpoint schema.

### `SOORIN_PRODUCT_ASSET_DETECTION_PATH`

`SOORIN_PRODUCT_ASSET_DETECTION_PATH` is the HTTP path template for the deep/full Detection endpoint and must contain the supported `{ip}` placeholder form expected by the Product client. It is used for expensive exhaustive Detection retrieval rather than normal compact views. An invalid template breaks full Detection requests. `(double-check exact path-placeholder validation against current settings.py)`

### `SOORIN_PRODUCT_ASSET_DETECTION_OVERVIEW_PATH`

`SOORIN_PRODUCT_ASSET_DETECTION_OVERVIEW_PATH` is the HTTP path template for the compact Detection overview view. It retrieves the normal high-level model-facing Detection evidence for an entity and is preferred over full Detection when the TaskSpec only requires classification/summary information. The path must preserve the `{ip}` substitution contract.

### `SOORIN_PRODUCT_ASSET_DETECTION_EVIDENCE_PATH`

`SOORIN_PRODUCT_ASSET_DETECTION_EVIDENCE_PATH` is the HTTP path template for the compact Detection evidence view used when the workflow needs supporting rules/signals or explanation beyond the overview. It reduces context and transport size compared with the deep full endpoint while using the same Product authentication/session machinery.

### `SOORIN_PRODUCT_ASSET_DETECTION_SIMILARITY_PATH`

`SOORIN_PRODUCT_ASSET_DETECTION_SIMILARITY_PATH` is the HTTP path template for Detection similarity evidence. In the current Product semantics, similarity represents rule/tag/role affinity rather than embedding-vector similarity. The path must match the Product backend contract and preserve `{ip}` substitution.

### `SOORIN_PRODUCT_ASSET_DETECTION_CLUSTER_PATH`

`SOORIN_PRODUCT_ASSET_DETECTION_CLUSTER_PATH` is the HTTP path template for the Detection cluster/cohort view. Current semantics describe grouping by Detection rule/tag/role affinity rather than an arbitrary unsupervised ML cluster, and small populations should not be overinterpreted. The endpoint uses the same authenticated Product client.

### `SOORIN_PRODUCT_ASSET_PROFILE_PATH`

`SOORIN_PRODUCT_ASSET_PROFILE_PATH` is the Product HTTP path template used to fetch the current full asset Profile object for an IP. The provider retains the full response internally and deterministic Profile projections such as `overview`, `identity`, `security`, `network`, or `activity` are selected for model context. Current validation requires the expected safe `{ip}` placeholder contract.

### `SOORIN_PRODUCT_LOGIN_PATH`

`SOORIN_PRODUCT_LOGIN_PATH` is the HTTP route used by the shared Product client to obtain or refresh an authenticated Product session when credentials are configured. It is not a filesystem path. Changing it requires the Product authentication contract to expose the same login behavior at the new route.

### `LLM_USAGE_REPORTING_ENABLED`

`LLM_USAGE_REPORTING_ENABLED` controls whether completed Copilot requests send aggregated LLM token-usage metadata to the configured Product reporting endpoint. `true` enables reporting; `false` skips the reporting call without disabling Copilot generation. Reporting is observability/accounting and does not influence Router, Planner, or Synthesizer decisions.

### `LLM_USAGE_REPORTING_URL`

`LLM_USAGE_REPORTING_URL` is the complete Product endpoint URL used to report aggregated LLM usage after a request. It is a URL rather than a capacity parameter; changing it only changes the reporting destination. Reporting failures should remain non-authoritative to the analytical result according to the current integration design.

### `SOORIN_PRODUCT_API_TOKEN`

`SOORIN_PRODUCT_API_TOKEN` optionally provides an initial Product bearer token that the shared Product client can use before performing login. It is a secret and must never be committed or logged. The client can invalidate and refresh/login when the token is rejected according to the current 401 recovery behavior.

### `SOORIN_PRODUCT_USERNAME`

`SOORIN_PRODUCT_USERNAME` supplies the Product account username used by the Product authentication flow when login is required. It is authentication identity information and should remain private even though it is not equivalent to a password.

### `SOORIN_PRODUCT_PASSWORD`

`SOORIN_PRODUCT_PASSWORD` is the Product account password used during Product login. It is a high-sensitivity secret and must remain only in private runtime configuration or an external secret manager, never Git, trace output, or logs.

### `SOORIN_PRODUCT_CAPTCHA_BYPASS`

`SOORIN_PRODUCT_CAPTCHA_BYPASS` supplies the private Product-side captcha-bypass credential/value required by the service login integration where configured. It is a secret, should never be exposed to users or logs, and must match the Product backend authentication contract.

### `SOORIN_PRODUCT_TOKEN_REFRESH_SECONDS`

`SOORIN_PRODUCT_TOKEN_REFRESH_SECONDS` controls how long an acquired Product authentication token is considered reusable before proactive refresh/login logic treats it as due for renewal. A larger value reuses tokens longer and reduces authentication traffic but risks approaching Product expiry; a smaller value refreshes more frequently and increases login overhead. The Product's actual token lifetime remains authoritative.

### `SOORIN_PRODUCT_HWID`

`SOORIN_PRODUCT_HWID` supplies the `x-hwid` identity/credential value required by the Product API integration. It is security-sensitive deployment material and must not appear in tracked configuration, logs, or model context.

### `SOORIN_PRODUCT_CONNECT_TIMEOUT_SECONDS`

`SOORIN_PRODUCT_CONNECT_TIMEOUT_SECONDS` limits how long Product HTTP calls wait to establish a connection. Increasing it tolerates slower network establishment but delays recognition of unreachable Product services; decreasing it fails connectivity problems faster.

### `SOORIN_PRODUCT_READ_TIMEOUT_SECONDS`

`SOORIN_PRODUCT_READ_TIMEOUT_SECONDS` limits how long the Product client waits for response data after a connection has been established. Larger values support slower/deeper Product endpoints but can hold capability execution longer; smaller values reduce worst-case wait but may prematurely fail valid slow responses.

### `SOORIN_PRODUCT_MAX_RETRIES`

`SOORIN_PRODUCT_MAX_RETRIES` configures the bounded HTTP retry behavior used by the Product client/session adapter for retryable transport/status failures. Increasing it can improve resilience to temporary Product/network errors but increases worst-case latency and repeated traffic; decreasing it fails more quickly. Authentication 401 refresh behavior also has its own bounded semantics.

### `SOORIN_PRODUCT_RETRY_BACKOFF_SECONDS`

`SOORIN_PRODUCT_RETRY_BACKOFF_SECONDS` controls the delay/backoff base between retryable Product API attempts. Larger values reduce immediate retry pressure at the cost of slower recovery; smaller values retry sooner but can increase request bursts against an unhealthy backend.

### `SOORIN_DETECTION_CACHE_ENABLED`

`SOORIN_DETECTION_CACHE_ENABLED` enables the in-process Detection response cache. `true` allows recently retrieved Detection evidence for the same normalized IP/view to be reused within the configured TTL, reducing Product latency and duplicate requests; `false` fetches Detection evidence from Product on every required call.

### `SOORIN_DETECTION_CACHE_TTL_SECONDS`

`SOORIN_DETECTION_CACHE_TTL_SECONDS` defines how long cached Detection entries remain fresh enough for ordinary cache reuse. A larger TTL reduces Product traffic and latency but allows older Detection evidence to be reused longer; a smaller TTL improves freshness at the cost of more Product requests. This cache policy does not override TaskSpec/Gate-8 evidence freshness rules for authoritative memory reuse.

### `SOORIN_DETECTION_STALE_ON_ERROR`

`SOORIN_DETECTION_STALE_ON_ERROR` controls whether a previously cached Detection result may be returned explicitly as stale when a fresh Product request fails. `true` preserves bounded stale evidence with freshness/limitation metadata instead of losing all Detection context; `false` reports the provider evidence as unavailable when refresh fails. Stale evidence must never be presented as current.

---

## Graph

### `SOORIN_GRAPH_RAW_PATH`

`SOORIN_GRAPH_RAW_PATH` is the filesystem path for retained raw Product topology payloads. These JSON snapshots support audit and debugging only; the active graph and all graph evidence are published from Neo4j.

### `SOORIN_GRAPH_MAX_UI_NODES`

`SOORIN_GRAPH_MAX_UI_NODES` limits how many graph nodes the Streamlit topology visualization may render. Increasing it can display more of the topology but raises browser rendering, layout, memory, and interaction cost; decreasing it keeps visualization responsive by showing a more bounded subset. It does not change backend Graph evidence.

### `SOORIN_GRAPH_DEFAULT_MIN_DEGREE`

`SOORIN_GRAPH_DEFAULT_MIN_DEGREE` controls the default minimum node degree used by topology UI filtering. Raising it hides lower-connectivity nodes and emphasizes more connected assets; lowering it includes more peripheral nodes and increases visual density. This is a visualization filter rather than an analytical evidence threshold.

### `SOORIN_GRAPH_API_MAX_NEIGHBORS`

`SOORIN_GRAPH_API_MAX_NEIGHBORS` caps the number of neighbors returned by the public Graph neighbor API endpoint. Increasing it exposes larger deterministic API responses and raises serialization/network cost; decreasing it keeps responses smaller but can omit additional neighbors from that API representation. Copilot's internal graph retrieval has its own scope-specific limits.

### `SOORIN_GRAPH_DEFAULT_SCOPE`

`SOORIN_GRAPH_DEFAULT_SCOPE` defines the fallback/default Graph retrieval scope when the application needs a Graph scope and none more specific has been established. The current example uses `node_summary`; supported runtime scopes include concepts such as `node_summary`, `one_hop`, `full_neighbors`, `two_hop`, comparison, and path through their validated routing contracts, but not every route value is necessarily valid as a configurable default. `(double-check exact validation enum in settings.py)`

### `SOORIN_GRAPH_ONE_HOP_MAX_NODES`

`SOORIN_GRAPH_ONE_HOP_MAX_NODES` caps how many nodes a bounded one-hop Graph retrieval may include. A larger value preserves more direct-neighbor evidence but increases retrieval/serialization/context pressure; a smaller value keeps requests compact and may truncate broad neighborhoods, which is surfaced through completeness metadata.

### `SOORIN_GRAPH_FULL_NEIGHBORS_HARD_MAX`

`SOORIN_GRAPH_FULL_NEIGHBORS_HARD_MAX` is the hard safety ceiling for exhaustive direct-neighbor retrieval. Increasing it permits larger “all neighbors” evidence sets but can create very large Graph results; decreasing it bounds cost more aggressively and causes exhaustive requests beyond the ceiling to be reported as limited rather than silently complete.

### `SOORIN_GRAPH_TWO_HOP_MAX_NODES`

`SOORIN_GRAPH_TWO_HOP_MAX_NODES` caps the number of nodes included in two-hop topology retrieval. Larger values improve coverage of neighbors-of-neighbors but grow graph size rapidly and consume more memory/context; smaller values make two-hop analysis cheaper but increase truncation.

### `SOORIN_GRAPH_MAX_EDGES`

`SOORIN_GRAPH_MAX_EDGES` limits Graph edge records retained during bounded retrieval. Increasing it allows denser subgraphs but increases CPU, memory, serialization, and context pressure; decreasing it protects performance but may cause Graph completeness limitations when the requested scope requires more edges.

### `SOORIN_GRAPH_MAX_CONTEXT_TOKENS`

`SOORIN_GRAPH_MAX_CONTEXT_TOKENS` is the global maximum token budget available to Graph evidence in final model context. A larger budget allows more peer/path/comparison detail but leaves less room for other evidence/history/output; a smaller budget compacts Graph more aggressively. Scope-specific serializers may impose smaller caps before this global ceiling.

### `SOORIN_GRAPH_MAX_PATH_LENGTH`

`SOORIN_GRAPH_MAX_PATH_LENGTH` limits the maximum number of nodes/hops serialized for a selected shortest observed communication-graph path. Increasing it permits longer paths to be represented but uses more context and may produce less useful long chains; decreasing it truncates long paths earlier and must surface that limitation.

### `SOORIN_GRAPH_FULL_ENUMERATION_MAX_PEERS`

`SOORIN_GRAPH_FULL_ENUMERATION_MAX_PEERS` limits how many individual peer identities are enumerated into model context for full-neighbor requests even when backend retrieval has more peers. Increasing it makes exhaustive answers more list-rich but consumes more context; decreasing it preserves aggregate totals while showing fewer explicit peers. Retrieval completeness and model-visible enumeration remain separate concepts.

### `SOORIN_GRAPH_CONTEXT_MAX_ENUMERATED_NODES`

`SOORIN_GRAPH_CONTEXT_MAX_ENUMERATED_NODES` caps the number of retrieved graph node identities that compact Graph serialization may expose to the final model. A larger value preserves more explicit node detail at higher token cost; a smaller value relies more heavily on aggregate metadata and omitted-count reporting.

### `SOORIN_GRAPH_CONTEXT_MAX_ENUMERATED_EDGES`

`SOORIN_GRAPH_CONTEXT_MAX_ENUMERATED_EDGES` caps the number of graph edges that may be explicitly serialized into model-visible context where the selected serializer supports edge records. Increasing it provides more edge-level evidence but can quickly consume tokens; decreasing it protects context size. Pair direct-relationship truth is protected separately in comparison logic.

### `SOORIN_GRAPH_COMPARISON_MAX_PEERS_PER_ENTITY`

`SOORIN_GRAPH_COMPARISON_MAX_PEERS_PER_ENTITY` limits how many peer identities per compared asset are retained in comparison detail. Increasing it provides richer distinct-peer examples but increases retrieval/context size; decreasing it keeps pair comparisons compact while aggregate peer totals, degree differences, subnet diversity, and protected direct-relationship facts remain available.

### `SOORIN_GRAPH_COMPARISON_MAX_SHARED_PEERS`

`SOORIN_GRAPH_COMPARISON_MAX_SHARED_PEERS` limits how many shared peer identities are explicitly included in two-asset comparison evidence. A larger value preserves more overlap detail at higher token cost; a smaller value retains aggregate shared-peer counts while showing fewer concrete identities.

### `SOORIN_GRAPH_AUTO_REFRESH_ENABLED`

`SOORIN_GRAPH_AUTO_REFRESH_ENABLED` controls the background Graph refresh scheduler. `true` periodically retrieves current topology from Product and safely replaces the active graph after validation; `false` leaves the loaded last-known-good artifact unchanged until an explicit/manual refresh path is used.

### `SOORIN_GRAPH_REFRESH_INTERVAL_SECONDS`

`SOORIN_GRAPH_REFRESH_INTERVAL_SECONDS` sets the nominal interval between scheduled Graph refresh checks. The default is 3600 seconds (one hour). Increasing it refreshes less frequently, reducing Product/load activity but allowing the topology projection to age longer; decreasing it improves snapshot freshness while increasing Product traffic and Neo4j sync work.

### `SOORIN_GRAPH_REFRESH_ON_STARTUP`

`SOORIN_GRAPH_REFRESH_ON_STARTUP` controls whether the scheduler attempts a refresh after API startup. `true` schedules an initial refresh after the configured delay while still allowing last-known-good startup; `false` waits for the regular interval/manual workflow and avoids immediate Product topology traffic.

### `SOORIN_GRAPH_REFRESH_STARTUP_DELAY_SECONDS`

`SOORIN_GRAPH_REFRESH_STARTUP_DELAY_SECONDS` defines how long the refresh scheduler waits after application startup before its startup refresh attempt. A larger delay gives other services time to become ready and reduces startup contention; a smaller delay refreshes topology sooner.

### `SOORIN_GRAPH_REFRESH_JITTER_SECONDS`

`SOORIN_GRAPH_REFRESH_JITTER_SECONDS` adds bounded timing variation around scheduled Graph refresh work to avoid synchronized refresh bursts when multiple instances start or run on the same cadence. Increasing jitter spreads requests over a wider time range; reducing it makes refresh timing more predictable. `(double-check exact jitter application semantics against refresh scheduler code)`

### `SOORIN_GRAPH_REFRESH_MAX_CONSECUTIVE_FAILURES`

`SOORIN_GRAPH_REFRESH_MAX_CONSECUTIVE_FAILURES` limits how many successive refresh failures are tolerated before the scheduler enters its configured protective behavior. A larger value tolerates longer Product/network outages before escalation, while a smaller value reacts sooner. Failed refreshes preserve the last-known-good active graph rather than replacing it with invalid data. `(double-check exact post-threshold behavior in refresh code)`

### `SOORIN_GRAPH_REFRESH_KEEP_RAW_SNAPSHOTS`

`SOORIN_GRAPH_REFRESH_KEEP_RAW_SNAPSHOTS` sets how many historical raw Product snapshots are retained by count during refresh cleanup. This retains source evidence for audit and debugging, never an active graph fallback.

### `SOORIN_GRAPH_SNAPSHOT_TTL_HOURS`

`SOORIN_GRAPH_SNAPSHOT_TTL_HOURS` controls age-based Graph snapshot cleanup. A positive value removes retained snapshots older than the configured number of hours in addition to count-based retention; `0` disables TTL-based cleanup according to the current configuration contract. Larger values retain history longer and use more disk.


### `SOORIN_GRAPH_REFRESH_LOCK_TIMEOUT_SECONDS`

`SOORIN_GRAPH_REFRESH_LOCK_TIMEOUT_SECONDS` bounds how long refresh work waits to acquire the Graph refresh lock when another refresh is already in progress. Increasing it waits longer for an existing refresh to finish; decreasing it abandons competing refresh attempts sooner and reduces the chance of stacked work.

### `SOORIN_GRAPH_REFRESH_MIN_NODES`

`SOORIN_GRAPH_REFRESH_MIN_NODES` is a safety threshold requiring a refreshed topology to contain at least the configured number of nodes before it may replace the active last-known-good graph. Raising it rejects suspiciously small snapshots more aggressively; lowering it permits smaller environments but weakens that sanity check.

### `SOORIN_GRAPH_REFRESH_MIN_EDGES`

`SOORIN_GRAPH_REFRESH_MIN_EDGES` requires a candidate refreshed graph to contain at least the configured number of edges before activation. Increasing it guards against unexpectedly empty/incomplete topology responses; decreasing it supports genuinely sparse environments but reduces this validation protection.

---

## RAG and Embeddings

### `SOORIN_RAG_ENABLED`

`SOORIN_RAG_ENABLED` globally controls the Knowledge/RAG capability. `true` initializes the configured knowledge retrieval foundation and allows Router/plan-selected `knowledge.search`; `false` makes Knowledge unavailable while Product/Graph and unrelated functionality can continue. RAG remains documentation authority, not live operational truth.

### `SOORIN_RAG_SOURCE_ROOT`

`SOORIN_RAG_SOURCE_ROOT` is the filesystem root containing approved source documents for the separate indexing/maintenance workflow. It is not scanned during normal application startup or ordinary retrieval. The corpus is normally kept outside the repository; changing this path affects what the explicit indexer can discover, not an already-built Qdrant collection.

### `SOORIN_RAG_BACKEND`

`SOORIN_RAG_BACKEND` selects the vector-search backend implementation for Knowledge retrieval, with the current architecture using `qdrant`. It is a backend identifier, not a boolean. Only values implemented and validated by the current code are usable. `(double-check exact accepted backend enum in settings.py)`

### `SOORIN_RAG_COLLECTION`

`SOORIN_RAG_COLLECTION` specifies the Qdrant collection containing indexed approved Knowledge chunks. Changing it switches retrieval to a different collection and therefore a potentially different corpus/index version; its vector dimension/distance must match the configured embedding model.

### `SOORIN_RAG_TOP_K`

`SOORIN_RAG_TOP_K` controls the maximum number of top vector-search candidates requested for a normal Knowledge query before context budgeting/safety filtering. Increasing it improves recall and provides more potential evidence but raises search/context work; decreasing it reduces latency/context and may miss relevant chunks.

### `SOORIN_RAG_SCORE_THRESHOLD`

`SOORIN_RAG_SCORE_THRESHOLD` sets the minimum vector-similarity score accepted for Knowledge results. Raising it increases precision by rejecting weaker matches but can reduce recall and return no chunks; lowering it increases recall but admits less semantically relevant material. The appropriate range depends on the configured distance/scoring semantics.

### `SOORIN_RAG_QDRANT_MODE`

`SOORIN_RAG_QDRANT_MODE` selects how Qdrant is accessed. Current supported modes are `local`, which opens an embedded file-backed Qdrant database at `SOORIN_RAG_QDRANT_PATH`, and `server`, which connects to `SOORIN_RAG_QDRANT_URL`. Local mode is useful for single-process development; server mode is appropriate when Qdrant runs as a service.

### `SOORIN_RAG_QDRANT_URL`

`SOORIN_RAG_QDRANT_URL` is the Qdrant server base URL used only when `SOORIN_RAG_QDRANT_MODE=server`. It should be empty/not required in local mode. An incorrect URL makes Knowledge retrieval unavailable but must not be interpreted as absence of knowledge.

### `SOORIN_RAG_QDRANT_PATH`

`SOORIN_RAG_QDRANT_PATH` is the filesystem location of the embedded Qdrant database used when `SOORIN_RAG_QDRANT_MODE=local`. The API process must have appropriate access, and only one compatible embedded-process ownership pattern should use that storage at a time. Container deployment may override it into the persistent `/workspace/data` tree.

### `SOORIN_RAG_QDRANT_API_KEY`

`SOORIN_RAG_QDRANT_API_KEY` provides the private authentication credential for Qdrant server mode when the remote Qdrant deployment requires one. It must remain secret and is normally unused/empty for local embedded mode.

### `SOORIN_RAG_QDRANT_TIMEOUT_SECONDS`

`SOORIN_RAG_QDRANT_TIMEOUT_SECONDS` limits Qdrant server search/management operations. Increasing it tolerates slower vector-service responses but delays failure detection; decreasing it fails an unavailable/overloaded Qdrant service sooner. Retrieval failure must remain a classified unavailable/partial state rather than fabricated evidence.

### `SOORIN_RAG_EMBEDDING_MODEL`

`SOORIN_RAG_EMBEDDING_MODEL` identifies the Hugging Face embedding model used for both document and query vectors in the current Knowledge index, with the current architecture using `BAAI/bge-base-en-v1.5`. Changing the model generally requires rebuilding the collection because vectors from different models are not interchangeable.

### `SOORIN_RAG_EMBEDDING_DIMENSION`

`SOORIN_RAG_EMBEDDING_DIMENSION` defines the expected embedding vector dimension and must exactly match both the configured embedding model output and Qdrant collection dimension. The current BGE-base configuration uses `768`, and current validation explicitly expects that value for the default BGE configuration. Arbitrarily increasing or decreasing it will not improve quality and instead creates dimension mismatches.

### `SOORIN_RAG_EMBEDDING_LOCAL_FILES_ONLY`

`SOORIN_RAG_EMBEDDING_LOCAL_FILES_ONLY` controls Hugging Face model-loading network behavior. `true` requires the exact configured model/revision to already exist in the local cache and safely reports unavailable when missing; `false` permits Hugging Face's normal download path on first use. Offline/local production deployments should normally keep it `true`.

### `SOORIN_RAG_EMBEDDING_CACHE_DIR`

`SOORIN_RAG_EMBEDDING_CACHE_DIR` points to the filesystem cache containing Hugging Face tokenizer/model files. It allows the lazy embedder to use a controlled external model cache rather than storing weights in the repository; container deployments typically bind-mount this cache read-only. A wrong path can cause safe model-unavailable results when local-only mode is active.

### `SOORIN_RAG_EMBEDDING_REVISION`

`SOORIN_RAG_EMBEDDING_REVISION` pins the exact Hugging Face model revision used by the embedding loader. Pinning makes indexing and query embeddings reproducible and prevents silent model drift; changing the revision should be treated like changing the embedding model and may require index validation/rebuild.

### `SOORIN_RAG_DISTANCE`

`SOORIN_RAG_DISTANCE` selects the vector distance/similarity metric expected by the Qdrant collection; the current configuration uses `cosine`. The configured metric must match the collection created for the embeddings. Changing it without rebuilding/reconfiguring the collection invalidates score interpretation. `(double-check exact accepted metric values in current vector-store validation)`

### `SOORIN_RAG_MAX_CONTEXT_TOKENS`

`SOORIN_RAG_MAX_CONTEXT_TOKENS` caps how many approximate tokens of retrieved Knowledge chunks may be included in final model context. Increasing it allows more documentation evidence but can crowd other context/output; decreasing it makes RAG more compact and can omit lower-ranked useful chunks. Operational Product/Graph evidence retains higher authority.

### `SOORIN_RAG_CHUNK_SIZE_CHARS`

`SOORIN_RAG_CHUNK_SIZE_CHARS` controls the target character size used by the explicit Knowledge indexing chunker. Larger chunks preserve more local document context but produce coarser retrieval and larger model-context items; smaller chunks improve retrieval granularity but can fragment concepts and create more vectors.

### `SOORIN_RAG_CHUNK_OVERLAP_CHARS`

`SOORIN_RAG_CHUNK_OVERLAP_CHARS` controls character overlap between adjacent indexed document chunks. Increasing overlap helps preserve concepts crossing chunk boundaries but duplicates more text/vectors and index storage; decreasing overlap reduces duplication but increases the chance that boundary-spanning information is split.

### `SOORIN_RAG_UPSERT_BATCH_SIZE`

`SOORIN_RAG_UPSERT_BATCH_SIZE` sets how many vectors are written to Qdrant per indexing batch. Larger batches can improve indexing throughput but consume more memory and create larger individual requests; smaller batches reduce peak resource pressure but increase the number of Qdrant operations. It affects indexing maintenance, not normal search.

---

## Conversation and Context

### `SOORIN_CHAT_STORE_HISTORY`

`SOORIN_CHAT_STORE_HISTORY` controls whether completed user/assistant turns are retained in the active conversation memory implementation. `true` enables bounded history used for continuity and UI/session behavior; `false` avoids retaining ordinary chat history beyond the immediate request. It does not enable long-term typed organizational memory.

### `SOORIN_CHAT_MAX_HISTORY_MESSAGES`

`SOORIN_CHAT_MAX_HISTORY_MESSAGES` limits the total number of raw chat messages retained by the conversation store before older messages are discarded. Increasing it preserves more local conversation history but increases memory/storage and potential context-selection work; decreasing it keeps history smaller. Final-model context remains separately bounded.

### `SOORIN_CONVERSATION_MAX_MESSAGES`

`SOORIN_CONVERSATION_MAX_MESSAGES` is the raw-history retention-pressure threshold. When enabled summary maintenance has older complete turns available, crossing this count summarizes them before removal; final model selection remains separately token bounded. Larger values can preserve more conversational continuity but consume input tokens and increase stale-context risk; smaller values prioritize current evidence and the current request.

### `SOORIN_CONVERSATION_RECENT_RAW_MESSAGES`

`SOORIN_CONVERSATION_RECENT_RAW_MESSAGES` is a message count controlling recent raw preservation alongside a compact deterministic summary. It is rounded up to an even count and never below two, so at least one complete user/assistant pair remains raw. Increasing it gives the Synthesizer more verbatim recent context at higher token cost; decreasing it relies more heavily on summarized/selected memory.

### `SOORIN_CONVERSATION_SUMMARY_ENABLED`

`SOORIN_CONVERSATION_SUMMARY_ENABLED` controls deterministic bounded conversation-summary maintenance. `true` permits the memory layer to replace excess older conversational material with a compact summary once thresholds are reached; `false` disables that summary mechanism and leaves continuity to the configured raw/relevant-turn limits.

### `SOORIN_CONVERSATION_SUMMARY_TRIGGER_TOKENS`

`SOORIN_CONVERSATION_SUMMARY_TRIGGER_TOKENS` defines the approximate conversation-memory size at which the deterministic summary process becomes eligible. Lower values summarize sooner and reduce raw-context pressure but lose verbatim detail earlier; higher values retain more raw material before compaction and consume more memory/context budget.

### `SOORIN_CONVERSATION_SUMMARY_MAX_TOKENS`

`SOORIN_CONVERSATION_SUMMARY_MAX_TOKENS` caps the approximate size of the compact conversation summary. A larger budget preserves more historical detail but uses more final memory context; a smaller budget is cheaper but forces stronger compression. It is a token budget, not a secret.

### `SOORIN_CONVERSATION_SUMMARY_TEMPERATURE`

`SOORIN_CONVERSATION_SUMMARY_TEMPERATURE` is retained as configuration for conversation-summary behavior, but the current architecture states that working/episode summaries are deterministic and do not use an LLM. It is retained for environment compatibility but is inactive: deterministic summary code does not read it or call an LLM.

### `SOORIN_CONVERSATION_SUMMARY_TIMEOUT_SECONDS`

`SOORIN_CONVERSATION_SUMMARY_TIMEOUT_SECONDS` historically/configurationally bounds summary generation work, but current architecture describes conversation/episode summaries as deterministic and non-LLM. It is retained for environment compatibility but is inactive: deterministic summary work has no provider timeout.

### `SOORIN_SYSTEM_PROMPT_PATH`

`SOORIN_SYSTEM_PROMPT_PATH` points to the filesystem Markdown file containing the static final-Synthesizer policy core. The default is `app/prompts/synthesizer/synthesizer_static_prompt.md`; deterministic typed task modules are composed with it at request time. The legacy `app/prompts/system_prompt.md` remains available as an explicit rollback path and as the compatibility fallback when the configured static prompt is missing or empty. Changing this path does not alter deterministic Router, PlanValidator, EvidenceReviewer, or provider authority.

---

## Working and Long-Term Memory

### `SOORIN_DURABLE_WORKING_MEMORY_ENABLED`

`SOORIN_DURABLE_WORKING_MEMORY_ENABLED` controls use of the bounded typed thread/working-memory state that can survive through the configured thread-state adapter where durability is available. `true` enables the current working-memory continuity path; `false` limits continuity to the simpler/in-process behavior. This is distinct from typed long-term organizational memory.

### `SOORIN_MEMORY_RELEVANT_TURN_LIMIT`

`SOORIN_MEMORY_RELEVANT_TURN_LIMIT` caps both retained ThreadState turn references and how many same-conversation prior turns deterministic relevant-turn selection may include for the current entity/topic context. Increasing it preserves more potentially useful investigation history but consumes more memory-context budget; decreasing it gives stronger recency/compactness.

### `SOORIN_MEMORY_RELEVANT_TURN_TOKEN_BUDGET`

`SOORIN_MEMORY_RELEVANT_TURN_TOKEN_BUDGET` caps the combined token estimate of selected relevant prior turns. Increasing it permits richer historical conversation evidence but leaves less room within the total memory context; decreasing it prioritizes compact continuity and current operational evidence.

### `SOORIN_MEMORY_EPISODE_RETENTION_LIMIT`

`SOORIN_MEMORY_EPISODE_RETENTION_LIMIT` limits how many completed bounded episode summaries are retained in the current thread/session memory. Increasing it preserves a longer investigation history and uses more state/storage; decreasing it removes older episodes sooner. Retention alone does not make an old episode relevant to the current task.

### `SOORIN_MEMORY_EPISODE_CONTEXT_LIMIT`

`SOORIN_MEMORY_EPISODE_CONTEXT_LIMIT` limits how many matching previous episode summaries may be reintroduced into the model-facing memory package for a request. Increasing it can restore more historical context when context keys match but consumes more tokens; decreasing it keeps context focused on the most relevant/recent episodes.

### `SOORIN_MEMORY_EPISODE_CONTEXT_TOKEN_BUDGET`

`SOORIN_MEMORY_EPISODE_CONTEXT_TOKEN_BUDGET` caps token usage for episode-summary context. A larger budget preserves more episode detail but competes with relevant-turn and long-term-memory budgets; a smaller budget makes episode recall more aggressively compact.

### `SOORIN_MEMORY_CONTEXT_TOKEN_BUDGET`

`SOORIN_MEMORY_CONTEXT_TOKEN_BUDGET` is the overall token budget available to the bounded short-term/working-memory package before final context composition. Increasing it provides richer continuity but can increase input size and stale-context pressure; decreasing it protects current evidence/context capacity. Current live Product/Graph evidence remains authoritative regardless of this budget.

### `SOORIN_LONG_TERM_MEMORY_ENABLED`

`SOORIN_LONG_TERM_MEMORY_ENABLED` globally enables the typed long-term memory subsystem. `true` allows current approved/canonical memory retrieval and related lifecycle behavior through the configured backend; `false` keeps long-term memory out of the request context while working/episodic memory can still operate. This should be enabled deliberately during the long-term-memory test phase.

### `SOORIN_LONG_TERM_MEMORY_BACKEND`

`SOORIN_LONG_TERM_MEMORY_BACKEND` accepts `sqlite` or `product`. `sqlite` remains the local behavioral reference; `product` selects `ProductLongTermMemoryStore`, whose canonical authority is the Product Backend/PostgreSQL lifecycle API. In Product mode, each lifecycle decision is one `POST {SOORIN_PRODUCT_MEMORY_LTM_PATH}/{memoryId}/transition` request and the Backend owns any related-record/audit changes in its transaction. Copilot does not dual-write or fall back to SQLite. Qdrant remains a discovery index, never a canonical backend.

### `SOORIN_MEMORY_VECTOR_INDEX_ENABLED`

`SOORIN_MEMORY_VECTOR_INDEX_ENABLED` controls whether typed long-term memory also uses the semantic Qdrant index for candidate recall. `true` enables BGE-based semantic candidate retrieval in addition to exact structured/entity lookup; `false` leaves canonical/exact memory retrieval available without semantic vector search. The vector index is never the canonical source of truth.

### `SOORIN_MEMORY_QDRANT_COLLECTION`

`SOORIN_MEMORY_QDRANT_COLLECTION` specifies the separate Qdrant collection used to index typed memory retrieval projections, for example `soorin_copilot_memory_v1`. It must remain separate from the general Knowledge/RAG collection because memory has different authority, filters, ownership, provenance, and lifecycle semantics.

### `SOORIN_MEMORY_RETRIEVAL_CANDIDATE_K`

`SOORIN_MEMORY_RETRIEVAL_CANDIDATE_K` controls how many semantic-memory candidates are initially recalled before canonical hydration, validity/freshness filtering, deduplication, and optional reranking. Increasing it can improve recall and gives the reranker more alternatives but increases vector search, canonical lookup, and optional CrossEncoder work; decreasing it lowers cost but may discard relevant memories before final selection.

### `SOORIN_MEMORY_RETRIEVAL_TOP_K`

`SOORIN_MEMORY_RETRIEVAL_TOP_K` limits how many validated long-term memory records survive the retrieval pipeline for potential context inclusion. Increasing it preserves more relevant memories but consumes more memory-context budget; decreasing it keeps recall concise. It should normally be less than or equal to `SOORIN_MEMORY_RETRIEVAL_CANDIDATE_K`.

### `SOORIN_MEMORY_MIN_SCORE`

`SOORIN_MEMORY_MIN_SCORE` sets the minimum semantic similarity score accepted from memory-vector retrieval before candidate use. Raising it favors precision and rejects weak semantic matches but can reduce recall; lowering it allows broader recall but increases irrelevant candidates. This score affects candidate relevance only and never establishes entity identity, authority, freshness, or epistemic confidence.

### `SOORIN_MEMORY_RERANK_ENABLED`

`SOORIN_MEMORY_RERANK_ENABLED` enables optional CrossEncoder reranking of the already bounded memory candidate pool. `true` loads/uses the configured reranker when available to improve relevance ordering; `false` preserves the original exact/BGE retrieval order. Reranking cannot change canonical authority, freshness, entity binding, or confidence, and failures safely fall back to the pre-rerank order.

### `SOORIN_MEMORY_RERANK_MODEL`

`SOORIN_MEMORY_RERANK_MODEL` identifies the optional local CrossEncoder model used to score `(current query, candidate memory)` pairs after initial retrieval. It is a separate reranker model identifier and is not automatically the same model as `SOORIN_RAG_EMBEDDING_MODEL`; leaving it empty while reranking is disabled requires no additional model. The exact approved reranker has not yet been selected for the current dev test. `(double-check after model selection)`

### `SOORIN_MEMORY_RERANK_TIMEOUT_SECONDS`

`SOORIN_MEMORY_RERANK_TIMEOUT_SECONDS` bounds optional CrossEncoder reranking work for the small memory candidate pool. A larger timeout allows a slower CPU reranker to finish but can increase request latency; a smaller timeout abandons reranking earlier and safely falls back to the original candidate ordering.

### `SOORIN_MEMORY_CONTEXT_LONG_TERM_TOKEN_BUDGET`

`SOORIN_MEMORY_CONTEXT_LONG_TERM_TOKEN_BUDGET` caps the portion of final memory context reserved for validated typed long-term memory records. Increasing it permits more/longer historical organizational memory to reach synthesis but competes with other context; decreasing it keeps long-term memory concise. It does not alter retrieval authority or permit memory to replace volatile live evidence.

### `SOORIN_MEMORY_AUTO_PROMOTION_ENABLED`

Enables the deterministic candidate evaluation path. The default is `true`, but it
has no effect while `SOORIN_LONG_TERM_MEMORY_ENABLED=false`. Automatic promotion is
limited to the versioned safe structured Product profile/detection allow-list;
analyst statements, hypotheses, incomplete/stale evidence, unsafe classes, and
contradictions do not become active automatically.

### `SOORIN_MEMORY_PROMOTION_POLICY_VERSION`

Identifies the deterministic policy recorded on each lifecycle decision and audit
event. The default is `ltm-promotion-v1`. Change this only with a reviewed policy
and regression tests; it is an audit/version label, not a free-form prompt.

### `SOORIN_MEMORY_ACTIVE_VALIDITY_SECONDS`

Sets the validity window applied to automatically promoted operational records,
measured from the structured Product observation time. The default is `86400`
(24 hours), with settings validation constraining it to 60–2,592,000 seconds.
Elapsed validity removes operational authority; it is independent from storage
retention and does not delete audit history.

---

## Local Persistence and State

### `SOORIN_LOCAL_PRODUCT_SIMULATION_ENABLED`

`SOORIN_LOCAL_PRODUCT_SIMULATION_ENABLED` enables the development-only local Product/chat simulation layer backed by local persistence. `true` activates local conversation/user simulation routes and storage behavior needed for the unified local workspace; `false` keeps the normal legacy/Product-facing workflow. Local simulation is development infrastructure and is not production authentication.

### `SOORIN_STREAMLIT_AUTH_BACKEND`

`SOORIN_STREAMLIT_AUTH_BACKEND` selects the Streamlit-side identity/chat backend mode: `none` for the legacy direct developer workflow, `local_simulation` for the password-free SQLite development simulation, `product` for real Product login/chatroom/message APIs, and `oidc` for the existing OIDC placeholder. Product mode retains the interactive Product JWT only in the Streamlit session, sends it to Product chat CRUD, and sends it with the custom Copilot API key and trusted `X-User-ID` when streaming. It has no SQLite fallback and startup validation requires Product thread state and Product long-term memory.

### `SOORIN_LOCAL_TEST_USER_CREATION_ENABLED`

`SOORIN_LOCAL_TEST_USER_CREATION_ENABLED` controls whether the local development simulation is allowed to create local test-user metadata. `true` permits the development-only user creation path where local simulation is active; `false` prevents creation. It must not be treated as a production user-provisioning or authentication capability.

### `SOORIN_LOCAL_PRODUCT_TEST_USER_ID`

`SOORIN_LOCAL_PRODUCT_TEST_USER_ID` is an optional dedicated non-production Product owner identifier used only when local simulation exercises Product thread-state or Product LTM persistence. It replaces the random local simulation user only in Product-memory transport (`X-User-ID`); domain `RequestIdentity` and `LongTermMemoryRecord.user_id` remain the local user. Leave it blank unless the Product test backend recognizes the owner. It is ownership context, not authentication, and must never contain a production user identity.

### `SOORIN_THREAD_STATE_BACKEND`

`SOORIN_THREAD_STATE_BACKEND` accepts `memory`, `sqlite`, or `product`. `memory` is process-local; `sqlite` is opt-in local persistence; `product` uses the Product memory API and PostgreSQL through the shared Product authentication client. Product mode requires a trusted `X-User-ID` and `conversation_id` per request, does not fall back to SQLite on failure, and persists only bounded typed state—not arbitrary LangGraph runtime state or raw provider payloads.

### `SOORIN_PRODUCT_MEMORY_THREAD_STATE_PATH` and `SOORIN_PRODUCT_MEMORY_LTM_PATH`

These non-secret paths select Product Backend memory resources while reusing `SOORIN_PRODUCT_API_BASE_URL`, bearer authentication, and HWID. The thread path receives a conversation ID suffix; the LTM path is the base for create, search, ID, audit, and transition routes. Defaults are `/api/v1/copilot/memory/thread-state` and `/api/v1/copilot/memory/ltm`.

### `SOORIN_PRODUCT_CHAT_ROOMS_PATH`

Non-secret base path for Product-owned chatroom and transcript CRUD used by Streamlit `product` mode. The default is `/chat-rooms`; room IDs and message suffixes are derived by the Product UI client. Product owns authorization, chatroom identity, ordering, and PostgreSQL persistence. Copilot does not fall back to local transcript storage when these calls fail.

### `SOORIN_LOCAL_SQLITE_PATH`

`SOORIN_LOCAL_SQLITE_PATH` is the filesystem location of the local development SQLite database used by local chat simulation, thread state, and development memory adapters where enabled. The parent directory must be writable; changing it selects a different local persistence database. It contains development state and should live under ignored/persistent runtime data rather than source control.

### `SOORIN_LOCAL_MAX_CONVERSATIONS_PER_USER`

Positive owner-scoped local conversation limit. The default is `100`. At capacity,
the oldest conversation without durable thread state is removed; if every record is
protected, creation fails safely.

### `SOORIN_LOCAL_MAX_MESSAGES_PER_CONVERSATION`

Positive stored transcript-message limit per local conversation. The default is
`200`; successful appends prune only the oldest messages and retain the newest.

### `SOORIN_MEMORY_WORKING_FACT_RETENTION_LIMIT`

Positive same-conversation working-fact limit. The default is `20`. Deterministic
key replacement occurs before retaining the newest bounded set.

### `SOORIN_MEMORY_MAX_ACTIVE_RECORDS_PER_USER`

Positive active LTM limit per owner. The default is `500`. Admission beyond the
limit is rejected; existing authoritative records are never deleted automatically.

### `SOORIN_MEMORY_MAX_CANDIDATE_RECORDS_PER_USER`

Positive candidate LTM limit per owner. The default is `250`. A genuinely new
candidate at capacity evicts the oldest candidate only; exact retries deduplicate
before quota enforcement.

### `SOORIN_LANGGRAPH_CHECKPOINT_BACKEND`

`SOORIN_LANGGRAPH_CHECKPOINT_BACKEND` selects the configured LangGraph checkpoint backend. The current safe/implemented runtime behavior uses `none`; architecture documentation notes that SQLite checkpointing remains deliberately deferred because full `InvestigationState` is not yet checkpoint-safe. A configured `sqlite` value may currently log deferral rather than enabling resume. `(double-check exact accepted values and current deferral behavior against workflow initialization code)`

---

## Evidence Snapshots and Human Trace

### `SOORIN_HUMAN_TRACE_ENABLED`

`SOORIN_HUMAN_TRACE_ENABLED` controls emission of the bounded human-readable workflow trace. `true` records the safe request/entity/routing/plan/evidence/context/memory/result trace without prompts, secrets, raw payloads, or hidden reasoning; `false` disables the human trace while ordinary structured application logging can continue.

### `SOORIN_HUMAN_TRACE_DETAIL`

`SOORIN_HUMAN_TRACE_DETAIL` controls human-trace verbosity. Supported values are `summary` and `detailed`: `summary` provides the compact routing/execution/review/result view, while `detailed` includes bounded section-by-section visibility for entity authority, Planner, capabilities, Product views, Graph completeness, Knowledge, context budgets, memory decisions, and usage without exposing payload contents.

### `SOORIN_EVIDENCE_SNAPSHOT_ENABLED`

`SOORIN_EVIDENCE_SNAPSHOT_ENABLED` enables request-scoped sanitized evidence snapshot artifacts for debugging/audit. `true` writes snapshots according to the selected snapshot mode and retention limits; `false` disables snapshot storage. It is intentionally disabled by default because snapshots consume disk and may contain operational values even after safety filtering.

### `SOORIN_EVIDENCE_SNAPSHOT_MODE`

`SOORIN_EVIDENCE_SNAPSHOT_MODE` selects the evidence snapshot representation. Current documented modes are `summary`, `metadata`, `redacted`, and `none`: `summary` stores bounded safe task/plan/result values, `metadata` stores shape/count metadata with less actual evidence content, `redacted` permits deeper diagnostic structure while applying mandatory redaction, and `none` stores no evidence content. Exact implementation semantics should remain aligned with snapshot code.

### `SOORIN_EVIDENCE_SNAPSHOT_ROOT`

`SOORIN_EVIDENCE_SNAPSHOT_ROOT` is the filesystem directory under which request/date-scoped evidence snapshot artifacts are written. It should live in ignored persistent runtime storage, normally beneath `data/runtime/evidence`, and requires API write access. Snapshot code applies restrictive file/directory permissions according to the current design.

### `SOORIN_EVIDENCE_SNAPSHOT_TTL_HOURS`

`SOORIN_EVIDENCE_SNAPSHOT_TTL_HOURS` defines age-based retention for completed evidence snapshot request directories. Increasing it keeps diagnostic snapshots for longer and consumes more disk; decreasing it removes them sooner. Cleanup is bounded and non-fatal to normal Copilot execution.

### `SOORIN_EVIDENCE_SNAPSHOT_MAX_REQUESTS`

`SOORIN_EVIDENCE_SNAPSHOT_MAX_REQUESTS` caps the number of completed request snapshot directories retained. Increasing it preserves more individual investigations at greater storage cost; decreasing it prunes older requests more aggressively regardless of available disk.

### `SOORIN_EVIDENCE_SNAPSHOT_MAX_TOTAL_BYTES`

`SOORIN_EVIDENCE_SNAPSHOT_MAX_TOTAL_BYTES` caps aggregate storage consumed by retained evidence snapshots. Increasing it allows a larger diagnostic archive; decreasing it triggers size-based pruning sooner. It protects the runtime data volume from unbounded observability growth.

### `SOORIN_EVIDENCE_SNAPSHOT_MAX_BYTES`

`SOORIN_EVIDENCE_SNAPSHOT_MAX_BYTES` limits the maximum storage permitted for one individual request snapshot. Increasing it permits deeper/larger diagnostic records but raises worst-case per-request disk use; decreasing it forces large snapshots to remain more strongly bounded or be rejected/truncated according to snapshot policy.

---

## Model Cache Runtime

### `HF_HUB_DISABLE_TELEMETRY`

`HF_HUB_DISABLE_TELEMETRY` is a Hugging Face runtime environment flag controlling Hub telemetry. Truthy/enabled behavior disables Hugging Face telemetry from the process, which is appropriate for privacy-focused/offline deployments; disabling the flag allows the library's normal telemetry behavior according to the installed Hugging Face version. It does not control model downloads.

### `HF_HUB_OFFLINE`

`HF_HUB_OFFLINE` controls Hugging Face Hub offline mode. When enabled, Hugging Face libraries avoid network Hub requests and require requested models/revisions to exist in local cache; when disabled, normal Hub network access is permitted where other settings allow it. This complements, but is distinct from, the Soorin-specific `SOORIN_RAG_EMBEDDING_LOCAL_FILES_ONLY` setting.

### `TRANSFORMERS_OFFLINE`

`TRANSFORMERS_OFFLINE` controls offline behavior in the Transformers library. When enabled, Transformers must resolve model/tokenizer resources from local files/cache and should not attempt remote retrieval; when disabled, normal library download behavior can occur when requested. Local/offline Copilot deployments normally enable it alongside the Hugging Face offline settings.
