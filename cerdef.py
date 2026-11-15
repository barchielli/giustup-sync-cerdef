#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Giustup - le sentenze tributarie del CERDEF (Documentazione Economica e Finanziaria).

Porta nell'archivio la giurisprudenza tributaria di merito pubblicata dal MEF
nel servizio Documentazione Economica e Finanziaria (def.finanze.it), che
raccoglie le sentenze delle Corti di giustizia tributaria di primo e secondo
grado in forma integrale e pseudonimizzata.

PERCHE' QUESTO RACCOGLITORE, OLTRE A QUELLO DELLA BANCA DATI DGT

La banca dati della Giurisprudenza Tributaria (bancadatigiurisprudenza.
giustiziatributaria.gov.it) e' la fonte completa, ma la sua ricerca e' protetta
da Akamai e risponde 403 agli indirizzi dei data center e, dopo troppe
richieste, anche a quelli residenziali. Il CERDEF e' la seconda fonte
istituzionale: 65.000 sentenze (misura del 15.11.2026), testo integrale in
HTML, nessuna protezione bot, raggiungibile da Milano. E' piu' piccolo ma
aggiunge gli anni che la banca dati non copre (risale agli anni Novanta).

COME LAVORA

1. Si apre la ricerca avanzata del CERDEF e si sceglie "Tutte le Corti di
   giustizia tributaria di primo e secondo grado". La pagina dei risultati
   porta i dati in una variabile JavaScript (`xmlResult`): 50 provvedimenti per
   pagina, con identificativo ed estremi. Si pagina fino in fondo e si raccoglie
   l'elenco degli identificativi.
2. Per ogni identificativo si scarica la pagina di dettaglio, che porta un XML
   completo (`xmlDettaglio`): estremi, intitolazione, massima, udienza,
   collegio, relatore, data di pronunciamento, ricorsi e il testo integrale in
   HTML dentro CDATA. Si prendono TUTTI i campi, non solo il testo.
3. Il testo diventa un PDF (fpdf2) perche' il visualizzatore e le miniature di
   Giustup lavorano sui PDF. Il PDF va su S3 in
   `contentstore/cerdef/<anno>/<id_nodo>.pdf` con `ContentType application/pdf`
   e i metadati del documento.
4. Il documento va in indice con `_id` = UUIDv5 dell'identificativo CERDEF:
   rilanciare il sync aggiorna lo stesso documento invece di duplicarlo.
5. In scissione (raccoglitore su Milano, motore a Francoforte) il giro di
   Milano scrive su S3 anche il record JSON (`--salva-record`) e il motore lo
   indicizza (`--indicizza-da-s3`), senza riscaricare nulla dalla fonte.

USO

    python3 cerdef.py --prova 5              # cinque documenti, non tocca niente
    python3 cerdef.py --massimo 332 --anno 2025
    python3 cerdef.py --salva-record --senza-indice     # lato Milano
    python3 cerdef.py --indicizza-da-s3                 # lato motore
    python3 cerdef.py --stato
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from html import unescape

BASE = "https://def.finanze.it/DocTribFrontend"
PAGINA_RICERCA = BASE + "/callRicAvanzataGiurisprudenza.do?js_enabled=1&reset=y"
TUTTE_LE_CORTI = "Tutte le Corti di giustizia tributaria di primo e secondo grado"

BUCKET = "giustup-archivio-sentenze-593859982945"
IDX = "giustup-tributaria"
PREFISSO_PDF = "contentstore/cerdef"
PREFISSO_REC = "lavorazione/cerdef/record"
CHIAVE_STATO = "stato-sync/cerdef.json"
NOMESPACE = uuid.UUID("5b8e2f4a-9c71-4d63-b0f2-1a7c3d9e5f40")

ATTESA_RICERCA = 2.0        # fra una pagina di elenco e la successiva
PAUSA_FRA_DOCUMENTI = 0.08  # il CERDEF non ha protezione bot: si va piano per educazione
TENTATIVI = 4
RIGHE_PER_LOTTO = 200
VERSIONE = "cerdef-1.0"

_salva_record = False


