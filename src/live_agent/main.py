import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .runtime import Runtime
from .settings import Settings


def create_app(settings=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = Runtime(settings)
        yield
        await app.state.runtime.stop()
        app.state.runtime.kb.close()
        app.state.runtime.store.close()

    app = FastAPI(title="直播间 Agent", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Loopback binding plus same-origin checks block browser-driven cross-origin writes.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "跨域操作被拒绝"}, status_code=403)
        return await call_next(request)

    @app.get("/api/status")
    async def status():
        return app.state.runtime.status()

    @app.get("/api/candidates")
    async def candidates():
        return app.state.runtime.store.candidates()

    @app.get("/api/tools")
    async def tools():
        return [{"name": t.name, "description": t.description,
                 "schema": t.args_schema.model_json_schema()}
                for t in app.state.runtime.agent.tools.values()]

    @app.post("/api/connect")
    async def connect():
        try:
            await app.state.runtime.start()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return app.state.runtime.status()

    @app.post("/api/pause")
    async def pause():
        app.state.runtime.executor.paused = True
        return app.state.runtime.status()

    @app.post("/api/resume")
    async def resume():
        runtime = app.state.runtime
        try:
            await runtime.browser.healthy()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime.error = ""
        runtime.executor.paused = False
        return runtime.status()

    @app.post("/api/approve/{candidate_id}")
    async def approve(candidate_id: str):
        runtime = app.state.runtime
        if settings.mode != "assist":
            raise HTTPException(409, "仅 assist 模式接受人工批准")
        try:
            candidate = runtime.executor.candidate(candidate_id)
            if candidate.status != "pending":
                raise ValueError("候选已处理")
            candidate.approved = True
            runtime.store.save(candidate)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "approved"}

    @app.websocket("/ws")
    async def stream(ws: WebSocket):
        if ws.headers.get("origin") not in (None, f"http://{ws.headers.get('host')}"):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                runtime = app.state.runtime
                await ws.send_json({"status": runtime.status(), "candidates": runtime.store.candidates()})
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            pass

    dist = Path("web/dist")
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(dist / "index.html")

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "doctor", "build-kb"], default="serve", nargs="?")
    parser.add_argument("--mode", choices=["shadow", "assist", "auto"])
    args = parser.parse_args()
    settings = Settings()
    if args.mode:
        settings.mode = args.mode
    if args.command == "build-kb":
        from .knowledge import Knowledge
        knowledge = Knowledge(settings=settings)
        try:
            print(json.dumps(asyncio.run(knowledge.build_vectors()), ensure_ascii=False))
        finally:
            knowledge.close()
    elif args.command == "doctor":
        from .browser import CDPBrowser

        async def doctor():
            browser = CDPBrowser(settings)
            try:
                await browser.connect()
                print(json.dumps({"connected": True, "product": await browser.product()}, ensure_ascii=False))
            finally:
                await browser.close()
        asyncio.run(doctor())
    else:
        uvicorn.run(create_app(settings), host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
