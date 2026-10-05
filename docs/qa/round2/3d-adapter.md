# Hugging Face image-to-3D adapter

The adapter exposes the existing `keepframe.asset.request/1` API on a
`ThreadingHTTPServer` bound only to `127.0.0.1`. It accepts `task=generate`,
`kind=3d`, and `input_image={mime: "image/png", data: <base64>}`. The existing
`prompt` and `size` fields are accepted; these image-conditioned Space recipes
use the input image and the Space's published defaults. Successful responses
contain validated, self-contained GLB bytes with `Content-Type:
model/gltf-binary`. Unsupported requests return HTTP 501. Invalid input returns
400/413, and Space failures return HTTP 503 with JSON
`code=asset_api_unavailable`.

## Run

From the repository root:

```bash
export KEEPFRAME_ASSET_API_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
uv run --with gradio_client scripts/asset_adapter_hf.py --port 8790
uv run --with gradio_client scripts/asset_adapter_hf.py --port 8790 --space stabilityai/stable-fast-3d
```

The default `--space` is `trellis-community/TRELLIS`; the second command shows
an explicit override. Use one server command at a time. The server refuses to
start without a nonempty `KEEPFRAME_ASSET_API_KEY`. The analysis process must inherit the same
exported key; the existing `AssetClient` sends it as `Authorization: Bearer
<key>` automatically. In that process, also set:

```bash
export KEEPFRAME_ASSET_API_URL=http://127.0.0.1:8790
```

Missing or incorrect bearer authentication returns HTTP 401. Any `Origin`
header (including an empty value or `null`) returns HTTP 403, and requests
whose `Content-Type` is not `application/json` return HTTP 415. These checks
run before parsing the body or calling a Space. Keys are never logged.

`gradio_client` is imported lazily and is not a project runtime dependency.
Server requests use `HF_TOKEN`, or `huggingface_hub.get_token()` when the
environment variable is absent, so the server uses the logged-in Hugging Face
token when available. Tokens are never printed. Each request uses its
own temporary download directory and Gradio session. Result file paths must
resolve inside that request's directory; symlinks, including directory
symlinks, and nonregular files are ignored. If no valid GLB remains, the adapter
returns HTTP 503 with `code=asset_api_unavailable`. The adapter bounds discovery,
upload, generation, and download with a **300-second** deadline and attempts to
cancel timed-out jobs. A running remote GPU job may outlive local cancellation.
Each accepted client connection also has a **30-second socket timeout** to
bound stalled request reads and response writes; the upstream generation
deadline remains 300 seconds.

The existing `AssetClient` has a 120-second default timeout and represents HTTP
failures as `asset_api_http_error`; its caller can still take the existing error
fallback. To wait for the entire adapter budget in a direct smoke test, use
`AssetClient(timeout=310)`:

```python
from pathlib import Path
from keepframe.assets.client import AssetClient

response = AssetClient("http://127.0.0.1:8790", timeout=310).request(
    task="generate",
    kind="3d",
    prompt="A 3D model of the input image",
    size={"width": 64, "height": 64},
    input_image=Path("input.png").read_bytes(),
)
Path("output.glb").write_bytes(response.data)
```

The inspected recipes call `/requires_bg_remove` before Stable Fast 3D's
`/run_button`, Hunyuan's `/generation_all` with `seed=0` and
`randomize_seed=False`, and TRELLIS's `/start_session` and `/preprocess_image`
before `/generate_and_extract_glb` with `seed=0`. Hunyuan's textured GLB is
preferred over its shape-only output. Stable Fast 3D's public endpoint depends
on its prepared alpha-image session state; opaque inputs remain subject to that
Space's background-removal behavior. Unknown endpoint layouts fail as
unavailable and require an explicit new recipe.

