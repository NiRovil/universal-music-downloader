# ytm-dl

Baixa playlists do YouTube Music em sete formatos, organizadas por pasta, com
capa em alta resolução, gênero, gravadora e ano embutidos nos arquivos.

> **Uso legal:** baixe apenas faixas livres de copyright, sob licença que
> permita, ou que você tenha direito de baixar. A ferramenta não verifica isso
> por você.

## Instalação

O `ytm-dl.py` roda num virtualenv próprio, já criado em `.venv/`:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Também precisa do `ffmpeg` no sistema, que faz toda a conversão de áudio:

```bash
brew install ffmpeg
```

### Um atalho que vale a pena

```bash
echo "alias ytmdl='$HOME/Documents/ytm-dl/.venv/bin/python $HOME/Documents/ytm-dl/ytm-dl.py'" >> ~/.zshrc
source ~/.zshrc
```

Daí em diante, só `ytmdl '<link>'`. O resto deste documento escreve o caminho
completo para não depender do alias.

---

## Uso

```bash
~/Documents/ytm-dl/.venv/bin/python ~/Documents/ytm-dl/ytm-dl.py \
  'https://music.youtube.com/playlist?list=PLxxxxxxxx'
```

Sempre entre **aspas simples** — links de playlist têm `?` e `&`, que o shell
interpretaria como comandos.

### Comece sempre por um dry-run

```bash
... ytm-dl.py -n 5 --dry-run '<link>'
```

Mostra exatamente que título, artista, álbum, gênero e capa cada faixa
receberia, **sem baixar nada**. É a forma barata de descobrir que uma playlist
casa mal com o Deezer antes de gastar disco e banda.

### Saída

```
Downloads/House/
├── 01 - Step-Grandma.flac        17,0 MB
├── 02 - MONEY ON THE DASH.flac   16,1 MB
└── .ytm-dl-baixados.txt          histórico para retomar
```

Cada arquivo carrega:

| tag | origem | exemplo |
|---|---|---|
| title, artist | YouTube Music | `Step-Grandma` · `Salvatore Ganacci` |
| album, albumartist | Deezer (YouTube como reserva) | `Step-Grandma` |
| tracknumber | posição na playlist | `1` |
| date | Deezer | `2021-07-09` |
| **genre** | **só Deezer** | `Electro; Techno/House` |
| organization | Deezer | `Zatara Recordings` |
| **capa** | **só Deezer** | JPEG 1000×1000 embutido |

---

## Opções

| flag | efeito | padrão |
|---|---|---|
| `-o DIR` | pasta base de destino | `<pasta do script>/Downloads` |
| `--folder PASTA` | baixa direto nesta pasta, sem subpasta da playlist | — |
| `--no-dedup` | não checa se a música já existe na pasta | — |
| `-n N` | baixa só as N primeiras faixas | tudo |
| `-j N` | fragmentos simultâneos por faixa | `4` |
| `-c NAV` | cookies do navegador (playlists privadas) | — |
| `-f` | ignora o histórico e rebaixa tudo | — |
| `--format FMT` | formato de saída (ver tabela abaixo) | `flac` |
| `--bitrate K` | kbps nos formatos com perdas | `320` mp3, `256` m4a |
| `--bits 16\|24` | profundidade | `16` |
| `--rate HZ` | reamostra | mantém os 48000 Hz da fonte |
| `--no-enrich` | não consulta o Deezer | — |
| `--no-cover` | não embute capa | — |
| `--tolerance S` | diferença de duração aceita ao casar | `3` s |
| `--loose` | aceita casamento aproximado | — |
| `--dry-run` | simula, sem baixar | — |

---

## Como o enriquecimento funciona

O YouTube Music entrega título, artista, álbum e ano — **mas não gênero**, e a
imagem que ele expõe é o thumbnail 16:9 do vídeo, não a capa do álbum. Por isso
o que falta vem do Deezer, cuja API pública não exige chave nem cadastro.

Para não etiquetar uma faixa com dados de outra, cada resultado passa por dois
filtros:

1. **Duração** dentro de ±3 s da faixa baixada (`--tolerance` ajusta).
2. **Título e artista** ambos semelhantes.

Os dois juntos importam. Só o título deixaria passar coletâneas de cover — numa
das faixas de teste, "Lone Digger" casou com o álbum *EDM Hits on Piano*, uma
versão ao piano com duração parecida. Exigir também o artista descartou o
falso positivo.

Quando nada casa, a faixa **mantém os dados do YouTube** e aparece no relatório
final:

```
5/5 faixas processadas | 4 com genero/capa do Deezer

sem enriquecimento:
  03 - Lone Digger: Deezer: 11 resultado(s), nenhum com duracao ~230s
```

