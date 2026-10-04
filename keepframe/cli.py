from __future__ import annotations
import argparse, json, os, sys, threading
from pathlib import Path
from urllib.parse import urlparse
from .compose.composer import compose
from .ir.store import load_scene, save_scene
from .ir.synth import make_synthetic_scene
from .log import configure, get
from .render.renderer import render, render_result_from_json
from .verify.verifier import verify


def _ae_relay_config() -> tuple[str, str, int, str] | None:
    names = (
        "KEEPFRAME_AE_RELAY_URL",
        "KEEPFRAME_AE_RELAY_HOST",
        "KEEPFRAME_AE_RELAY_PORT",
        "KEEPFRAME_AE_RELAY_TOKEN",
    )
    values = {name: (os.getenv(name) or "").strip() for name in names}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise ValueError("all KEEPFRAME_AE_RELAY_* settings are required")
    try:
        parsed = urlparse(values["KEEPFRAME_AE_RELAY_URL"])
        public_port = parsed.port
    except ValueError as exc:
        raise ValueError("KEEPFRAME_AE_RELAY_URL has an invalid port") from exc
    loopback = (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or public_port is not None
        and not 1 <= public_port <= 65535
        or parsed.scheme == "http"
        and not loopback
    ):
        raise ValueError("KEEPFRAME_AE_RELAY_URL must be HTTPS or loopback HTTP without credentials")
    try:
        port = int(values["KEEPFRAME_AE_RELAY_PORT"])
    except ValueError as exc:
        raise ValueError("KEEPFRAME_AE_RELAY_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("KEEPFRAME_AE_RELAY_PORT must be between 1 and 65535")
    return (
        values["KEEPFRAME_AE_RELAY_URL"].rstrip("/"),
        values["KEEPFRAME_AE_RELAY_HOST"],
        port,
        values["KEEPFRAME_AE_RELAY_TOKEN"],
    )



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="keepframe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth"); s.add_argument("--out", required=True); s.add_argument("--seed", type=int, default=1)
    s.add_argument("--frames", type=int, default=60); s.add_argument("--no-text", action="store_true")
    c = sub.add_parser("compose"); c.add_argument("--scene", required=True); c.add_argument("--out", required=True)
    r = sub.add_parser("render"); r.add_argument("--scene", required=True); r.add_argument("--html", required=True)
    r.add_argument("--out", required=True); r.add_argument("--frames", default=None); r.add_argument("--mp4", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("--scene", required=True); v.add_argument("--render-json", default=None)
    v.add_argument("--reference", default=None)
    g = sub.add_parser("gate-m1"); g.add_argument("--out", required=True); g.add_argument("--n", type=int, default=20)
    an = sub.add_parser("analyze"); an.add_argument("--video", required=True); an.add_argument("--start", type=int, default=0)
    an.add_argument("--end", type=int, required=True); an.add_argument("--out", required=True); an.add_argument("--copy", default=None)
    an.add_argument("--no-ocr", action="store_true"); an.add_argument("--no-refine", action="store_true"); an.add_argument("--bg", default=None)
    an.add_argument("--ui", action="store_true")
    an.add_argument("--no-captions", action="store_true")
    an.add_argument("--ocr-max-side", type=int, default=1280)
    co = sub.add_parser("correct"); co.add_argument("--root", required=True); co.add_argument("--scene", default="s1")
    co.add_argument("--op", required=True, choices=["reassign", "mask", "bbox", "text"]); co.add_argument("--args", required=True)
    g2 = sub.add_parser("gate-m2"); g2.add_argument("--out", required=True); g2.add_argument("--n", type=int, default=20)
    g2r = sub.add_parser("gate-m2-real"); g2r.add_argument("--clips", required=True); g2r.add_argument("--out", required=True)
    g2r.add_argument("--max-frames", type=int, default=150)
    g2r.add_argument("--render-check", action="store_true")
    ed = sub.add_parser("edit")
    ed.add_argument("--root", required=True)
    ed.add_argument("--scene", default="s1")
    ed.add_argument("--prompt", required=True)
    ed.add_argument("--attach", default=None)
    ed.add_argument("--element", default=None)
    ed.add_argument("--confirm", action="store_true")
    ed.add_argument("--choice", action="append", default=[])
    g3 = sub.add_parser("gate-m3"); g3.add_argument("--out", required=True); g3.add_argument("--n", type=int, default=8)
    sv = sub.add_parser("serve"); sv.add_argument("--workspace", required=True); sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--admin", action=argparse.BooleanOptionalAction, default=True)
    ae_install = sub.add_parser("ae-install")
    ae_install.add_argument("--ae-path")
    ae_connect = sub.add_parser("ae-connect")
    ae_connect.add_argument("--url", required=True)
    ae_target = ae_connect.add_mutually_exclusive_group()
    ae_target.add_argument("--code")
    ae_target.add_argument("--project")
    a = ap.parse_args(argv)

    if a.cmd == "synth":
        out = Path(a.out)
        scene = make_synthetic_scene(out, seed=a.seed, frames=a.frames, with_text=not a.no_text)
        save_scene(scene, out / "scene.json"); print(out / "scene.json"); return 0
    if a.cmd == "compose":
        sp = Path(a.scene); print(compose(load_scene(sp), sp.parent, Path(a.out))); return 0
    if a.cmd == "render":
        sp = Path(a.scene); frames = [int(x) for x in a.frames.split(",")] if a.frames else None
        res = render(Path(a.html), load_scene(sp), Path(a.out), frames=frames, mp4=a.mp4)
        print(json.dumps({"frames": len(res.frames), "mp4": str(res.mp4) if res.mp4 else None})); return 0
    if a.cmd == "verify":
        sp = Path(a.scene); scene = load_scene(sp)
        rr = render_result_from_json(Path(a.render_json)) if a.render_json else None
        ref = load_scene(Path(a.reference)) if a.reference else None
        ref_dir = Path(a.reference).parent if a.reference else None
        rep = verify(scene, sp.parent, render_result=rr, reference=ref, reference_dir=ref_dir)
        print(rep.model_dump_json(indent=2)); return 0 if rep.passed else 1
    if a.cmd == "gate-m1":
        from .gates import m1_gate
        res = m1_gate(Path(a.out), n=a.n)
        for row in res["rows"]:
            print(row)
        print({k: v for k, v in res.items() if k != "rows"}); return 0 if res["passed"] else 1
    if a.cmd == "analyze":
        from .analyze.pipeline import AnalyzeOptions, analyze
        from .session.llm import vision_llm
        opts = AnalyzeOptions(bg_override=a.bg, copy=a.copy.split(",") if a.copy else None, ocr=not a.no_ocr,
                              refine=not a.no_refine, ui=a.ui, ocr_max_side=a.ocr_max_side)
        p = analyze(Path(a.video), a.start, a.end, Path(a.out), opts, captioner=None if a.no_captions else vision_llm())
        print(json.dumps({"scenes": [s.id for s in p.scenes], "version": p.versions[-1].id})); return 0
    if a.cmd == "correct":
        from .review import corrections as C
        from .ir.schema import FontGuess, validate_font_family
        kw = json.loads(a.args)
        if a.op == "reassign":
            v = C.reassign_id(Path(a.root), a.scene, tuple(kw["frames"]), kw["from_id"], kw["to_id"], note=kw.get("note", "reassign id"))
        elif a.op == "mask":
            v = C.set_region_mask(Path(a.root), a.scene, kw["frame"], Path(kw["mask_png"]), kw["object_id"], note=kw.get("note", "set region mask"))
        elif a.op == "bbox":
            v = C.add_bbox_prompt(Path(a.root), a.scene, kw["frame"], tuple(kw["bbox"]), kw["object_id"], note=kw.get("note", "bbox prompt"))
        else:
            font_data = kw.get("font")
            if font_data and "family_guess" in font_data:
                try:
                    font_data = {**font_data, "family_guess": validate_font_family(font_data["family_guess"])}
                except ValueError as exc:
                    co.error(str(exc))
            font = FontGuess(**font_data) if font_data else None
            v = C.edit_text(Path(a.root), a.scene, kw["element_id"], text=kw.get("text"),
                            font=font, note=kw.get("note", "edit text"))
        print(v.model_dump_json(indent=2)); return 0
    if a.cmd == "gate-m2":
        from .gates import m2_gate
        res = m2_gate(Path(a.out), n=a.n)
        for row in res["rows"]:
            print(row)
        print({k: v for k, v in res.items() if k != "rows"}); return 0 if res["passed"] else 1
    if a.cmd == "edit":
        from .edit.agent import edit
        choices = {}
        for item in a.choice:
            if "=" in item:
                k, v = item.split("=", 1)
                choices[k] = v
        res = edit(
            Path(a.root),
            a.scene,
            a.prompt,
            attachment=Path(a.attach) if a.attach else None,
            element=a.element,
            confirm=a.confirm,
            choices=choices or None,
        )
        print(json.dumps(res.to_json(), ensure_ascii=False, indent=2))
        return 0 if res.status in ("done", "needs_confirm", "needs_choice") else 1
    if a.cmd == "gate-m3":
        from .gates import m3_gate
        res = m3_gate(Path(a.out), n=a.n)
        for row in res["rows"]:
            print(row)
        print({k: v for k, v in res.items() if k != "rows"})
        return 0 if res["passed"] else 1
    if a.cmd == "gate-m2-real":
        from .gates import m2_gate_real
        print(json.dumps(m2_gate_real(Path(a.clips), Path(a.out), max_frames=a.max_frames,
                                    render_check=a.render_check), indent=2)); return 0
    if a.cmd == "ae-install":
        from .after_effects.installer import install_panel, manual_instructions

        panel = install_panel(Path(a.ae_path) if a.ae_path else None)
        print(panel)
        print(manual_instructions())
        return 0
    if a.cmd == "ae-connect":
        from getpass import getpass

        from .after_effects.connector import run_connector

        code = a.code
        if code is None and a.project is None:
            code = getpass("Pairing code: ")
        return run_connector(a.url, code=code, project=a.project)
    if a.cmd == "serve":
        from .web.server import JOBS, make_server
        configure()
        log = get("keepframe.cli")
        host = a.host
        workspace = Path(a.workspace)
        relay_config = _ae_relay_config()
        kwargs: dict = {
            "admin": a.admin,
            "ae_relay_url": relay_config[0] if relay_config is not None else None,
        }
        if a.admin:
            from .admin.memory import MemoryAdmin
            from .admin.auth import MemoryAuth, load_admin_users
            users = load_admin_users()
            kwargs["admin_svc"] = MemoryAdmin(workspace=workspace, job_store=JOBS)
            kwargs["admin_auth"] = MemoryAuth(users)
        srv = make_server(workspace, port=a.port, host=host, **kwargs)
        workflow = getattr(srv, "ae_workflow", None)
        relay = None
        relay_thread = None
        relay_started = False
        relay_failures: list[Exception] = []
        stopping = threading.Event()
        private_serving = threading.Event()
        try:
            if relay_config is not None:
                relay_url, relay_host, relay_port, relay_token = relay_config
                from .after_effects.relay import make_relay_server

                relay = make_relay_server(
                    workspace,
                    host=relay_host,
                    port=relay_port,
                    deployment_token=relay_token,
                    workflow=workflow,
                )

                def serve_relay() -> None:
                    try:
                        relay.serve_forever()
                    except Exception as exc:
                        relay_failures.append(exc)
                    else:
                        if not stopping.is_set():
                            relay_failures.append(
                                RuntimeError("AE relay stopped unexpectedly")
                            )
                    finally:
                        while not stopping.is_set():
                            if private_serving.wait(0.05):
                                if not stopping.is_set():
                                    srv.shutdown()
                                break

                relay_thread = threading.Thread(
                    target=serve_relay,
                    daemon=True,
                    name="keepframe-ae-relay",
                )
                relay_thread.start()
                relay_started = True
                log.info(
                    "AE relay listening %s bind=%s:%s",
                    relay_url,
                    relay_host,
                    relay_port,
                )
            from .analyze.device import gpu_status

            st = gpu_status()
            if not st["torch"]:
                log.info("torch not installed; refine stays on CPU skip")
            else:
                log.info(
                    "torch %s cuda=%s device=%s name=%s required=%s",
                    st["torch_version"],
                    st["cuda"],
                    st["device"],
                    st["name"],
                    st["required"],
                )
                if st["required"] and not st["cuda"]:
                    log.error(
                        "KEEPFRAME_DEVICE=cuda but CUDA is not available; "
                        "sprite refine will fail"
                    )
            log.info(
                "listening http://%s:%s/ workspace=%s admin=%s",
                host,
                a.port,
                a.workspace,
                a.admin,
            )
            if a.admin:
                log.info("admin http://%s:%s/admin/", host, a.port)
                from .session.chatgpt_oauth import (
                    CHATGPT_CALLBACK_PORT,
                    FLOW,
                    callback_bind_host,
                )

                FLOW.workspace = workspace
                FLOW.bind_host = callback_bind_host(host)
                try:
                    FLOW.ensure_listener()
                    log.info(
                        "chatgpt oauth callback http://localhost:%s/auth/callback",
                        CHATGPT_CALLBACK_PORT,
                    )
                except RuntimeError as e:
                    log.warning("%s", e)
            private_serving.set()
            srv.serve_forever()
            if relay_failures:
                raise relay_failures[0]
        except Exception:
            log.exception("serve stopped")
            raise
        finally:
            stopping.set()
            srv.server_close()
            if relay is not None:
                if relay_started:
                    relay.shutdown()
                relay.server_close()
                if relay_started and relay_thread is not None:
                    relay_thread.join()
            if workflow is not None:
                close = getattr(workflow, "close", None)
                if callable(close):
                    close()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