These calls follow the [Gradio Python client documentation](https://www.gradio.app/guides/getting-started-with-the-python-client)
and the inspected [Stable Fast 3D source](https://huggingface.co/spaces/stabilityai/stable-fast-3d/blob/main/gradio_app.py)
and [TRELLIS source](https://huggingface.co/spaces/trellis-community/TRELLIS/blob/main/app.py).
Public Spaces can queue, rate-limit, change their APIs, and produce different
results despite fixed seeds.

## Anonymous live probe, 2026-10-05 KST

```bash
uv run --with gradio_client scripts/asset_adapter_hf.py --probe
```

`--probe` is **always anonymous**: it explicitly uses `token=False`, even when
`HF_TOKEN` is set or local credential lookup would succeed. ZeroGPU Spaces may
fail this anonymous probe on quota even when logged-in server requests succeed.
It starts no HTTP server and requires no
`KEEPFRAME_ASSET_API_KEY`. It visits the candidates in the required order, calls
`view_api(print_info=False, return_format="dict")`, performs the required
session preparation, and attempts one generation pipeline for a 64×64 RGBA PNG containing
a red circle on a transparent background. Its only output is one JSON result
per Space, containing reachability, endpoint names, valid-GLB success/failure,
and the first 120 characters of any sanitized error.

Final anonymous results with `gradio_client 2.7.2`:

| Space | Reachable / API discovered | Valid GLB | Error (first 120 characters) |
| --- | --- | --- | --- |
| `stabilityai/stable-fast-3d` | yes | no | `The upstream Gradio app has raised an exception but has not enabled verbose error reporting. To enable, set show_error=T` |
| `tencent/Hunyuan3D-2` | yes | no | `'NameError'` |
| `trellis-community/TRELLIS` | yes | no | `You have exceeded your ZeroGPU quota (120s requested vs. 160s left). Try again in 23:51:22. Authenticate with a Hugging` |

Endpoint names returned by `view_api()`:

- `stabilityai/stable-fast-3d`: `/lambda`, `/requires_bg_remove`, `/run_button`, `/update_foreground_ratio`.
- `tencent/Hunyuan3D-2`: `/generation_all`, `/lambda`, `/lambda_1`, `/lambda_2`, `/lambda_3`, `/lambda_4`, `/lambda_5`, `/lambda_6`, `/on_decode_mode_change`, `/on_export_click`, `/on_gen_mode_change`, `/shape_generation`.
- `trellis-community/TRELLIS`: `/extract_gaussian`, `/generate_and_extract_glb`, `/get_seed`, `/lambda`, `/lambda_1`, `/lambda_2`, `/lambda_3`, `/lambda_4`, `/lambda_5`, `/preprocess_image`, `/preprocess_images`, `/start_session`.

All three APIs were reachable; none yielded a valid GLB anonymously in this
probe. TRELLIS explicitly reported quota/authentication limits. The other two
errors do not establish that login would fix them. No credentials were obtained
or login attempted. A preliminary probe used credential lookup and is excluded
from this anonymous evidence; the final CLI now prevents that path. Any later
user login request belongs to the controller.

## Controller logged-in smoke

The controller supplied these results using the logged-in token:

| Space | Result |
| --- | --- |
| `trellis-community/TRELLIS` | Returned a valid **1.33 MB GLB in 51 s**. This is the default server Space. |
| `stabilityai/stable-fast-3d` | Failed Space-side: "upstream Gradio app has raised an exception". |

These authenticated results are separate from the anonymous `--probe` results
above. This fix round did not make additional live Space requests.

## Controller follow-up: ig2 re-analysis

The ig2 measurement depends on Tasks 15/16 and is deferred to the controller.
No evaluation inputs or outputs were changed here. After integrating those
tasks, generate and export a key as above, start the adapter, and ensure the
analysis process inherits that same `KEEPFRAME_ASSET_API_KEY`. Set
`KEEPFRAME_ASSET_API_URL=http://127.0.0.1:8790`, and run the ig2 re-analysis and
render check using the [round-2 evaluation commands](README.md). Record the
guard-selected representation, all three representation error values, any
globe GLB render screenshot, and the resulting `render_l1`. Compare against the
README's ig2 baseline (`mean_l1=0.0644`, `render_l1=0.0738`). Record unavailable
3D output as observed; it is an accepted probe outcome.

## Offline tests

```bash
.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_asset_adapter_hf.py
```

Tests inject fake Gradio clients and perform only loopback HTTP calls. They
cover the real asset-client request/response contract, the three generation
endpoint layouts, session preparation, fixed seeds, textured-output selection,
invalid images/GLBs, remote failures, deadlines/cancellation, optional imports,
credential selection, forced anonymous probes, the TRELLIS default and explicit
Space overrides, connection timeouts, and redacted HTTP/probe errors.
Security regressions cover required server configuration, bearer authentication,
Origin and media-type rejection, request-isolated downloads, and ignoring
outside paths and file/directory symlinks.