Aí você decide: rodar com `--loose` (aceita o mais próximo por nome e marca
como aproximado no relatório) ou deixar sem gênero. `--loose` reabre a porta
para o falso positivo do cover ao piano — use com o relatório na mão.

---

## Juntando vários álbuns numa coletânea

Por padrão cada playlist ganha sua própria pasta, com o nome dela. Quando você
quer garimpar álbuns soltos no YouTube e jogar tudo num lugar só — uma
coletânea "2000 hits", por exemplo — use `--folder`:

```bash
... ytm-dl.py --folder "2000 hits" '<link do primeiro álbum>'
... ytm-dl.py --folder "2000 hits" '<link do segundo álbum>'
... ytm-dl.py --folder "2000 hits" '<link do terceiro>'
```

`--folder` aceita um nome (criado sob `--dest`) ou um caminho absoluto, e muda
duas coisas:

**A numeração continua** de onde a pasta parou, em vez de recomeçar do 01 a
cada playlist:

```
-> ja na pasta: 3 arquivo(s) .flac, numerando a partir de 04
```

**Faixas repetidas são detectadas e puladas.** Playlists diferentes compartilham
muita coisa, e a mesma gravação costuma ter um id de vídeo *diferente* em cada
uma — então conferir só o histórico não resolveria. A verificação lê
**título, artista e duração** das tags dos arquivos que já estão na pasta e
compara com o que a playlist anuncia, antes de baixar:

```
[01/04] Step-Grandma
        = ja existe na pasta: 01 - Step-Grandma.flac — pulando duplicata
[04/04] System
        OK 18.2 MB | Electro; Dance | capa 1000x1000
```

```
4 faixa(s) na playlist
    3 repetidas — a musica ja estava na pasta (puladas)
    1 baixadas agora | 1 com genero/capa do Deezer
```

### O que conta como a mesma música

Título semelhante **e** duração dentro de 5 segundos. Quando o artista é
conhecido dos dois lados, ele também precisa bater.

A duração é o critério que faz a diferença na prática:

| situação | resultado |
|---|---|
| mesma faixa vinda de outra playlist | pulada ✅ |
| *Extended Mix* de uma faixa que você já tem | baixada — durações distantes |
| remix homônimo por outro artista | baixada — artista não bate |
| mesma faixa, upload com 2 s a mais | pulada ✅ |
| *remaster* com a mesma duração | pulada ⚠️ |

Aquela última linha é o limite conhecido: um remaster que dure o mesmo que o
original é indistinguível pelos dados que a playlist expõe antes do download.
Se quiser os dois, use `--no-dedup` nessa rodada e renomeie um deles depois.

A verificação olha só o formato pedido — um `.wav` na pasta não impede o
download do `.flac`.

## Retomar e re-baixar

Antes de baixar qualquer coisa, o script **varre a pasta de destino** e cruza o
que achou com o histórico em `.ytm-dl-baixados.txt`. O que já está lá é pulado
sem download e **sem consulta ao Deezer** — vale também para `--dry-run`, que
por isso não gasta rede reprocessando o que você já tem.

```
-> ja na pasta: 2 arquivo(s) .flac

[01/03] Step-Grandma
        = ja baixada: 01 - Step-Grandma.flac (17.0 MB) | capa | Electro; Techno/House
[02/03] MONEY ON THE DASH
        = ja baixada: 02 - MONEY ON THE DASH.flac (16.1 MB) | capa | Dance
[03/03] Lone Digger
        ...
```

Cada faixa pulada vem com o que ela já carrega (`capa`, gênero), lido do
próprio arquivo. Se algo estiver faltando, o relatório final junta tudo:

```
3 faixa(s) na playlist
    2 ja estavam na pasta (puladas)
    1 baixadas agora | 1 com genero/capa do Deezer

ja na pasta, porem com metadata incompleta:
  05 - Alguma Faixa: sem capa | sem genero
  para reetiquetar, apague esses arquivos e rode de novo (ou use -f)
```

A verificação considera o **formato pedido**. Um `.wav` na pasta não faz o
script pular o `.flac` que você acabou de pedir — ele avisa e gera o que falta:

```
[01/02] Step-Grandma
        ! so existe em .flac (01 - Step-Grandma.flac) — gerando .wav
```

Três situações e o que acontece em cada uma:

| situação | comportamento |
|---|---|
| conexão caiu, Ctrl+C | rode o mesmo comando; pega só o que falta |
| playlist ganhou faixas | mesmo comando; só as novas |
| você apagou um arquivo mas o id continua no histórico | detecta a ausência e rebaixa (`! no historico, mas ausente da pasta`) |

Aquela última linha importa: o histórico sozinho faria o yt-dlp pular para
sempre uma faixa que não existe mais em disco. Por isso a pasta manda, e o
histórico só complementa.

