#!/usr/bin/env python3
"""
ytm-dl.py — baixa playlists do YouTube Music em FLAC (ou WAV) com metadados
completos: capa embutida em alta resolucao, genero, gravadora e ano.

O YouTube nao expoe genero nem capa de album (so o thumbnail do video), entao
os dados que faltam vem do Deezer, cuja API publica nao exige chave. Cada
resultado so e aceito se a duracao bater com a faixa baixada, para nunca
etiquetar uma faixa com os dados de outra.

Uso:  ytm-dl.py [opcoes] <link do music.youtube.com>
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import yt_dlp
    from mutagen import File as MutagenFile
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.wave import WAVE
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"erro: dependencia faltando ({exc.name}).\n"
        f"use o venv do projeto:\n"
        f"  {Path(__file__).parent}/.venv/bin/python {Path(__file__).name} <url>\n"
        f"ou recrie-o:\n"
        f"  python3 -m venv .venv && ./.venv/bin/pip install yt-dlp mutagen"
    )

DEEZER_API = "https://api.deezer.com"
UA = "ytm-dl/2.0 (uso pessoal)"
DURATION_TOLERANCE = 3  # segundos, ao casar no Deezer
DUP_TOLERANCE = 5       # segundos, ao considerar duas faixas a mesma musica
NET_TIMEOUT = 15


@dataclass(frozen=True)
class Fmt:
    """Como produzir e etiquetar um formato de saida."""

    ext: str        # extensao em disco
    codec: str      # o que pedir ao FFmpegExtractAudio do yt-dlp
    lossless: bool
    tagger: str     # 'vorbis' | 'id3' | 'mp4'
    quality: str = ""   # kbps, so para os com perdas
    nota: str = ""


FORMATS: dict[str, Fmt] = {
    # sem perdas
    "flac": Fmt("flac", "flac", True, "vorbis",
                nota="padrao: sem perdas, tags e capa nativas"),
    "alac": Fmt("m4a", "alac", True, "mp4",
                nota="sem perdas da Apple; toca no Music.app"),
    "wav":  Fmt("wav", "wav", True, "id3",
                nota="PCM cru; capa so via chunk ID3, suporte irregular"),
    "aiff": Fmt("aiff", "wav", True, "id3",
                nota="PCM big-endian; formato preferido do Rekordbox"),
    # com perdas
    "mp3":  Fmt("mp3", "mp3", False, "id3", "320",
                nota="320 kbps; o mais compativel que existe"),
    "m4a":  Fmt("m4a", "m4a", False, "mp4", "256",
                nota="AAC 256 kbps"),
    "opus": Fmt("opus", "opus", False, "vorbis",
                nota="o proprio audio do YouTube, sem reconversao"),
}

AUDIO_EXTS = tuple(sorted({f".{f.ext}" for f in FORMATS.values()}))

# --------------------------------------------------------------------- util ---


def norm(s: str) -> str:
    """Minusculas, sem acento e sem pontuacao — para comparar titulos."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def sanitize(name: str) -> str:
    """Nome de pasta seguro no macOS."""
    name = re.sub(r"[/:\\]", "-", name or "")
    name = "".join(c for c in name if ord(c) >= 32)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name or "Playlist sem nome"


def human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} GB"


# ------------------------------------------------------------------ metadata ---


@dataclass
class Meta:
    """Metadados de uma faixa, montados a partir do YouTube + Deezer."""

    title: str = ""
    artist: str = ""
    album: str = ""
    albumartist: str = ""
    date: str = ""
    genres: list[str] = field(default_factory=list)
    label: str = ""
    cover: bytes = b""
    cover_dim: str = ""
    enriched: bool = False
    note: str = ""


def deezer(path: str, **params: Any) -> dict | None:
    """GET na API do Deezer. Devolve None em qualquer falha (nunca levanta)."""
    url = f"{DEEZER_API}/{path}"
    if params:
        url += "?" + urlencode(params)
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=NET_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and "error" in data:
                return None
            return data
        except (URLError, HTTPError, json.JSONDecodeError, TimeoutError):
            if attempt < 2:
                time.sleep(1 + attempt)
    return None


def fetch(url: str) -> bytes:
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=NET_TIMEOUT) as r:
            return r.read()
    except (URLError, HTTPError, TimeoutError):
        return b""


