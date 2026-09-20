#!/usr/bin/env python3
"""
Interface web local do ytm-dl.

Sobe um servidor em 127.0.0.1 e serve uma pagina para quem nao usa terminal:
cola o link, escolhe formato e pasta, acompanha o progresso faixa a faixa.

Nao usa nenhuma dependencia alem da biblioteca padrao e do proprio ytm-dl.
O servidor escuta apenas em loopback — nao fica exposto na rede.

Uso:  server.py [--port N] [--open]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import mimetypes
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

RAIZ = Path(__file__).resolve().parent
WEB = RAIZ / "web"
FFMPEG_LOCAL = RAIZ / "bin" / "ffmpeg"
FFMPEG_RELEASE = ("https://api.github.com/repos/eugeneware/ffmpeg-static"
                  "/releases/latest")


# ------------------------------------------------------------------ ffmpeg ---


def ffmpeg_path() -> str | None:
    """ffmpeg utilizavel: o que baixamos, senao o do sistema."""
    if FFMPEG_LOCAL.is_file() and os.access(FFMPEG_LOCAL, os.X_OK):
        return str(FFMPEG_LOCAL)
    return shutil.which("ffmpeg")


def ffmpeg_asset_url() -> str:
    """URL do binario estatico para esta arquitetura."""
    import platform
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    nome = f"ffmpeg-darwin-{arch}"
    with urlopen(Request(FFMPEG_RELEASE, headers={"User-Agent": "ytm-dl"}),
                 timeout=30) as r:
        dados = json.loads(r.read().decode())
    for a in dados.get("assets", []):
        if a["name"] == nome:
            return a["browser_download_url"]
    raise RuntimeError(f"release sem o arquivo {nome}")


def instalar_ffmpeg(progresso) -> str:
    """Baixa a build estatica para bin/ffmpeg, reportando o andamento."""
    url = ffmpeg_asset_url()
    FFMPEG_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    tmp = FFMPEG_LOCAL.with_suffix(".parcial")
    with urlopen(Request(url, headers={"User-Agent": "ytm-dl"}), timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        baixado = 0
        with open(tmp, "wb") as f:
            while pedaco := r.read(262144):
                f.write(pedaco)
                baixado += len(pedaco)
                progresso(baixado, total)
    tmp.chmod(0o755)
    tmp.replace(FFMPEG_LOCAL)
    # Confere que roda antes de declarar sucesso: um download truncado ou um
    # binario de outra arquitetura so apareceria na primeira conversao.
    r = subprocess.run([str(FFMPEG_LOCAL), "-version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        FFMPEG_LOCAL.unlink(missing_ok=True)
        raise RuntimeError("o binario baixado nao executou")
    return r.stdout.splitlines()[0] if r.stdout else "ffmpeg"


# ------------------------------------------------------------ modulo ytm-dl ---


def carregar_ytmdl():
    """Importa o ytm-dl.py (o hifen no nome impede um import normal)."""
    caminho = RAIZ / "ytm-dl.py"
    spec = importlib.util.spec_from_file_location("ytmdl", caminho)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ytmdl"] = mod          # dataclasses precisam achar o modulo
    spec.loader.exec_module(mod)
    return mod


# -------------------------------------------------------------------- jobs ---


class Job:
    def __init__(self, jid: str):
        self.id = jid
        self.eventos: queue.Queue[dict | None] = queue.Queue()
        self.capas: dict[int, bytes] = {}
        self.urls: dict[int, str] = {}
        self.corpo: dict = {}
        self.destino: str = ""
        self.cancelar = threading.Event()


JOBS: dict[str, Job] = {}


def criar_emissor(ytmdl, job: Job):
    """Emissor que serializa cada acontecimento como JSON para a pagina."""

    class WebEmitter(ytmdl.Emitter):
        def _envia(self, **ev):
            job.eventos.put(ev)

        def reading(self):
            self._envia(t="reading")

        def head(self, playlist, count, dest, fmt, dry_run):
            job.destino = str(dest)
            self._envia(t="head", playlist=playlist, count=count,
                        dest=str(dest), fmt=fmt, dry_run=dry_run)

        def present(self, n, ext, next_num, pooling):
            self._envia(t="present", n=n, ext=ext, next_num=next_num,
                        pooling=pooling)

        def track(self, idx, total, label, url):
            if job.cancelar.is_set():
                raise KeyboardInterrupt("cancelado pelo usuario")
            job.urls[idx] = url
            self._envia(t="track", idx=idx, total=total, label=label)

        def progress(self, idx, d):
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            baixado = d.get("downloaded_bytes") or 0
            self._envia(t="progress", idx=idx,
                        pct=round(baixado / total * 100, 1) if total else None,
                        speed=d.get("speed"), eta=d.get("eta"))

        def skipped(self, idx, label, path, estado):
            self._envia(t="skipped", idx=idx, nome=path.name, estado=estado,
                        tamanho=path.stat().st_size)

        def dup(self, idx, label, other):
            self._envia(t="dup", idx=idx, outro=other.name)

        def orphan(self, idx, label, other, ext):
            self._envia(t="orphan", idx=idx,
                        outro=other.name if other else None, ext=ext)

        def failed(self, idx, label, motivo):
            self._envia(t="failed", idx=idx, motivo=motivo)

        def dry(self, idx, label, meta):
            if meta.cover:
                job.capas[idx] = meta.cover
            self._envia(t="dry", idx=idx, titulo=meta.title, artista=meta.artist,
                        album=meta.album, generos=meta.genres,
                        capa=bool(meta.cover), nota=meta.note)

        def done(self, idx, label, path, meta, tag_err):
            if meta.cover:
                job.capas[idx] = meta.cover
            self._envia(t="done", idx=idx, nome=path.name,
                        tamanho=path.stat().st_size, generos=meta.genres,
                        gravadora=meta.label, capa=bool(meta.cover),
                        nota=meta.note, erro_tags=tag_err)

        def summary(self, d):
            self._envia(
                t="summary", total=d["total"], dest=str(d["dest"]),
                dry_run=d["dry_run"], puladas=len(d["skipped"]),
                repetidas=len(d["dups"]), falhas=len(d["failed"]),
                baixadas=len(d["novas"]), enriquecidas=d["rich"],
                sem_enriquecimento=[{"idx": i, "label": l,
                                     "nota": m.note or "sem dados"}
                                    for i, l, m, _ in d["faltando"]],
                falhou=[{"idx": i, "label": l, "motivo": mo}
                        for i, l, mo in d["failed"]],
            )

    return WebEmitter()


def montar_args(ytmdl, corpo: dict) -> argparse.Namespace:
    """Traduz o formulario da pagina nas opcoes que o run_job espera."""
    destino = corpo.get("dest") or str(RAIZ / "Downloads")
    return argparse.Namespace(
        index=corpo.get("index"),
        url=corpo["url"].strip(),
        dest=Path(destino).expanduser(),
        folder=(corpo.get("folder") or "").strip() or None,
        format=corpo.get("format") or "flac",
        bitrate=corpo.get("bitrate") or None,
        bits=int(corpo.get("bits") or 16),
        rate=int(corpo["rate"]) if corpo.get("rate") else None,
        limit=int(corpo["limit"]) if corpo.get("limit") else None,
        jobs=4,
        cookies=(corpo.get("cookies") or "").strip() or None,
        force=bool(corpo.get("force")),
        no_dedup=bool(corpo.get("no_dedup")),
        no_enrich=bool(corpo.get("no_enrich")),
        no_cover=bool(corpo.get("no_cover")),
        tolerance=ytmdl.DURATION_TOLERANCE,
        loose=bool(corpo.get("loose")),
        dry_run=bool(corpo.get("dry_run")),
    )


def rodar_job(job: Job, corpo: dict) -> None:
    try:
        ff = ffmpeg_path()
        if ff:
            os.environ["YTMDL_FFMPEG"] = ff
        ytmdl = carregar_ytmdl()
        job.corpo = corpo
        args = montar_args(ytmdl, corpo)
        ytmdl.run_job(args, criar_emissor(ytmdl, job))
    except KeyboardInterrupt:
        job.eventos.put({"t": "cancelado"})
    except SystemExit as exc:               # probe() aborta assim
        job.eventos.put({"t": "erro", "msg": str(exc)})
    except Exception:
        job.eventos.put({"t": "erro", "msg": traceback.format_exc(limit=3)})
    finally:
        job.eventos.put(None)


# --------------------------------------------------------------- utilidades ---


def escolher_pasta() -> str | None:
    """Abre o seletor de pastas nativo do macOS."""
    r = subprocess.run(
        ["osascript", "-e",
         'POSIX path of (choose folder with prompt "Onde salvar as musicas?")'],
        capture_output=True, text=True)
    return r.stdout.strip() or None


# ------------------------------------------------------------------ servidor ---


class Handler(BaseHTTPRequestHandler):
    server_version = "ytm-dl"

    def log_message(self, *a):        # sem ruido no terminal
        pass

    # -- respostas ---------------------------------------------------------
    def _json(self, dados: Any, status: int = 200):
        corpo = json.dumps(dados).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _bytes(self, corpo: bytes, tipo: str):
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _sse_abre(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def _sse(self, ev: dict):
        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
        self.wfile.flush()

    # -- rotas -------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            return self._bytes((WEB / "index.html").read_bytes(),
                               "text/html; charset=utf-8")

        if u.path == "/api/config":
            ytmdl = carregar_ytmdl()
            return self._json({
                "formatos": [{"id": k, "ext": v.ext, "lossless": v.lossless,
                              "nota": v.nota, "quality": v.quality}
                             for k, v in ytmdl.FORMATS.items()],
                "destino_padrao": str(RAIZ / "Downloads"),
                "ffmpeg": ffmpeg_path(),
            })

        if u.path == "/api/eventos":
            job = JOBS.get(q.get("job", [""])[0])
            if not job:
                return self._json({"erro": "job desconhecido"}, 404)
            self._sse_abre()
            try:
                while (ev := job.eventos.get()) is not None:
                    self._sse(ev)
                self._sse({"t": "fim"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if u.path == "/api/capa":
            job = JOBS.get(q.get("job", [""])[0])
            idx = int(q.get("idx", ["0"])[0])
            if job and idx in job.capas:
                return self._bytes(job.capas[idx], "image/jpeg")
            return self._json({"erro": "sem capa"}, 404)

        if u.path == "/api/ffmpeg/instalar":
            self._sse_abre()
            try:
                def progresso(feito, total):
                    self._sse({"t": "baixando", "feito": feito, "total": total})
                versao = instalar_ffmpeg(progresso)
                self._sse({"t": "pronto", "versao": versao})
            except Exception as exc:
                self._sse({"t": "erro", "msg": str(exc)})
            return

        return self._json({"erro": "nao encontrado"}, 404)

    def do_POST(self):
        u = urlparse(self.path)

        if u.path == "/api/job":
            corpo = self._corpo_json()
            if not (corpo.get("url") or "").strip():
                return self._json({"erro": "informe o link da playlist"}, 400)
            jid = uuid.uuid4().hex[:12]
            job = Job(jid)
            JOBS[jid] = job
            threading.Thread(target=rodar_job, args=(job, corpo),
                             daemon=True).start()
            return self._json({"job": jid})

        if u.path == "/api/retentar":
            dados = self._corpo_json()
            origem = JOBS.get(dados.get("job", ""))
            idx = int(dados.get("idx") or 0)
            if not origem or idx not in origem.urls:
                return self._json({"erro": "faixa desconhecida"}, 404)

            # Reaproveita as opcoes do job original, trocando a playlist pela
            # faixa unica. O destino vai como caminho absoluto em `folder`
            # para que a pasta continue a mesma: resolvido pelo titulo, um
            # link de faixa unica criaria uma pasta com o nome da musica.
            # `force` fica de fora de proposito — ele apaga o historico da
            # playlist inteira, e aqui so queremos uma faixa de volta.
            corpo = dict(origem.corpo)
            corpo.update(url=origem.urls[idx], folder=origem.destino,
                         index=idx, limit=None, force=False)
            jid = uuid.uuid4().hex[:12]
            novo = Job(jid)
            JOBS[jid] = novo
            threading.Thread(target=rodar_job, args=(novo, corpo),
                             daemon=True).start()
            return self._json({"job": jid})

        if u.path == "/api/cancelar":
            job = JOBS.get(self._corpo_json().get("job", ""))
            if job:
                job.cancelar.set()
            return self._json({"ok": True})

        if u.path == "/api/pasta":
            return self._json({"pasta": escolher_pasta()})

        if u.path == "/api/abrir":
            caminho = self._corpo_json().get("caminho") or ""
            if caminho and Path(caminho).exists():
                subprocess.run(["open", caminho])
            return self._json({"ok": True})

        return self._json({"erro": "nao encontrado"}, 404)


def porta_livre(preferida: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferida))
            return preferida
        except OSError:
            pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    p = argparse.ArgumentParser(description="Interface web local do ytm-dl")
    p.add_argument("--port", type=int, default=8756)
    p.add_argument("--open", action="store_true",
                   help="abre o navegador ao subir")
    args = p.parse_args()

    mimetypes.init()
    porta = porta_livre(args.port)
    url = f"http://127.0.0.1:{porta}/"
    srv = ThreadingHTTPServer(("127.0.0.1", porta), Handler)
    print(f"ytm-dl: {url}")
    if not ffmpeg_path():
        print("aviso: ffmpeg ausente — a pagina oferece o download na abertura")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