def adesso():
    return datetime.datetime.now(datetime.timezone.utc)


def log(messaggio):
    print("%s %s" % (adesso().strftime("%Y-%m-%d %H:%M:%S"), messaggio), flush=True)


# --------------------------------------------------------------------- AWS

def s3():
    import boto3
    return boto3.client("s3", region_name="eu-central-1")


def cloudwatch():
    import boto3
    return boto3.client("cloudwatch", region_name="eu-central-1")


def metrica(nome, valore, unita="Count"):
    try:
        cloudwatch().put_metric_data(
            Namespace="Giustup", MetricData=[{
                "MetricName": nome, "Value": float(valore), "Unit": unita}])
    except Exception as e:
        log("metrica %s non scritta: %r" % (nome, e))


# --------------------------------------------------------------- il browser

def sessione():
    """Apre Chrome con Playwright. Serve solo per l'elenco: la pagina dei
    risultati costruisce i dati via JavaScript, quindi una richiesta HTTP nuda
    non basta a leggerli."""
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    navigatore = p.chromium.launch(
        channel="chrome", headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"])
    contesto = navigatore.new_context(
        locale="it-IT", timezone_id="Europe/Rome",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/157.0.0.0 Safari/537.36"))
    pagina = contesto.new_page()
    return p, navigatore, contesto, pagina


def _xml_result(pagina):
    """La lista dei risultati sta in `window.xmlResult`."""
    try:
        return pagina.evaluate("() => window.xmlResult || ''")
    except Exception:
        return ""


def _attendi_risultati(pagina, secondi=90):
    """Aspetta che il JavaScript abbia messo i risultati in `xmlResult`."""
    try:
        pagina.wait_for_function("() => (window.xmlResult || '').length > 100", timeout=secondi * 1000)
        return True
    except Exception:
        return False


def enumera(pagina, anno=0, massimo=0):
    """Tutti gli identificativi delle sentenze delle Corti tributarie.

    Restituisce una lista di dict con id, estremi, pagina. Si ferma quando le
    pagine sono finite, quando ha raggiunto `massimo` o quando una pagina non
    porta nulla di nuovo (rete lenta, sessione scaduta).
    """
    pagina.goto(PAGINA_RICERCA, wait_until="domcontentloaded", timeout=90000)
    pagina.evaluate("""(anno) => {
        const f = document.getElementById('formRicAvanzG');
        f.superEnte.value = 'Tutte le Corti di giustizia tributaria di primo e secondo grado';
        f.parole.value = '';
        if (f.ricercaPresenzaMassima) f.ricercaPresenzaMassima.checked = false;
        if (anno) { f.annoDataEmissioneDa.value = anno; f.annoDataEmissioneA.value = anno; }
        f.js_enabled.value = '1';
        f.submit();
    }""", str(anno or ""))
    pagina.wait_for_load_state("domcontentloaded", timeout=90000)
    _attendi_risultati(pagina)

    voci = []
    visti = set()
    pagina_corrente = 1
    while True:
        xml = _xml_result(pagina)
        if not xml:
            log("pagina %d: nessun risultato nell'HTML" % pagina_corrente)
            break
        trovati = re.findall(r'<Provvedimento idProvvedimento="([^"]+)">(.*?)</Provvedimento>',
                             xml, re.S)
        nuovi = 0
        for identificativo, corpo in trovati:
            if identificativo in visti:
                continue
            visti.add(identificativo)
            estremi = re.search(r"<estremi[^>]*>(.*?)</estremi>", corpo, re.S)
            voci.append({
                "id": identificativo,
                "estremi": unescape(estremi.group(1)).strip() if estremi else "",
                "pagina": pagina_corrente,
            })
            nuovi += 1
        totale = re.search(r"<contatoreGiurisprudenza>(\d+)</contatoreGiurisprudenza>", xml)
        ulteriori = "<ulterioriRisultati>true</ulterioriRisultati>" in xml
        log("pagina %d: %d provvedimenti (in tutto %d%s)"
            % (pagina_corrente, nuovi, len(voci),
               ", dichiarati %s" % totale.group(1) if totale else ""))
        if not ulteriori or nuovi == 0:
            break
        if massimo and len(voci) >= massimo:
            break
        # la pagina successiva: il pulsante "Avanti" del paginatore
        cliccato = pagina.evaluate("""() => {
            const c = [...document.querySelectorAll('a,button,input')]
              .filter(x => /avanti/i.test((x.textContent || x.value || '')));
            if (!c.length) return 'nessuno';
            const t = c[c.length - 1];
            if (t.tagName === 'A' && t.getAttribute('href') && t.getAttribute('href') !== '#') {
                t.click(); return 'link';
            }
            t.click(); return 'click';
        }""")
        if cliccato == "nessuno":
            log("paginatore non trovato: mi fermo con %d voci" % len(voci))
            break
        try:
            pagina.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
            pass
        _attendi_risultati(pagina)
        pagina_corrente += 1
        if pagina_corrente > 4000:
            log("troppe pagine: mi fermo")
            break
    return voci


# -------------------------------------------------------- il documento

def _testo_da_js(html, nome):
    """Estrae una stringa JavaScript (`var nome = '...'`) e ne scioglie gli
    escape: il CERDEF ci mette dentro un XML con i dati del provvedimento."""
    marcatore = "var " + nome + " = '"
    i = html.find(marcatore)
    if i < 0:
        return ""
    inizio = i + len(marcatore)
    fine = html.find("';", inizio)
    if fine < 0:
        return ""
    s = html[inizio:fine]
    s = s.replace(chr(92) + "/", "/")
    s = s.replace(chr(92) + '"', '"')
    s = s.replace(chr(92) + chr(39), chr(39))
    s = s.replace(chr(92) + "r", chr(13))
    s = s.replace(chr(92) + "n", chr(10))
    s = s.replace(chr(92) + "t", chr(9))
    return s


def html_a_testo(html):
    """Dal frammento HTML del provvedimento al testo leggibile: i paragrafi
    restano separati, i tag spariscono, le entita si sciolgono."""
    if not html:
        return ""
    t = re.sub("(?is)<(script|style).*?</\1>", " ", html)
    t = re.sub(r"(?i)<br\s*/?>", chr(10), t)
    for tag in ("</p>", "</div>", "</tr>", "</li>", "</h1>", "</h2>", "</h3>", "</h4>", "</h5>", "</h6>"):
        t = t.replace(tag, chr(10) * 2)
        t = t.replace(tag.upper(), chr(10) * 2)
    t = re.sub("<[^>]+>", " ", t)
    t = unescape(t)
    t = t.replace(chr(160), " ")
    t = re.sub("[ " + chr(9) + "]+", " ", t)
    while chr(10) * 3 in t:
        t = t.replace(chr(10) * 3, chr(10) * 2)
    return t.strip()


def _campi(radice, nome):
    """Tutti i figli diretti di un nodo, come dizionario: nessun campo si perde."""
    fuori = {}
    for figlio in list(radice):
        chiave = figlio.tag.split("}")[-1]
        valore = (figlio.text or "").strip()
        if valore:
            fuori[chiave] = valore
    return fuori


def dettaglio(contesto, identificativo):
    """La scheda completa di un provvedimento: XML del CERDEF + testo."""
    url = "%s/getGiurisprudenzaDetail.do?id=%s" % (BASE, identificativo.replace("{", "%7B").replace("}", "%7D"))
    ultimo = None
    for tentativo in range(1, TENTATIVI + 1):
        try:
            risposta = contesto.request.get(url, timeout=90000)
            if risposta.status != 200:
                raise IOError("HTTP %d" % risposta.status)
            xml = _testo_da_js(risposta.text(), "xmlDettaglio")
            if not xml:
                raise IOError("nessun xmlDettaglio nella pagina")
            radice = ET.fromstring(xml)
            provvedimento = radice.find(".//Provvedimento")
            if provvedimento is None:
                raise IOError("nessun Provvedimento nell'XML")
            dati = _campi(provvedimento, "Provvedimento")
            testo_html = ""
            nodo_testo = provvedimento.find("testo")
            if nodo_testo is not None:
                testo_html = nodo_testo.text or ""
            testo = html_a_testo(testo_html)
            if len(testo) < 100:
                raise IOError("testo troppo corto (%d caratteri)" % len(testo))
            dati["testo"] = testo
            dati["id_cerdef"] = identificativo
            return dati
        except Exception as e:
            ultimo = e
            time.sleep(min(2 * tentativo, 8))
    raise IOError("scheda non letta: %r" % (ultimo,))


def _numero(testo):
    m = re.match(r"\s*(\d+)", testo or "")
    return int(m.group(1)) if m else 0


def _anno(estremi, data_pronunciamento):
    for testo in (data_pronunciamento, estremi):
        m = re.search(r"(\d{4})", testo or "")
        if m:
            return int(m.group(1))
    return 0


def scheda(dati, voci_elenco=None):
    """Dal dettaglio al documento d'indice: tutti i campi che la fonte da'."""
    estremi = dati.get("estremi") or (voci_elenco or {}).get("estremi", "")
    data_pron = dati.get("dataPronunciamento") or dati.get("udienza") or ""
    anno = _anno(estremi, data_pron)
    numero = _numero(estremi)
    corte = ""
    sezione = ""
    m = re.search(r"-\s*(Corte di giustizia tributaria[^-]*?)(?:\s*-\s*Sezione/Collegio\s*(\S+))?$", estremi)
    if m:
        corte = m.group(1).strip()
        sezione = (m.group(2) or "").strip()
    titolo = estremi or ("Sentenza CERDEF %s" % dati.get("id_cerdef"))
    id_nodo = str(uuid.uuid5(NOMESPACE, "cerdef:" + dati["id_cerdef"]))
    documento = {
        "id_nodo": id_nodo,
        "chiave_fonte": "cerdef:" + dati["id_cerdef"],
        "fonte": "cerdef",
        "tipo": "sentenza_tributaria",
        "titolo": titolo,
        "estremi": estremi,
        "anno": anno,
        "numero": numero,
        "data": _data_iso(data_pron),
        "corte": corte,
        "sezione": sezione,
        "grado": "secondo" if "secondo grado" in corte else ("primo" if corte else ""),
        "relatore": dati.get("relatore", ""),
        "collegio": dati.get("collegio", ""),
        "udienza": dati.get("udienza", ""),
        "intitolazione": dati.get("intitolazione", ""),
        "massima": dati.get("massima", ""),
        "ricorsi": dati.get("ricorsi", ""),
        "testo": dati.get("testo", ""),
        "caratteri": len(dati.get("testo", "")),
        "created": adesso().isoformat().replace("+00:00", "Z"),
        "versione_pipeline": VERSIONE,
    }
    return {k: v for k, v in documento.items() if v not in ("", None, 0) or k in ("anno", "numero")}


def _data_iso(testo):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", testo or "")
    if not m:
        return ""
    return "%s-%02d-%02d" % (m.group(3), int(m.group(2)), int(m.group(1)))


# ------------------------------------------------------------------- il PDF

def _ascii(testo, tetto=300):
    t = unicodedata.normalize("NFKD", (testo or "")[:tetto])
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.encode("ascii", "replace").decode("ascii")


def pdf_da_scheda(documento):
    """Il PDF che il visualizzatore mostra: intestazione con gli estremi, poi
    massima (se c'e') e testo integrale. fpdf2: ogni multi_cell porta
    new_x/new_y, altrimenti il cursore resta a destra e la chiamata dopo
    fallisce."""
    from fpdf import FPDF

    class Foglio(FPDF):
        def header(self):
            self.set_font("Times", "B", 9)
            self.cell(0, 5, _ascii("Giustizia tributaria - CERDEF", 120), new_x="LMARGIN", new_y="NEXT")
            self.ln(1)

        def footer(self):
            self.set_y(-12)
            self.set_font("Times", "", 8)
            self.cell(0, 4, "pagina %d" % self.page_no(), align="C", new_x="LMARGIN", new_y="NEXT")

    pdf = Foglio(format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()

    pdf.set_font("Times", "B", 13)
    pdf.multi_cell(0, 7, documento.get("titolo", ""), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    pdf.set_font("Times", "", 9)
    for etichetta, chiave in (("Corte", "corte"), ("Sezione", "sezione"),
                              ("Data", "data"), ("Numero", "numero"),
                              ("Relatore", "relatore"), ("Collegio", "collegio"),
                              ("Ricorsi", "ricorsi")):
        valore = documento.get(chiave)
        if valore:
            pdf.multi_cell(0, 5, _ascii("%s: %s" % (etichetta, valore), 400),
                           new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    if documento.get("massima"):
        pdf.set_font("Times", "B", 10)
        pdf.multi_cell(0, 5, "Massima", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Times", "", 9)
        pdf.multi_cell(0, 5, documento["massima"], new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    pdf.set_font("Times", "", 9)
    for paragrafo in (documento.get("testo") or "").split(chr(10) * 2):
        paragrafo = paragrafo.strip()
        if not paragrafo:
            continue
        # un token piu' largo della cella manda in errore il wrapping
        if len(paragrafo) > 4000:
            paragrafo = paragrafo[:4000]
        pdf.multi_cell(0, 4.6, paragrafo, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.2)
    return bytes(pdf.output())


# --------------------------------------------------------------------- S3

def metadati_s3(documento):
    """S3 accetta solo US-ASCII nei metadati: si normalizza sempre."""
    fuori = {}
    for chiave, valore in (("tipo", documento.get("tipo")), ("id_nodo", documento.get("id_nodo")),
                           ("titolo", documento.get("titolo")), ("anno", documento.get("anno")),
                           ("numero", documento.get("numero")), ("corte", documento.get("corte")),
                           ("chiave_fonte", documento.get("chiave_fonte")), ("fonte", documento.get("fonte"))):
        if valore not in (None, ""):
            fuori[chiave] = _ascii(str(valore), 200)
    return fuori


def chiave_pdf(documento):
    anno = documento.get("anno") or 0
    return "%s/%s/%s.pdf" % (PREFISSO_PDF, anno, documento["id_nodo"])


def carica_pdf(cliente, documento, corpo):
    chiave = chiave_pdf(documento)
    cliente.put_object(Bucket=BUCKET, Key=chiave, Body=corpo,
                       ContentType="application/pdf", Metadata=metadati_s3(documento))
    return chiave


def salva_record(cliente, documento):
    """Lato Milano: il record JSON accanto al PDF, cosi' il motore indicizza
    senza rifare la raccolta."""
    if not _salva_record or not documento:
        return
    anno = documento.get("anno") or 0
    chiave = "%s/%s/%s.json" % (PREFISSO_REC, anno, documento.get("id_nodo"))
    corpo = dict(documento)
    try:
        cliente.put_object(Bucket=BUCKET, Key=chiave,
                           Body=json.dumps(corpo, ensure_ascii=False).encode("utf-8"),
                           ContentType="application/json")
    except Exception as e:
        log("record %s non salvato: %r" % (chiave, e))


# ---------------------------------------------------------------- l'indice

def os_chiama(metodo, percorso, corpo=None):
    import urllib.request
    import ssl
    base = os.environ.get("GIUSTUP_OPENSEARCH", "https://localhost:9200").rstrip("/")
    dati = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    richiesta = urllib.request.Request(base + percorso, data=dati, method=metodo)
    richiesta.add_header("Content-Type", "application/json")
    contesto = ssl.create_default_context()
    contesto.check_hostname = False
    contesto.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(richiesta, timeout=120, context=contesto) as risposta:
        return json.loads(risposta.read().decode("utf-8"))


def crea_indice_se_manca():
    """L'indice e' quello della giustizia tributaria, condiviso con la banca
    dati del MEF: questa fonte ci aggiunge i suoi documenti con `fonte=cerdef`."""
    try:
        os_chiama("HEAD", "/" + IDX)
        return
    except Exception:
        pass
    mappatura = {
        "mappings": {
            "properties": {
                "chiave_fonte": {"type": "keyword"},
                "fonte": {"type": "keyword"},
                "tipo": {"type": "keyword"},
                "titolo": {"type": "text", "analyzer": "italian"},
                "estremi": {"type": "text", "analyzer": "italian"},
                "testo": {"type": "text", "analyzer": "italian"},
                "massima": {"type": "text", "analyzer": "italian"},
                "intitolazione": {"type": "text", "analyzer": "italian"},
                "corte": {"type": "keyword"},
                "sezione": {"type": "keyword"},
                "grado": {"type": "keyword"},
                "relatore": {"type": "text"},
                "collegio": {"type": "text"},
                "ricorsi": {"type": "text"},
                "anno": {"type": "integer"},
                "numero": {"type": "integer"},
                "data": {"type": "date"},
                "udienza": {"type": "text"},
                "caratteri": {"type": "integer"},
                "chiave_s3": {"type": "keyword", "index": False},
                "created": {"type": "date"},
                "ocr_fatto": {"type": "date"},
                "ocr_caratteri": {"type": "integer"},
                "versione_pipeline": {"type": "keyword"},
            }
        }
    }
    os_chiama("PUT", "/" + IDX, mappatura)


def conosciuti(chiavi):
    if not chiavi:
        return set()
    try:
        risposta = os_chiama("POST", "/%s/_search" % IDX, {
            "size": 0,
            "query": {"terms": {"chiave_fonte": chiavi}},
            "aggs": {"trovati": {"terms": {"field": "chiave_fonte", "size": len(chiavi) + 10}}},
        })
        return set(b["key"] for b in risposta.get("aggregations", {}).get("trovati", {}).get("buckets", []))
    except Exception as e:
        log("indice non interrogabile (%r): considero tutti nuovi" % (e,))
        return set()


def _bulk(documenti):
    righe = []
    for documento in documenti:
        righe.append(json.dumps({"index": {"_index": IDX, "_id": documento["id_nodo"]}}))
        righe.append(json.dumps(documento, ensure_ascii=False))
    corpo = (chr(10).join(righe) + chr(10)).encode("utf-8")
    import urllib.request
    import ssl
    base = os.environ.get("GIUSTUP_OPENSEARCH", "https://localhost:9200").rstrip("/")
    richiesta = urllib.request.Request(base + "/_bulk", data=corpo, method="POST")
    richiesta.add_header("Content-Type", "application/x-ndjson")
    contesto = ssl.create_default_context()
    contesto.check_hostname = False
    contesto.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(richiesta, timeout=180, context=contesto) as risposta:
        esito = json.loads(risposta.read().decode("utf-8"))
    errori = [v for v in esito.get("items", []) if v.get("index", {}).get("error")]
    if errori:
        raise IOError("%d documenti non indicizzati: %s"
                      % (len(errori), errori[0]["index"]["error"].get("reason", "")[:200]))
    return len(documenti)


def indicizza(documenti):
    totale = 0
    for i in range(0, len(documenti), RIGHE_PER_LOTTO):
        totale += _bulk(documenti[i:i + RIGHE_PER_LOTTO])
    return totale


def leggi_stato():
    try:
        corpo = s3().get_object(Bucket=BUCKET, Key=CHIAVE_STATO)["Body"].read()
        return json.loads(corpo.decode("utf-8"))
    except Exception:
        return {}


def scrivi_stato(riassunto):
    try:
        s3().put_object(Bucket=BUCKET, Key=CHIAVE_STATO,
                        Body=json.dumps(riassunto, ensure_ascii=False, indent=2).encode("utf-8"),
                        ContentType="application/json")
    except Exception as e:
        log("stato non scritto: %r" % (e,))


# -------------------------------------------------------- il giro completo

def sincronizza(args):
    inizio = time.time()
    riassunto = {"quando": adesso().isoformat().replace("+00:00", "Z"), "anno": args.anno or 0,
                 "prova": bool(args.prova), "letti": 0, "nuovi": 0, "saltati": 0,
                 "falliti": 0, "secondi": 0, "versionePipeline": VERSIONE}
    cliente = None if args.senza_s3 or args.prova else s3()
    indice = not args.senza_indice and not args.prova
    if indice:
        crea_indice_se_manca()

    play = navigatore = contesto = pagina = None
    try:
        play, navigatore, contesto, pagina = sessione()
        voci = enumera(pagina, anno=args.anno, massimo=args.massimo * 3 if args.massimo else 0)
        riassunto["letti"] = len(voci)
        log("elenco: %d provvedimenti" % len(voci))

        gia = set()
        if indice and voci:
            # il confronto con l'indice va fatto a blocchi: una query con
            # 65.000 termini e' troppo grande per OpenSearch
            for i in range(0, len(voci), 2000):
                chiavi = ["cerdef:" + v["id"] for v in voci[i:i + 2000]]
                gia |= conosciuti(chiavi)
            log("gia' in indice: %d" % len(gia))

        fatti = 0
        documenti_da_indicizzare = []
        for voce in voci:
            chiave_fonte = "cerdef:" + voce["id"]
            if chiave_fonte in gia and not args.rifai:
                riassunto["saltati"] += 1
                continue
            if args.massimo and riassunto["nuovi"] >= args.massimo:
                break
            try:
                dati = dettaglio(contesto, voce["id"])
                documento = scheda(dati, voce)
            except Exception as e:
                riassunto["falliti"] += 1
                log("  %s: %s" % (voce["estremi"][:70], e))
                continue
            riassunto["nuovi"] += 1
            if cliente is not None:
                try:
                    corpo = pdf_da_scheda(documento)
                    documento["chiave_s3"] = carica_pdf(cliente, documento, corpo)
                    salva_record(cliente, documento)
                except Exception as e:
                    log("  PDF/S3 di %s: %r" % (documento["id_nodo"], e))
                    riassunto["falliti"] += 1
                    continue
            if indice:
                documenti_da_indicizzare.append(documento)
                if len(documenti_da_indicizzare) >= RIGHE_PER_LOTTO:
                    try:
                        indicizza(documenti_da_indicizzare)
                    except Exception as e:
                        log("indicizzazione: %r" % (e,))
                        riassunto["falliti"] += len(documenti_da_indicizzare)
                    documenti_da_indicizzare = []
            if args.prova:
                log("  prova: %s (%d caratteri)" % (documento["titolo"][:80], documento["caratteri"]))
            fatti += 1
            if fatti % 25 == 0:
                log("  ... %d nuovi, %d saltati, %d falliti"
                    % (riassunto["nuovi"], riassunto["saltati"], riassunto["falliti"]))
            time.sleep(PAUSA_FRA_DOCUMENTI)
        if indice and documenti_da_indicizzare:
            try:
                indicizza(documenti_da_indicizzare)
            except Exception as e:
                log("indicizzazione finale: %r" % (e,))
                riassunto["falliti"] += len(documenti_da_indicizzare)
    finally:
        try:
            if navigatore:
                navigatore.close()
            if play:
                play.stop()
        except Exception:
            pass

    riassunto["secondi"] = int(time.time() - inizio)
    riassunto["esito"] = 1 if riassunto["falliti"] else 0
    if not args.prova:
        scrivi_stato(riassunto)
        metrica("EsitoSyncCerdef", riassunto["esito"])
        metrica("DocumentiNuoviCerdef", riassunto["nuovi"])
        metrica("DocumentiNonScaricatiCerdef", riassunto["falliti"])
    log(json.dumps(riassunto, ensure_ascii=False))
    return riassunto["esito"]


def indicizza_da_s3(args):
    """Lato motore: i record lasciati su S3 dal giro di Milano vanno in indice."""
    crea_indice_se_manca()
    cliente = s3()
    chiavi = []
    paginatore = cliente.get_paginator("list_objects_v2")
    for pagina in paginatore.paginate(Bucket=BUCKET, Prefix=PREFISSO_REC + "/"):
        for oggetto in pagina.get("Contents", []):
            if oggetto["Key"].endswith(".json"):
                chiavi.append(oggetto["Key"])
    log("record su S3: %d" % len(chiavi))
    documenti = []
    for chiave in chiavi:
        try:
            corpo = cliente.get_object(Bucket=BUCKET, Key=chiave)["Body"].read()
            documenti.append(json.loads(corpo.decode("utf-8")))
        except Exception as e:
            log("record %s non letto: %r" % (chiave, e))
    gia = conosciuti([d["chiave_fonte"] for d in documenti if d.get("chiave_fonte")])
    nuovi = [d for d in documenti if d.get("chiave_fonte") not in gia]
    log("gia' in indice: %d, da indicizzare: %d" % (len(documenti) - len(nuovi), len(nuovi)))
    falliti = 0
    for i in range(0, len(nuovi), RIGHE_PER_LOTTO):
        try:
            _bulk(nuovi[i:i + RIGHE_PER_LOTTO])
        except Exception as e:
            falliti += len(nuovi[i:i + RIGHE_PER_LOTTO])
            log("lotto non indicizzato: %r" % (e,))
    riassunto = {"quando": adesso().isoformat().replace("+00:00", "Z"),
                 "documentiSuS3": len(documenti), "documentiIndicizzati": len(nuovi) - falliti,
                 "documentiFalliti": falliti, "indice": IDX, "versionePipeline": VERSIONE,
                 "esito": 1 if falliti else 0}
    scrivi_stato(riassunto)
    metrica("EsitoIndicizzaCerdef", riassunto["esito"])
    log(json.dumps(riassunto, ensure_ascii=False))
    return riassunto["esito"]


def passa_ocr(args):
    """Il testo del CERDEF e' nativo (viene dall'HTML, non da scansioni), quindi
    il PDF generato ha sempre lo strato di testo. Questo giro resta per
    simmetria con gli altri sync: cerca i documenti il cui testo e' corto e li
    segnala, senza riscrivere nulla."""
    crea_indice_se_manca()
    try:
        risposta = os_chiama("POST", "/%s/_search" % IDX, {
            "size": args.massimo or 200,
            "query": {"bool": {"must": [{"term": {"fonte": "cerdef"}},
                                        {"range": {"caratteri": {"lt": 200}}}]}},
            "_source": ["id_nodo", "titolo", "caratteri"],
        })
    except Exception as e:
        log("indice non interrogabile: %r" % (e,))
        return 2
    colpiti = risposta.get("hits", {}).get("hits", [])
    log("documenti CERDEF con testo corto: %d" % len(colpiti))
    for c in colpiti:
        log("  %s (%s caratteri)" % (c["_source"].get("titolo", "")[:70],
                                     c["_source"].get("caratteri")))
    return 0


def main():
    a = argparse.ArgumentParser(description="Le sentenze tributarie del CERDEF in Giustup")
    a.add_argument("--prova", type=int, default=0, metavar="N",
                   help="N documenti, senza toccare S3 ne' l'indice")
    a.add_argument("--massimo", type=int, default=0, help="tetto di documenti nuovi in questo giro")
    a.add_argument("--anno", type=int, default=0, help="solo un anno (filtro della fonte)")
    a.add_argument("--rifai", action="store_true", help="riscarica anche i documenti gia' in indice")
    a.add_argument("--senza-s3", action="store_true", help="non carica su S3")
    a.add_argument("--senza-indice", action="store_true", help="non tocca OpenSearch")
    a.add_argument("--salva-record", action="store_true",
                   help="(scissione Milano) salva anche il record JSON su S3 per il motore")
    a.add_argument("--indicizza-da-s3", action="store_true",
                   help="(scissione motore) indicizza i record lasciati su S3 da Milano")
    a.add_argument("--ocr", action="store_true", help="cerca i documenti con testo corto")
    a.add_argument("--stato", action="store_true", help="stampa lo stato dell'ultimo giro")
    args = a.parse_args()

    global _salva_record
    _salva_record = not args.prova and not args.senza_s3 and bool(args.salva_record)

    if args.stato:
        print(json.dumps(leggi_stato(), ensure_ascii=False, indent=2))
        return 0
    if args.indicizza_da_s3:
        return indicizza_da_s3(args)
    if args.ocr:
        return passa_ocr(args)
    return sincronizza(args)


if __name__ == "__main__":
    sys.exit(main())