def deezer_candidates(meta: "Meta") -> list[dict]:
    """
    Junta candidatos de varias buscas.

    Uma query so nao basta: a sintaxe estrita (artist:"..." track:"...") falha
    com nomes que o Deezer grafa diferente, e a busca frouxa as vezes ranqueia
    remixes acima da versao de album. Consultando as duas e unindo os
    resultados, o casamento por duracao escolhe a certa entre elas.
    """
    queries: list[str] = []
    if meta.artist and meta.title:
        queries.append(f'artist:"{meta.artist}" track:"{meta.title}"')
        queries.append(f"{meta.artist} {meta.title}")
    if meta.title and meta.album and norm(meta.album) != norm(meta.title):
        queries.append(f"{meta.artist} {meta.album}".strip())
    if meta.title:
        queries.append(meta.title)

    seen: set[int] = set()
    pool: list[dict] = []
    for q in queries:
        data = deezer("search", q=q, limit=10)
        for r in (data or {}).get("data", []):
            rid = r.get("id")
            if rid and rid not in seen:
                seen.add(rid)
                pool.append(r)
        if len(pool) >= 25:
            break
    return pool


def name_matches(r: dict, nt: str, na: str, strict: bool = True) -> bool:
    """
    Casamento por nome.

    Em modo estrito exige titulo E artista. So o titulo nao serve: coletaneas
    de cover ("EDM Hits on Piano") trazem o mesmo titulo com duracao parecida
    e etiquetariam a faixa com o album errado.
    """
    rt, ra = norm(r.get("title", "")), norm(r.get("artist", {}).get("name", ""))
    title_ok = bool(rt and nt) and (rt in nt or nt in rt)
    artist_ok = bool(ra and na) and (ra in na or na in ra)
    if strict and na:
        return title_ok and artist_ok
    return title_ok or artist_ok


def pick_match(
    results: list[dict], title: str, artist: str, duration: float,
    tolerance: int, loose: bool,
) -> tuple[dict | None, str]:
    """
    Escolhe o resultado que corresponde a faixa baixada.

    Por padrao exige duracao dentro da tolerancia E semelhanca de nome: sem
    isso uma busca malsucedida etiquetaria a faixa com dados de outra versao —
    pior que ficar sem metadata. Com --loose, aceita so a semelhanca de nome e
    marca o resultado como aproximado.
    """
    nt, na = norm(title), norm(artist)
    named = [r for r in results if name_matches(r, nt, na, strict=True)]
    if not named and loose:
        named = [r for r in results if name_matches(r, nt, na, strict=False)]

    best, best_delta = None, None
    for r in named:
        delta = abs(r.get("duration", 0) - duration)
        if delta > tolerance:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = r, delta
    if best:
        return best, ""

    if loose and named:
        best = min(named, key=lambda r: abs(r.get("duration", 0) - duration))
        delta = abs(best.get("duration", 0) - duration)
        return best, f"aproximado ({best.get('duration')}s vs {duration:.0f}s)"

    return None, ""


_album_cache: dict[int, dict] = {}


def enrich(meta: Meta, duration: float, want_cover: bool,
           tolerance: int, loose: bool) -> Meta:
    """Completa `meta` com genero, gravadora e capa vindos do Deezer."""
    pool = deezer_candidates(meta)
    if not pool:
        meta.note = "Deezer: sem resultado"
        return meta

    hit, approx = pick_match(pool, meta.title, meta.artist, duration, tolerance, loose)
    if not hit:
        meta.note = (
            f"Deezer: {len(pool)} resultado(s), nenhum com duracao ~{duration:.0f}s"
            " (use --loose para aceitar aproximado)"
        )
        return meta

    album_id = hit.get("album", {}).get("id")
    album = _album_cache.get(album_id) if album_id else None
    if album is None and album_id:
        album = deezer(f"album/{album_id}") or {}
        _album_cache[album_id] = album
    album = album or {}

    genres = [g["name"] for g in album.get("genres", {}).get("data", []) if g.get("name")]
    if genres:
        meta.genres = genres
    if album.get("label"):
        meta.label = album["label"]
    if album.get("release_date"):
        meta.date = album["release_date"]
    if album.get("title"):
        meta.album = album["title"]
    if album.get("artist", {}).get("name"):
        meta.albumartist = album["artist"]["name"]

    if want_cover:
        for key in ("cover_xl", "cover_big", "cover_medium"):
            if album.get(key):
                meta.cover = fetch(album[key])
                if meta.cover:
                    m = re.search(r"/(\d+)x(\d+)", album[key])
                    meta.cover_dim = f"{m.group(1)}x{m.group(2)}" if m else "?"
                break

    meta.enriched = bool(meta.genres or meta.cover)
    if approx:
        meta.note = approx
    elif not meta.enriched:
        meta.note = "Deezer: faixa encontrada, mas sem genero/capa no album"
    return meta


# ------------------------------------------------------------------ tagging ---


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Largura/altura lidas dos marcadores SOFn do JPEG (0x0 se nao achar)."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                      0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return 0, 0


def _picture(cover: bytes) -> Picture:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = cover
    pic.width, pic.height = _jpeg_size(cover)
    pic.depth = 24
    return pic


