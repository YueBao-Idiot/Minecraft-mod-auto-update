from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class MockState:
    def __init__(self, files_dir: Path) -> None:
        self.files_dir = files_dir
        self.base_url = ""
        self.modrinth = {
            "projects": {},
            "versions": {},
            "project_versions": {},
            "hash_to_version": {},
            "search": [],
        }
        self.curseforge = {
            "projects": {},
            "files": {},
            "fingerprints": {},
            "search": [],
        }


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, state: MockState):
        super().__init__(address, handler)
        self.state = state


class MockHandler(BaseHTTPRequestHandler):
    server: MockServer

    def log_message(self, format, *args):  # noqa: A002
        return

    def _send(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name: str) -> None:
        path = self.server.state.files_dir / Path(name).name
        if not path.is_file():
            self._send(404, {"error": "file not found"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/java-archive")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        state = self.server.state
        if path.startswith("/files/"):
            self._send_file(path.split("/", 2)[2])
            return
        if path == "/v2/projects":
            ids: list[str] = []
            if "ids" in query:
                try:
                    ids = json.loads(query["ids"][0])
                except json.JSONDecodeError:
                    ids = []
            self._send(200, [state.modrinth["projects"][item] for item in ids if item in state.modrinth["projects"]])
            return
        if path.startswith("/v2/project/") and path.endswith("/version"):
            project_id = path[len("/v2/project/") : -len("/version")]
            versions = state.modrinth["project_versions"].get(project_id, [])
            self._send(200, versions)
            return
        if path.startswith("/v2/project/"):
            project_id = path[len("/v2/project/") :]
            project = state.modrinth["projects"].get(project_id)
            if project is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, project)
            return
        if path.startswith("/v2/version/"):
            version_id = path[len("/v2/version/") :]
            version = state.modrinth["versions"].get(version_id)
            if version is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, version)
            return
        if path == "/v2/search":
            self._send(200, {"hits": state.modrinth["search"], "total_hits": len(state.modrinth["search"])})
            return
        if path == "/v2/tag/game_version":
            self._send(200, [{"version": "1.20.1", "version_type": "release"}])
            return
        if path == "/v1/mods/search":
            self._send(200, {"data": state.curseforge["search"], "pagination": {"totalCount": len(state.curseforge["search"])}})
            return
        if path.startswith("/v1/mods/") and "/files/" in path:
            rest = path[len("/v1/mods/") :]
            mod_id, file_id = rest.split("/files/", 1)
            file_data = state.curseforge["files"].get(file_id)
            if file_data is None or str(file_data.get("modId")) != mod_id:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {"data": file_data})
            return
        if path.startswith("/v1/mods/") and path.endswith("/files"):
            mod_id = path[len("/v1/mods/") : -len("/files")]
            files = [
                item
                for item in state.curseforge["files"].values()
                if str(item.get("modId")) == mod_id
            ]
            files.sort(key=lambda item: item.get("fileDate", ""), reverse=True)
            self._send(200, {"data": files, "pagination": {"totalCount": len(files)}})
            return
        if path.startswith("/v1/mods/"):
            mod_id = path[len("/v1/mods/") :]
            project = state.curseforge["projects"].get(mod_id)
            if project is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {"data": project})
            return
        self._send(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        path = urllib.parse.urlparse(self.path).path
        state = self.server.state
        if path == "/v2/version_files":
            hashes = payload.get("hashes", [])
            result = {}
            for file_hash in hashes:
                version_id = state.modrinth["hash_to_version"].get(file_hash)
                if version_id and version_id in state.modrinth["versions"]:
                    result[file_hash] = state.modrinth["versions"][version_id]
            self._send(200, result)
            return
        if path == "/v1/mods":
            mod_ids = {str(item) for item in payload.get("modIds", [])}
            data = [project for mod_id, project in state.curseforge["projects"].items() if mod_id in mod_ids]
            self._send(200, {"data": data})
            return
        if path == "/v1/fingerprints":
            fingerprints = {int(item) for item in payload.get("fingerprints", [])}
            exact = []
            for fingerprint, file_id in state.curseforge["fingerprints"].items():
                if int(fingerprint) in fingerprints and file_id in state.curseforge["files"]:
                    exact.append({"id": int(file_id), "file": state.curseforge["files"][file_id]})
            self._send(200, {"data": {"exactMatches": exact, "unmatchedFingerprints": []}})
            return
        self._send(404, {"error": "not found", "path": path})


class MockApi:
    def __init__(self, files_dir: Path) -> None:
        self.state = MockState(files_dir)
        self.server = MockServer(("127.0.0.1", 0), MockHandler, self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        host, port = self.server.server_address
        self.state.base_url = f"http://{host}:{port}"
        return self.state.base_url

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
