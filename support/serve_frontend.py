#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
import mimetypes
import socketserver
import sys
from pathlib import Path
from urllib import error, parse, request

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class FrontendProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    static_dir = Path(".")
    backend_base_url = "http://127.0.0.1:8000"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def log_message(self, format: str, *args: object) -> None:
        sys.stdout.write(
            f"[serve_frontend] {self.address_string()} - {format % args}\n"
        )
        sys.stdout.flush()

    def _dispatch(self) -> None:
        if self.path == "/api" or self.path.startswith("/api/"):
            self._proxy_request()
            return

        if self.command not in {"GET", "HEAD"}:
            self.send_error(405, "Method Not Allowed")
            return

        self._serve_static()

    def _proxy_request(self) -> None:
        target_url = self._backend_target_url()
        body = self._read_request_body()
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        req = request.Request(target_url, data=body, headers=headers, method=self.command)

        try:
            with request.urlopen(req, timeout=30) as response:
                self._write_proxy_response(
                    status=response.status,
                    headers=response.headers.items(),
                    body=response.read() if self.command != "HEAD" else b"",
                )
        except error.HTTPError as exc:
            self._write_proxy_response(
                status=exc.code,
                headers=exc.headers.items(),
                body=exc.read() if self.command != "HEAD" else b"",
            )
        except Exception as exc:  # pragma: no cover - network failure path
            payload = f"proxy request failed: {exc}\n".encode("utf-8", errors="replace")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

    def _backend_target_url(self) -> str:
        split_result = parse.urlsplit(self.path)
        api_path = split_result.path
        rewritten_path = api_path[4:] if api_path.startswith("/api") else api_path
        if not rewritten_path:
            rewritten_path = "/"
        return parse.urlunsplit(
            (
                parse.urlsplit(self.backend_base_url).scheme,
                parse.urlsplit(self.backend_base_url).netloc,
                rewritten_path,
                split_result.query,
                "",
            )
        )

    def _read_request_body(self) -> bytes | None:
        length_text = self.headers.get("Content-Length")
        if not length_text:
            return None
        try:
            length = int(length_text)
        except ValueError:
            return None
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _write_proxy_response(
        self,
        *,
        status: int,
        headers: list[tuple[str, str]] | object,
        body: bytes,
    ) -> None:
        self.send_response(status)
        for key, value in headers:
            lower_key = key.lower()
            if lower_key in _HOP_BY_HOP_HEADERS or lower_key == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_static(self) -> None:
        target = self._resolve_static_path()
        if not target.exists():
            request_path = parse.urlsplit(self.path).path
            if Path(request_path).suffix:
                self.send_error(404, "Frontend asset not found")
                return
            target = self.static_dir / "index.html"

        if not target.exists() or not target.is_file():
            self.send_error(404, "Frontend asset not found")
            return

        data = target.read_bytes()
        content_type, encoding = mimetypes.guess_type(target.name)
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _resolve_static_path(self) -> Path:
        requested_path = parse.urlsplit(self.path).path
        normalized = requested_path.lstrip("/")
        candidate = (self.static_dir / normalized).resolve()
        static_root = self.static_dir.resolve()

        if not str(candidate).startswith(str(static_root)):
            return static_root / "index.html"
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve built frontend and proxy /api requests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--static-dir", required=True)
    parser.add_argument("--backend-base-url", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    FrontendProxyHandler.static_dir = Path(args.static_dir).resolve()
    FrontendProxyHandler.backend_base_url = args.backend_base_url.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), FrontendProxyHandler)
    print(
        f"[serve_frontend] serving {FrontendProxyHandler.static_dir} "
        f"on http://{args.host}:{args.port} -> {FrontendProxyHandler.backend_base_url}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