def tag_vorbis(path: Path, meta: Meta, index: int, fmt: str) -> None:
    """FLAC e Opus: comentarios Vorbis."""
    a = FLAC(str(path)) if fmt == "flac" else OggOpus(str(path))
    a["title"] = meta.title
    a["artist"] = meta.artist
    a["album"] = meta.album or meta.title
    a["tracknumber"] = str(index)
    if meta.albumartist:
        a["albumartist"] = meta.albumartist
    if meta.date:
        a["date"] = meta.date
    if meta.genres:
        a["genre"] = meta.genres
    if meta.label:
        a["organization"] = meta.label
    if meta.cover:
        pic = _picture(meta.cover)
        if fmt == "flac":
            a.clear_pictures()
            a.add_picture(pic)
        else:
            # Opus nao tem bloco de imagem proprio: vai um Picture do FLAC
            # serializado em base64, que e a convencao do formato.
            a["metadata_block_picture"] = [
                base64.b64encode(pic.write()).decode("ascii")
            ]
    a.save()


def tag_id3(path: Path, meta: Meta, index: int, fmt: str) -> None:
    """MP3, WAV e AIFF: ID3v2. Nos dois ultimos e um chunk dentro do RIFF/IFF."""
    opener = {"mp3": MP3, "wav": WAVE, "aiff": AIFF}[fmt]
    a = opener(str(path))
    if a.tags is None:
        a.add_tags()
    a.tags.delall("APIC")
    a.tags.add(TIT2(encoding=3, text=meta.title))
    a.tags.add(TPE1(encoding=3, text=meta.artist))
    a.tags.add(TALB(encoding=3, text=meta.album or meta.title))
    a.tags.add(TRCK(encoding=3, text=str(index)))
    if meta.albumartist:
        a.tags.add(TPE2(encoding=3, text=meta.albumartist))
    if meta.date:
        a.tags.add(TDRC(encoding=3, text=meta.date))
    if meta.genres:
        a.tags.add(TCON(encoding=3, text="; ".join(meta.genres)))
    if meta.cover:
        a.tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                        desc="Cover", data=meta.cover))
    a.save()


def tag_mp4(path: Path, meta: Meta, index: int, fmt: str) -> None:
    """ALAC e AAC: atomos do container MP4."""
    a = MP4(str(path))
    a["\xa9nam"] = [meta.title]
    a["\xa9ART"] = [meta.artist]
    a["\xa9alb"] = [meta.album or meta.title]
    a["trkn"] = [(index, 0)]
    if meta.albumartist:
        a["aART"] = [meta.albumartist]
    if meta.date:
        a["\xa9day"] = [meta.date]
    if meta.genres:
        a["\xa9gen"] = ["; ".join(meta.genres)]
    if meta.cover:
        a["covr"] = [MP4Cover(meta.cover, imageformat=MP4Cover.FORMAT_JPEG)]
    a.save()


TAGGERS = {"vorbis": tag_vorbis, "id3": tag_id3, "mp4": tag_mp4}


def tag_file(path: Path, meta: Meta, index: int, fmt: str) -> None:
    TAGGERS[FORMATS[fmt].tagger](path, meta, index, fmt)


def to_aiff(src: Path, bits: int, rate: int | None) -> Path:
    """
    Converte o WAV recem-gerado em AIFF.

    O yt-dlp nao produz AIFF (aceita aac, alac, flac, m4a, mp3, opus, vorbis e
    wav), entao pedimos WAV e trocamos o container aqui. AIFF usa PCM
    big-endian — dai o `be` no codec.
    """
    out = src.with_suffix(".aiff")
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src),
           "-acodec", "pcm_s16be" if bits == 16 else "pcm_s24be", "-ac", "2"]
    if rate:
        cmd += ["-ar", str(rate)]
    cmd.append(str(out))
    subprocess.run(cmd, check=True, capture_output=True)
    src.unlink()
    return out


# ----------------------------------------------------------------- download ---


# ------------------------------------------------- verificacao da pasta ---


def scan_dest(dest: Path, ext: str) -> dict[int, Path]:
    """
    Mapeia `indice -> arquivo` do que ja existe na pasta de destino.

    Filtra pelo formato pedido de proposito: um WAV na pasta nao significa que
    o FLAC ja existe, e tratar os dois como equivalentes faria o script pular
    faixas que voce acabou de pedir em outro formato.
    """
    found: dict[int, Path] = {}
    if not dest.is_dir():
        return found
    for f in sorted(dest.iterdir()):
        if not f.is_file() or f.suffix.lower() != f".{ext.lower()}":
            continue
        m = re.match(r"(\d+)\s*-\s*", f.name)
        if m:
            found.setdefault(int(m.group(1)), f)
    return found


def scan_other_formats(dest: Path, ext: str) -> dict[int, Path]:
    """Mesmo indice, porem em outro formato de audio."""
    found: dict[int, Path] = {}
    for other in AUDIO_EXTS:
        if other == f".{ext.lower()}":
            continue
        for k, v in scan_dest(dest, other.lstrip(".")).items():
            found.setdefault(k, v)
    return found


