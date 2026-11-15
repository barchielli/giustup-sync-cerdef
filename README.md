# giustup-sync-cerdef — le sentenze tributarie del CERDEF in Giustup

Porta nell'archivio di Giustup la giurisprudenza tributaria di merito pubblicata
dal MEF nel servizio **Documentazione Economica e Finanziaria**
([def.finanze.it](https://def.finanze.it)), che raccoglie le sentenze delle Corti
di giustizia tributaria di primo e secondo grado in forma integrale e
pseudonimizzata.

## Perché una seconda fonte, oltre alla banca dati DGT

| | banca dati DGT | CERDEF |
|---|---|---|
| documenti | ~973.000 | **64.804** (misura del 15.11.2026) |
| copertura | dal 2021 | **dagli anni Novanta** (13.622 fino al 2005) |
| testo | PDF | HTML, integrale |
| protezione | Akamai: 403 dai data center, e dopo troppe richieste anche residenziale | **nessuna** |
| metadati | estremi, materia, esito, spese, valore | estremi, sezione, **collegio e relatore**, massima, ricorsi |

Le due fonti si completano: il CERDEF aggiunge gli anni che la banca dati non
copre e i nomi dei giudici; la banca dati DGT aggiunge il resto. In indice
finiscono **nello stesso `giustup-tributaria`**, distinti dal campo `fonte`
(`mef-dgt` / `cerdef`), così la linguetta resta una sola.

## Come lavora

1. Apre la ricerca avanzata del CERDEF e sceglie «Tutte le Corti di giustizia
tributaria di primo e secondo grado». La pagina dei risultati porta i dati in
`window.xmlResult`: 50 provvedimenti per pagina, con identificativo ed estremi.
2. Per ogni identificativo scarica la scheda: un XML completo (`xmlDettaglio`)
con estremi, intitolazione, massima, udienza, collegio, relatore, data di
pronunciamento, ricorsi e il testo integrale in HTML dentro CDATA. Prende
**tutti** i campi, non solo il testo.
3. Il testo diventa un **PDF** (fpdf2) perché il visualizzatore e le miniature
di Giustup lavorano sui PDF. Il PDF va su S3 in
`contentstore/cerdef/<anno>/<id_nodo>.pdf` con `ContentType application/pdf` e i
metadati del documento.
4. Il documento va in indice con `_id` = UUIDv5 dell'identificativo CERDEF:
rilanciare il sync aggiorna lo stesso documento invece di duplicarlo.

## La scissione: raccolta a Milano, indice a Francoforte

```
Milano (i-0b1666c8c3e4251a6)              Francoforte (i-03b4df02ac1795862)
cerdef.py --senza-indice --salva-record   cerdef.py --indicizza-da-s3
        ↓                                          ↑
        └──────────── S3 (PDF + record JSON) ──────┘
```

- **Milano** non tocca OpenSearch: deposita il PDF e il record JSON in
  `lavorazione/cerdef/record/<anno>/<id_nodo>.json`.
- **Francoforte** legge i record, confronta `chiave_fonte` con l'indice e
  indicizza solo i nuovi (idempotente: rilanciarlo non duplica nulla).

## Uso

```
python3 cerdef.py --prova 5                    # cinque documenti, non tocca niente
python3 cerdef.py --anno 2025 --massimo 400    # una fetta
python3 cerdef.py --senza-indice --salva-record --massimo 800   # lato Milano
python3 cerdef.py --indicizza-da-s3            # lato motore
python3 cerdef.py --stato                      # ultimo giro
```

## Unità

| unità | nodo | quando |
|---|---|---|
| `giustup-cerdef.timer` | Milano | sabato 01:40 UTC (entro la finestra in cui Milano è accesa) |
| `giustup-cerdef-indicizza.timer` | Francoforte | sabato 12:45 UTC |

## OCR

Il testo del CERDEF è **nativo**: viene dall'HTML della scheda, non da
scansioni, quindi il PDF generato ha sempre lo strato di testo e non serve
riconoscere nulla. Il giro `--ocr` esiste per simmetria con gli altri sync
(segnala i documenti con `caratteri < 200`) ed è idempotente.

## Prerequisiti sul nodo di raccolta

- `python3` con `playwright`, `fpdf2`, `boto3`
- Google Chrome vero (`google-chrome`), non solo il Chromium di Playwright:
  `channel=\"chrome\"` lo cerca in `/opt/google/chrome/chrome`
- `xvfb` non serve: Chrome gira in `headless=True`