- **Quer tudo de novo?** `-f` apaga o histórico e sobrescreve.

  > **Cuidado com o `-f`:** a substituição é *no lugar* — o arquivo antigo é
  > removido antes de o novo ficar pronto. Se a rede cair no meio, a faixa fica
  > faltando até você rodar de novo. Sem `-f` esse risco não existe. Use `-f`
  > para recomeçar de verdade, não como forma de "tentar de novo".

Uma faixa indisponível não derruba o resto: o script segue e lista a falha no
relatório, separada das que já existiam.

## Formatos de saída

```bash
... ytm-dl.py --format aiff '<link>'
... ytm-dl.py --format mp3 --bitrate 256 '<link>'
```

Medido numa faixa de 2 min 41 s, todos com capa 1000×1000 e gênero embutidos:

| `--format` | arquivo | tamanho | codec | perdas | para quê |
|---|---|---|---|---|---|
| **`flac`** (padrão) | `.flac` | 17,9 MB | FLAC 16/48 | não | uso geral; tags e capa nativas |
| `alac` | `.m4a` | 18,4 MB | ALAC 16/48 | não | mesmo áudio, para o ecossistema Apple |
| `wav` | `.wav` | 31,0 MB | PCM 16 LE | não | quando algo exige PCM cru |
| `aiff` | `.aiff` | 31,0 MB | PCM 16 BE | não | PCM no formato que o Rekordbox prefere |
| `mp3` | `.mp3` | 6,6 MB | MP3 320 kbps | sim | compatibilidade com qualquer coisa |
| `m4a` | `.m4a` | 5,5 MB | AAC 256 kbps | sim | metade do MP3, qualidade semelhante |
| `opus` | `.opus` | 2,7 MB | Opus ~160 kbps | — | o áudio original, sem reconversão |

### Como escolher

O YouTube entrega **Opus a ~160 kbps, 48 kHz** — já comprimido com perdas.
Tudo na tabela parte daí, e isso decide a escolha:

- **`opus`** é o único que não reconverte nada: copia o que o YouTube serviu.
  É o menor arquivo e, tecnicamente, o mais fiel à fonte. Perde em
  compatibilidade — muito equipamento de DJ e player antigo não abre.
- **`flac`, `alac`, `wav`, `aiff`** guardam sem perda adicional o que o Opus
  decodificou. Não recuperam qualidade que não existe, mas entregam PCM ou
  lossless, que é o que DAW, sampler e software de DJ costumam exigir.
- **`mp3` e `m4a`** são reconversão de lossy para lossy, com perda em cima de
  perda. Use quando compatibilidade importar mais que fidelidade.

O padrão é **16 bits, 48 kHz**, e os dois números têm motivo:

- **16 bits** porque o Opus decodifica em float e, deixado por conta própria, o
  ffmpeg deduz 24 bits — o FLAC sairia maior que o WAV, sem nenhum ganho: a
  fonte é lossy. `--bits 24` existe se algum fluxo seu exigir.
- **48 kHz**, a taxa nativa da fonte. Reamostrar para 44,1 kHz é uma conversão
  a mais sem benefício; use `--rate 44100` só se o equipamento exigir.

### Onde a capa fica em cada um

Os quatro esquemas de metadados que os sete formatos usam:

| esquema | formatos | capa |
|---|---|---|
| Vorbis comments | `flac` | bloco de imagem nativo |
| Vorbis comments | `opus` | bloco FLAC serializado em base64 |
| ID3v2 | `mp3`, `wav`, `aiff` | frame `APIC` |
| átomos MP4 | `alac`, `m4a` | átomo `covr` |

Em `wav` e `aiff` o ID3 é um chunk enfiado dentro do container — funciona
(Rekordbox lê), mas nem todo player procura ali. O Finder do macOS não mostra
miniatura de `flac` nem de `opus`: é limitação do QuickLook, não do arquivo. Se
a capa visível no Finder importa, use `m4a` ou `mp3`.

## Quando dá errado

**`erro: nao consegui ler essa playlist.`**

1. *Link truncado.* O id depois de `list=` costuma ter ~34 caracteres. Copie da
   barra de endereço, não do texto de um compartilhamento.
2. *Playlist privada.* Use `-c chrome` (ou `brave`, `firefox`, `safari`, `edge`).
3. *Erro transitório.* O YouTube às vezes responde "playlist does not exist"
   para uma playlist que existe. Tente de novo antes de investigar.

**`erro: dependencia faltando`** — você chamou o script com o Python do sistema
em vez do `.venv`. Use o caminho completo do `.venv/bin/python`.

**Muitas faixas sem gênero** — rode com `--dry-run` para ver os motivos sem
baixar. Playlists de remix, edits e sets ao vivo casam mal com catálogo
comercial; é limitação da fonte, não do script.

**Faixas falhando em massa** — quase sempre o YouTube mudou algo:
`./.venv/bin/pip install -U yt-dlp`.

---