def read_archive(archive: Path) -> set[str]:
    """Ids ja registrados no historico ('youtube <id>' por linha)."""
    ids: set[str] = set()
    if archive.is_file():
        for line in archive.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                ids.add(parts[1])
    return ids


@dataclass
class LibEntry:
    """Uma faixa que ja esta na pasta, identificada pelo conteudo."""

    path: Path
    title: str
    artist: str
    duration: float


def read_basic_tags(path: Path) -> tuple[str, str, float]:
    """
    Titulo, artista e duracao de qualquer um dos formatos suportados.

    A interface `easy` do mutagen cobre FLAC, MP3, MP4 e Opus, mas nao WAV nem
    AIFF: nesses dois ela devolve os frames ID3 crus, e `get("title")` volta
    vazio. Dai a leitura direta de TIT2/TPE1 como reserva — sem ela o dedupe
    perderia o artista justamente nos formatos de PCM.
    """
    title = artist = ""
    duration = 0.0
    try:
        easy = MutagenFile(str(path), easy=True)
        if easy is not None:
            title = (easy.get("title") or [""])[0]
            artist = (easy.get("artist") or [""])[0]
            duration = float(easy.info.length)
    except Exception:
        pass
    if not title or not artist:
        try:
            audio = MutagenFile(str(path))
            tags = getattr(audio, "tags", None)
            if tags is not None and hasattr(tags, "getall"):
                if not title:
                    fr = tags.getall("TIT2")
                    title = fr[0].text[0] if fr else ""
                if not artist:
                    fr = tags.getall("TPE1")
                    artist = fr[0].text[0] if fr else ""
            if not duration and audio is not None:
                duration = float(audio.info.length)
        except Exception:
            pass
    return title, artist, duration


def index_library(dest: Path, ext: str) -> list[LibEntry]:
    """
    Le titulo/artista/duracao de tudo que ja existe na pasta.

    Le as tags, nao o nome do arquivo: ao juntar varias playlists numa
    coletanea, a mesma musica chega com numeracao e ate nome diferentes.
    """
    out: list[LibEntry] = []
    if not dest.is_dir():
        return out
    for f in sorted(dest.iterdir()):
        if not f.is_file() or f.suffix.lower() != f".{ext.lower()}":
            continue
        title, artist, duration = read_basic_tags(f)
        if not title:  # sem tags legiveis, o nome do arquivo serve
            title = re.sub(r"^\d+\s*-\s*", "", f.stem)
        out.append(LibEntry(f, title, artist, duration))
    return out


def find_duplicate(
    library: list[LibEntry], title: str, artist: str, duration: float,
) -> LibEntry | None:
    """
    Acha a mesma musica ja presente na pasta.

    Titulo semelhante E duracao proxima. A duracao e o que separa a faixa de
    uma extended mix ou de um remix homonimo, que sao arquivos diferentes e
    voce provavelmente quer os dois. O artista, quando conhecido dos dois
    lados, precisa bater tambem — senao covers seriam descartados como copia.
    """
    nt, na = norm(title), norm(artist)
    if not nt:
        return None
    for e in library:
        et, ea = norm(e.title), norm(e.artist)
        if not (et and (et in nt or nt in et)):
            continue
        if duration and e.duration and abs(e.duration - duration) > DUP_TOLERANCE:
            continue
        if na and ea and not (ea in na or na in ea):
            continue
        return e
    return None


def titles_match(path: Path, title: str) -> bool:
    """O arquivo no indice N e mesmo esta faixa? (playlist pode ter mudado)"""
    if not title:
        return True
    stem = re.sub(r"^\d+\s*-\s*", "", path.stem)
    a, b = norm(stem), norm(title)
    return bool(a and b) and (a in b or b in a)


def _has_cover(audio: Any) -> bool:
    """Capa embutida, em qualquer um dos quatro esquemas que usamos."""
    if audio is None:
        return False
    if getattr(audio, "pictures", None):        # FLAC
        return True
    tags = getattr(audio, "tags", None)
    if tags is None:
        return False
    try:
        if hasattr(tags, "getall") and tags.getall("APIC"):   # ID3
            return True
    except Exception:
        pass
    try:
        return bool(tags.get("covr") or tags.get("metadata_block_picture"))
    except Exception:
        return False


def _read_genre(audio: Any) -> str:
    tags = getattr(audio, "tags", None)
    if tags is None:
        return ""
    try:
        if hasattr(tags, "getall"):
            frames = tags.getall("TCON")                      # ID3
            if frames:
                return "; ".join(frames[0].text)
    except Exception:
        pass
    for key in ("genre", "\xa9gen"):                          # Vorbis / MP4
        try:
            v = tags.get(key)
        except Exception:
            v = None
        if v:
            return "; ".join(str(x) for x in v)
    return ""


def inspect_existing(path: Path) -> str:
    """Resumo do que o arquivo ja carrega — so leitura local, sem rede."""
    try:
        audio = MutagenFile(str(path))
    except Exception:
        return "nao consegui ler as tags"
    if audio is None:
        return "formato nao reconhecido"
    genre = _read_genre(audio)
    return f"{'capa' if _has_cover(audio) else 'sem capa'} | {genre or 'sem genero'}"


@dataclass
class LibEntry:
    """Uma faixa que ja esta na pasta, identificada pelo conteudo."""

    path: Path
    title: str
    artist: str
    duration: float


def index_library(dest: Path, ext: str) -> list[LibEntry]:
    """
    Le titulo/artista/duracao de tudo que ja existe na pasta.

    Le as tags, nao o nome do arquivo: ao juntar varias playlists numa
    coletanea, a mesma musica chega com numeracao e ate nome diferentes.
    """
    out: list[LibEntry] = []
    if not dest.is_dir():
        return out
    for f in sorted(dest.iterdir()):
        if not f.is_file() or f.suffix.lower() != f".{ext.lower()}":
            continue
        title = artist = ""
        duration = 0.0
        try:
            if f.suffix.lower() == ".flac":
                a = FLAC(str(f))
                title = (a.get("title") or [""])[0]
                artist = (a.get("artist") or [""])[0]
                duration = float(a.info.length)
            else:
                w = WAVE(str(f))
                if w.tags:
                    tit, art = w.tags.getall("TIT2"), w.tags.getall("TPE1")
                    title = tit[0].text[0] if tit else ""
                    artist = art[0].text[0] if art else ""
                duration = float(w.info.length)
        except Exception:
            pass
        if not title:  # sem tags legiveis, o nome do arquivo serve
            title = re.sub(r"^\d+\s*-\s*", "", f.stem)
        out.append(LibEntry(f, title, artist, duration))
    return out


def find_duplicate(
    library: list[LibEntry], title: str, artist: str, duration: float,
) -> LibEntry | None:
    """
    Acha a mesma musica ja presente na pasta.

    Titulo semelhante E duracao proxima. A duracao e o que separa a faixa de
    uma extended mix ou de um remix homonimo, que sao arquivos diferentes e
    voce provavelmente quer os dois. O artista, quando conhecido dos dois
    lados, precisa bater tambem — senao covers seriam descartados como copia.
    """
    nt, na = norm(title), norm(artist)
    if not nt:
        return None
    for e in library:
        et, ea = norm(e.title), norm(e.artist)
        if not (et and (et in nt or nt in et)):
            continue
        if duration and e.duration and abs(e.duration - duration) > DUP_TOLERANCE:
            continue
        if na and ea and not (ea in na or na in ea):
            continue
        return e
    return None


def titles_match(path: Path, title: str) -> bool:
    """O arquivo no indice N e mesmo esta faixa? (playlist pode ter mudado)"""
    if not title:
        return True
    stem = re.sub(r"^\d+\s*-\s*", "", path.stem)
    a, b = norm(stem), norm(title)
    return bool(a and b) and (a in b or b in a)


def inspect_existing(path: Path) -> str:
    """Resumo do que o arquivo ja carrega — so leitura local, sem rede."""
    try:
        if path.suffix.lower() == ".flac":
            a = FLAC(str(path))
            genre = "; ".join(a.get("genre", []))
            cover = bool(a.pictures)
        else:
            w = WAVE(str(path))
            tags = w.tags
            tcon = tags.getall("TCON") if tags else []
            genre = "; ".join(tcon[0].text) if tcon else ""
            cover = bool(tags and tags.getall("APIC"))
    except Exception:
        return "nao consegui ler as tags"
    return f"{'capa' if cover else 'sem capa'} | {genre or 'sem genero'}"


def probe(url: str, opts: dict) -> tuple[str, list[dict]]:
    """Le a playlist sem baixar nada. Levanta SystemExit com dica se falhar."""
    with yt_dlp.YoutubeDL({**opts, "extract_flat": "in_playlist", "quiet": True}) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            sys.exit(
                f"erro: nao consegui ler essa playlist.\n  {exc}\n\n"
                "verifique:\n"
                "  - o link esta completo? o id depois de 'list=' costuma ter ~34 caracteres\n"
                "  - a playlist e publica? se for privada, use -c <navegador>"
            )
    if not info:
        sys.exit("erro: a playlist voltou vazia.")
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        entries = [info]  # link de faixa unica
    return info.get("title") or "Playlist sem nome", entries


def main() -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(
        prog="ytm-dl.py",
        description="Baixa playlists do YouTube Music com metadados completos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exemplos:\n"
            "  ytm-dl.py 'https://music.youtube.com/playlist?list=PLxxxx'\n"
            "  ytm-dl.py -n 3 --dry-run 'https://music.youtube.com/playlist?list=PLxxxx'\n"
            "  ytm-dl.py --format wav -c chrome 'https://music.youtube.com/playlist?list=LM'\n"
        ),
    )
    p.add_argument("url", help="link da playlist ou faixa do music.youtube.com")
    p.add_argument("-o", "--dest", type=Path, default=here / "Downloads",
                   help="pasta base de destino (padrao: <pasta do script>/Downloads)")
    p.add_argument("--folder", metavar="PASTA",
                   help="baixa para ESTA pasta em vez de criar uma com o nome da"
                        " playlist. Aceita nome (criado sob --dest) ou caminho"
                        " absoluto. Use para juntar varios albuns numa coletanea:"
                        " a numeracao continua de onde parou e faixas repetidas"
                        " entre playlists sao detectadas e puladas")
    p.add_argument("--no-dedup", action="store_true",
                   help="nao verifica se a musica ja existe na pasta por"
                        " titulo/artista/duracao (so pela posicao)")
    p.add_argument("-n", "--limit", type=int, metavar="N",
                   help="baixa so as N primeiras faixas")
    p.add_argument("-j", "--jobs", type=int, default=4,
                   help="fragmentos simultaneos por faixa (padrao: 4)")
    p.add_argument("-c", "--cookies", metavar="NAV",
                   help="ler cookies do navegador (chrome, brave, firefox, safari, edge)")
    p.add_argument("-f", "--force", action="store_true",
                   help="ignora o historico e rebaixa tudo")
    p.add_argument("--format", choices=tuple(FORMATS), default="flac",
                   metavar="FMT",
                   help="formato de saida (padrao: flac). Opcoes: "
                        + ", ".join(f"{k} ({v.nota})" for k, v in FORMATS.items()))
    p.add_argument("--bitrate", metavar="K",
                   help="kbps para os formatos com perdas"
                        " (padrao: 320 no mp3, 256 no m4a)")
    p.add_argument("--bits", type=int, choices=(16, 24), default=16,
                   help="profundidade de bits (padrao: 16)")
    p.add_argument("--rate", type=int, metavar="HZ",
                   help="reamostra para HZ (padrao: mantem os 48000 Hz da fonte,"
                        " evitando uma conversao sem beneficio)")
    p.add_argument("--no-enrich", action="store_true",
                   help="nao consulta o Deezer; usa so o que o YouTube fornece")
    p.add_argument("--no-cover", action="store_true", help="nao embute capa")
    p.add_argument("--tolerance", type=int, default=DURATION_TOLERANCE, metavar="S",
                   help=f"diferenca de duracao aceita ao casar no Deezer, em segundos"
                        f" (padrao: {DURATION_TOLERANCE})")
    p.add_argument("--loose", action="store_true",
                   help="aceita casamento so por nome quando a duracao nao bate;"
                        " marca a faixa como aproximada no relatorio")
    p.add_argument("--dry-run", action="store_true",
                   help="mostra o que seria baixado e etiquetado, sem baixar")
    args = p.parse_args()

    spec = FORMATS[args.format]
    base_opts: dict[str, Any] = {"quiet": True, "no_warnings": True}
    if args.cookies:
        base_opts["cookiesfrombrowser"] = (args.cookies,)

    print("-> lendo a playlist...")
    playlist_title, entries = probe(args.url, base_opts)
    if args.limit:
        entries = entries[: args.limit]

    if args.folder:
        folder = Path(args.folder).expanduser()
        dest = folder if folder.is_absolute() else args.dest / sanitize(args.folder)
    else:
        dest = args.dest / sanitize(playlist_title)
    pooling = bool(args.folder)
    archive = dest / ".ytm-dl-baixados.txt"
    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        if args.force and archive.exists():
            archive.unlink()

    print(f"-> playlist : {playlist_title}")
    print(f"-> faixas   : {len(entries)}")
    print(f"-> destino  : {dest}")
    if spec.lossless:
        detalhe = (f"{args.bits} bits {args.rate or 48000} Hz"
                   if spec.codec in ("wav", "flac", "alac") else "")
    else:
        detalhe = (f"{args.bitrate or spec.quality} kbps" if spec.quality
                   else "copia direta da fonte")
    print(f"-> formato  : .{spec.ext} ({args.format}) {detalhe}"
          f"{'' if args.no_enrich else ' + metadados do Deezer'}")
    if args.dry_run:
        print("-> DRY RUN  : nada sera baixado")
    print()

    report: list[tuple[int, str, Meta | None, str]] = []
    skipped: list[tuple[int, str, Path, str]] = []
    dups: list[tuple[int, str, Path]] = []
    failed: list[tuple[int, str, str]] = []

    # Verificacao previa: o que ja esta na pasta nao volta a ser baixado nem
    # consultado no Deezer — vale inclusive para --dry-run.
    present = scan_dest(dest, spec.ext)
    outro_present = scan_other_formats(dest, spec.ext)
    archive_ids = set() if args.force else read_archive(archive)
    library = [] if args.no_dedup else index_library(dest, spec.ext)

    # Numa pasta compartilhada a posicao na playlist nao serve de nome: a
    # numeracao continua de onde a pasta parou.
    next_num = (max(present) if present else 0) + 1

    if present:
        print(f"-> ja na pasta: {len(present)} arquivo(s) .{spec.ext}"
              + (f", numerando a partir de {next_num:02d}" if pooling else ""))
        print()

    for idx, entry in enumerate(entries, start=1):
        video_url = entry.get("url") or entry.get("webpage_url") or entry.get("id")
        vid = entry.get("id") or ""
        label = entry.get("title") or video_url
        print(f"[{idx:02d}/{len(entries):02d}] {label}")

        existing = None if pooling else present.get(idx)
        file_ok = (
            existing is not None
            and existing.stat().st_size > 0
            and titles_match(existing, entry.get("title") or "")
        )
        in_archive = bool(vid) and vid in archive_ids

        if file_ok and not args.force:
            estado = inspect_existing(existing)
            print(f"        = ja baixada: {existing.name}"
                  f" ({human(existing.stat().st_size)}) | {estado}\n")
            skipped.append((idx, label, existing, estado))
            continue

        # Dedupe por musica, nao por posicao: playlists diferentes repetem
        # faixas, e a mesma gravacao costuma ter outro id de video em cada uma.
        if not args.no_dedup and not args.force:
            dup = find_duplicate(
                library,
                entry.get("title") or "",
                entry.get("channel") or entry.get("uploader") or "",
                float(entry.get("duration") or 0),
            )
            if dup:
                print(f"        = ja existe na pasta: {dup.path.name}"
                      f" — pulando duplicata\n")
                dups.append((idx, label, dup.path))
                continue

        # Chegamos aqui sem achar arquivo correspondente. Se o id consta no
        # historico, o arquivo sumiu: deixar o archive valer faria o yt-dlp
        # pular para sempre uma faixa que nao existe mais em disco.
        orfa = in_archive
        if orfa:
            outro = outro_present.get(idx)
            if outro:
                print(f"        ! so existe em {outro.suffix} ({outro.name})"
                      f" — gerando .{spec.ext}")
            else:
                print("        ! no historico, mas ausente da pasta — rebaixando")

        out_idx = next_num if pooling else idx
        ydl_opts: dict[str, Any] = {
            **base_opts,
            "format": "bestaudio/best",
            "outtmpl": str(dest / f"{out_idx:02d} - %(title)s.%(ext)s"),
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": args.jobs,
            "ignoreerrors": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": spec.codec,
                "preferredquality": args.bitrate or spec.quality or "0",
            }],
        }
        pp_args: list[str] = []
        if spec.codec == "flac":
            # Sem fixar isto, o ffmpeg deduz 24 bits do float que o Opus
            # decodifica e o FLAC sai MAIOR que o WAV (31,5 vs 28,3 MB numa
            # faixa de 2:40) sem ganho algum: a fonte e lossy.
            pp_args += ["-sample_fmt", "s16" if args.bits == 16 else "s32"]
        elif spec.codec == "wav":
            pp_args += ["-acodec",
                        "pcm_s16le" if args.bits == 16 else "pcm_s24le", "-ac", "2"]
        elif spec.codec == "alac":
            # O -acodec explicito nao e redundante: o yt-dlp registra o alac com
            # acodec=None e depois sobrescreve more_opts com os args de
            # qualidade, descartando o proprio "-acodec alac". Sem isto o .m4a
            # sai como AAC lossy, com nome de formato sem perdas.
            pp_args += ["-acodec", "alac",
                        "-sample_fmt", "s16p" if args.bits == 16 else "s32p"]
        # Opus so existe a 48 kHz; reamostrar aqui so estragaria a copia direta.
        if args.rate and spec.codec != "opus":
            pp_args += ["-ar", str(args.rate)]
        ydl_opts["postprocessor_args"] = {"extractaudio": pp_args}
        if args.force or orfa:
            ydl_opts["overwrites"] = True
        else:
            ydl_opts["download_archive"] = str(archive)

        if args.dry_run:
            with yt_dlp.YoutubeDL({**base_opts, "quiet": True}) as ydl:
                info = ydl.extract_info(video_url, download=False)
        else:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)

        if not info:
            print("        x falhou ao baixar\n")
            failed.append((idx, label, "download falhou"))
            continue

        meta = Meta(
            title=info.get("track") or info.get("title") or "",
            artist=info.get("artist") or info.get("uploader") or "",
            album=info.get("album") or "",
            albumartist=info.get("album_artist") or "",
            date=str(info.get("release_date") or info.get("upload_date") or ""),
        )
        duration = float(info.get("duration") or 0)

        downloads = info.get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") else None

        if not args.dry_run and path is None:
            # O yt-dlp nao produziu arquivo. Antes isto virava "falhou ao
            # baixar", o que era falso: quase sempre a faixa ja estava em
            # disco. Conferimos a pasta antes de acusar qualquer coisa.
            achado = scan_dest(dest, spec.ext).get(idx)
            if achado:
                estado = inspect_existing(achado)
                print(f"        = ja baixada: {achado.name}"
                      f" ({human(achado.stat().st_size)}) | {estado}\n")
                skipped.append((idx, label, achado, estado))
            else:
                print("        x nao gerou arquivo (pos-processamento falhou)\n")
                failed.append((idx, label, "pos-processamento falhou"))
            continue

        if not args.no_enrich:
            meta = enrich(meta, duration, want_cover=not args.no_cover,
                          tolerance=args.tolerance, loose=args.loose)

        if args.dry_run:
            print(f"        titulo : {meta.title}")
            print(f"        artista: {meta.artist}")
            print(f"        album  : {meta.album or '(vazio)'}")
            print(f"        genero : {'; '.join(meta.genres) or '(vazio)'}")
            print(f"        capa   : {meta.cover_dim or '(nenhuma)'}")
            if meta.note:
                print(f"        nota   : {meta.note}")
            print()
            if pooling:
                next_num += 1
                library.append(LibEntry(dest / f"{out_idx:02d}", meta.title,
                                        meta.artist, duration))
            report.append((idx, label, meta, ""))
            continue

        if args.format == "aiff" and path.suffix.lower() == ".wav":
            try:
                path = to_aiff(path, args.bits, args.rate)
            except subprocess.CalledProcessError as exc:
                erro = (exc.stderr or b"").decode(errors="replace").strip()
                print(f"        x falhou ao converter para AIFF: {erro[:120]}\n")
                failed.append((idx, label, "conversao para AIFF falhou"))
                continue

        try:
            tag_file(path, meta, out_idx, args.format)
            tag_err = ""
        except Exception as exc:  # tagging nunca deve perder o download
            tag_err = f"tags falharam: {exc}"

        if pooling:
            next_num += 1
            library.append(LibEntry(path, meta.title, meta.artist, duration))

        size = human(path.stat().st_size)
        bits = [f"OK {size}"]
        if meta.genres:
            bits.append("; ".join(meta.genres))
        if meta.cover:
            bits.append(f"capa {meta.cover_dim}")
        if meta.label:
            bits.append(meta.label)
        print("        " + " | ".join(bits))
        if meta.note:
            print(f"        ! {meta.note}")
        if tag_err:
            print(f"        ! {tag_err}")
        print()
        report.append((idx, label, meta, tag_err))

    # ------------------------------------------------------------- resumo ---
    novas = [r for r in report if r[2] is not None]
    rich = [r for r in novas if r[2].enriched]

    print("=" * 62)
    print(f"{len(entries)} faixa(s) na playlist")
    if skipped:
        print(f"  {len(skipped):>3} ja estavam na pasta (puladas)")
    if dups:
        print(f"  {len(dups):>3} repetidas — a musica ja estava na pasta (puladas)")
    if novas:
        verbo = "seriam baixadas" if args.dry_run else "baixadas agora"
        extra = "" if args.no_enrich else f" | {len(rich)} com genero/capa do Deezer"
        print(f"  {len(novas):>3} {verbo}{extra}")
    if failed:
        print(f"  {len(failed):>3} falharam")
    if not skipped and not novas and not failed and not dups:
        print("  nada a fazer")

    if dups:
        print("\nrepetidas (mesma musica ja presente na pasta):")
        for idx, label, path in dups:
            print(f"  {idx:02d} - {label}  ->  {path.name}")

    incompletas = [s_ for s_ in skipped if "sem capa" in s_[3] or "sem genero" in s_[3]]
    if incompletas:
        print("\nja na pasta, porem com metadata incompleta:")
        for idx, label, _path, estado in incompletas:
            print(f"  {idx:02d} - {label}: {estado}")
        print("  para reetiquetar, apague esses arquivos e rode de novo (ou use -f)")

    faltando = [r for r in novas if not r[2].enriched]
    if faltando:
        print("\nsem enriquecimento:")
        for idx, label, meta, _ in faltando:
            print(f"  {idx:02d} - {label}: {meta.note or 'sem dados'}")

    if failed:
        print("\nfalharam:")
        for idx, label, motivo in failed:
            print(f"  {idx:02d} - {label}: {motivo}")

    if not args.dry_run:
        print(f"\ndestino: {dest}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrompido — rode de novo para continuar de onde parou.")
        sys.exit(130)
