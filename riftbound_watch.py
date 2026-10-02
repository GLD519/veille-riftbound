#!/usr/bin/env python3
"""Veille Riftbound sur la boutique Riot (merch.riotgames.com).

Envoie une alerte Discord à chaque changement d'état d'un produit :
  ✅ en stock · 🟡 précommande · ❌ rupture · ⚫ retiré · 🆕 nouveau produit

Aucune dépendance : bibliothèque standard Python uniquement.
Variables d'environnement :
  DISCORD_WEBHOOK_URL  (obligatoire) URL du webhook Discord
  DISCORD_MENTION      (optionnel)  ex. "@everyone" ou "<@123456789>"
  RIOT_LOCALE          (optionnel)  défaut "fr-fr"

Un lancement manuel (« Run workflow ») envoie en plus un récapitulatif de tous les produits.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

VERSION = 2  # change quand la logique de détection change : l'état est alors réinitialisé
BASE = os.environ.get("RIOT_BASE_URL", "https://merch.riotgames.com").rstrip("/")
LOCALE = os.environ.get("RIOT_LOCALE", "fr-fr")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
MENTION = os.environ.get("DISCORD_MENTION", "")
ETAT = os.environ.get("RIFTBOUND_ETAT", "riftbound_etat.json")
PAUSE = float(os.environ.get("RIFTBOUND_PAUSE", "1"))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# Produits déjà connus (le sitemap de Riot n'est pas toujours complet).
# Les nouveaux produits sont découverts automatiquement, inutile de tenir cette liste à jour.
PRODUITS_CONNUS = [
    "riftbound-origins-booster-display",
    "riftbound-spiritforged-booster-display",
    "riftbound-unleashed-booster-display",
    "riftbound-vendetta-booster-display",
    "riftbound-radiance-booster-display",
    "riftbound-origins-champion-deck-viktor",
    "riftbound-origins-champion-deck-jinx",
    "riftbound-origins-champion-deck-leesin",
    "riftbound-spiritforged-champion-deck-rumble",
    "riftbound-proving-grounds",
    "riftbound-arcane-box-set",
    "riftbound-worlds-bundle-2025",
    "riftbound-vendetta-zed-shen-showdown",
    "riftbound-radiance-evelynn-seraphine-showdown",
    "riftbound-t1-2025-worlds-signature-edition",
]

MARQUEURS = {
    "rupture": ["rupture de stock", "épuisé", "epuise", "out of stock", "sold out"],
    "precommande": ["précommander", "précommande", "pré-commande", "precommande", "pre-order", "preorder"],
    "stock": ["ajouter au panier", "add to cart", "add to bag"],
}
ACHETABLE = ("stock", "precommande")
LIBELLES = {
    "stock": "✅ EN STOCK",
    "precommande": "🟡 PRÉCOMMANDE",
    "rupture": "❌ Rupture de stock",
    "retire": "⚫ Retiré de la vente",
    "inconnu": "❔ État illisible",
}
COULEURS = {"stock": 0x2ECC71, "precommande": 0xF1C40F, "rupture": 0xE74C3C,
            "retire": 0x95A5A6, "inconnu": 0xE67E22}
COULEUR_NOUVEAU = 0x3498DB
COULEUR_ALERTE = 0xE67E22
# Sections « produits liés » : leur texte ne concerne pas le produit de la page.
COUPURES = ("Voir la collection", "Vous aimerez aussi", "Produits similaires",
            "Shop the collection", "You may also like")
MAX_ALERTES = 10
EXCLUS = ("sleeves", "playmat")   # accessoires : non suivis (decks, displays et éditions limitées seulement)
# Données de stock cachées dans la page (pour les fiches dont le bouton d'achat se charge après coup).
SIGNAL_RE = re.compile(
    r'\\?"([A-Za-z_]*(?:stock|avail|sold|preorder|pre_order|orderable|buyable|backorder)[A-Za-z_]*)\\?"'
    r'\s*:\s*(true|false|null|\\?"[A-Za-z_ -]{1,30}\\?")', re.I)
LIMITEE_RE = re.compile(r"[ée]dition limit[ée]e|limited edition|signature edition", re.I)


# ───────────────────────── Lecture de la boutique ─────────────────────────

def telecharger(url):
    """Retourne (code_http, texte). code 0 = erreur réseau."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # réseau, timeout…
        print(f"  ! erreur réseau {url} : {e}")
        return 0, ""


def texte_visible(page):
    """Texte affiché de la page (sans en-tête, scripts, styles ni gabarits cachés)."""
    page = re.sub(r"(?is)<head\b.*?</head>", " ", page)
    page = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def marqueurs_trouves(texte):
    t = texte.casefold()
    return sorted(k for k, mots in MARQUEURS.items() if any(m in t for m in mots))


def zone_produit(visible, titre=""):
    """Garde le texte du produit : ni le menu avant le titre, ni la section « produits liés » après."""
    trouve = bool(titre) and titre in visible
    debut = visible.find(titre) if trouve else 0
    depart = debut + len(titre) if trouve else 200
    coupes = [p for p in (visible.find(c, depart) for c in COUPURES) if p >= 0]
    fin = min(coupes) if coupes else len(visible)
    return visible[debut:fin]


def analyser(page):
    """Extrait titre, prix, statut et marqueurs d'une page produit."""
    m = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page)
         or re.search(r"(?is)<title[^>]*>(.*?)</title>", page))
    titre = html.unescape(m.group(1)).replace("\xa0", " ") if m else ""
    titre = re.split(r"\s*\|\s*", titre)[0].strip()

    visible = zone_produit(texte_visible(page), titre)
    trouves = marqueurs_trouves(visible)

    if "rupture" in trouves:
        statut = "rupture"
    elif re.search(r"\bretiré\b", visible, re.I) and set(trouves) <= {"precommande"}:
        statut = "retire"          # fiche périmée : badge « Retiré » + vieux badge « Précommander »
    elif "stock" in trouves:
        statut = "stock"           # un vrai bouton d'achat prime sur un badge « Précommander »
    elif "precommande" in trouves:
        statut = "precommande"
    else:
        statut = "inconnu"         # aucune mention lisible : on ne devine pas

    # Un prix a toujours des centimes : évite de confondre avec une année (« 2025 € »).
    p = re.search(r"€\s?\d{1,4}[.,]\d{2}|\d{1,4}[.,]\d{2}\s?€", visible)
    prix = ""
    if p:
        nombre = float(re.sub(r"[€\s]", "", p.group(0)).replace(",", "."))
        prix = f"{nombre:.2f}".replace(".", ",") + " €"      # format français : « 127,99 € »
    info = {"titre": titre, "prix": prix, "statut": statut, "marqueurs": trouves,
            "limitee": bool(LIMITEE_RE.search(f"{titre} {visible}")), "extrait": visible[:300]}
    if statut == "inconnu":
        info["signaux"] = signaux(page)
    return info


def signaux(page):
    """Indicateurs de stock trouvés dans les données de la page, triés (pour repérer un changement)."""
    vus = {f"{cle}={val.replace(chr(92), '').strip(chr(34))}" for cle, val in SIGNAL_RE.findall(page)}
    return sorted(vus)[:60]


def decouvrir():
    """Cherche les fiches produit Riftbound dans le sitemap et la page catégorie."""
    slugs = set()
    for url in (f"{BASE}/sitemap.xml", f"{BASE}/{LOCALE}/category/riftbound/"):
        code, page = telecharger(url)
        if code == 200:
            slugs.update(re.findall(r"/product/([a-z0-9-]*riftbound[a-z0-9-]*)/?", page))
        else:
            print(f"  ! découverte impossible ({code}) : {url}")
        time.sleep(PAUSE)
    return slugs


# ───────────────────────── Présentation ─────────────────────────

def url_produit(slug):
    return f"{BASE}/{LOCALE}/product/{slug}/"


def court(titre):
    """Titre raccourci (sans « Riftbound: League of Legends TCG »)."""
    t = re.sub(r"\s*Riftbound\s*:?\s*League of Legends\s*(?:™|ᵀᴹ)?\s*(?:TCG)?\s*", " ", titre)
    t = re.sub(r"^\s*Riftbound\s*:\s*", "", t)
    t = re.sub(r"\s*League of Legends\s*(?:™|ᵀᴹ)?\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip() or titre


def icone_type(slug):
    if "display" in slug:
        return "📦"
    if "deck" in slug or "showdown" in slug:
        return "🃏"
    if "proving" in slug:
        return "🎲"
    return "🎁"   # packs, coffrets, bundles, éditions spéciales


def nom_lien(slug, info, gras=False):
    nom = court(info.get("titre") or slug).replace("[", "(").replace("]", ")")
    lien = f"[{nom}]({url_produit(slug)})"
    return f"{icone_type(slug)} " + (f"**{lien}**" if gras else lien)


def carte(slug, info, titre, couleur):
    """Carte Discord pour une alerte : 2 lignes (nom cliquable, puis prix et mentions)."""
    details = []
    if info.get("prix"):
        details.append(f"💶 {info['prix']}")
    if info.get("limitee"):
        details.append("⭐ Édition limitée")
    description = nom_lien(slug, info, gras=True) + ("\n" + " · ".join(details) if details else "")
    return {"title": titre[:250], "description": description, "color": couleur, "url": url_produit(slug)}


def carte_groupe(titre, slugs, produits, texte):
    lignes = "\n\n".join(carte(s, produits[s], "", 0)["description"] for s in slugs[:8])
    return {"title": titre, "description": f"{texte}\n\n{lignes}", "color": COULEUR_ALERTE}


def embeds_liste(titre, lignes, couleur, limite=3500):
    """Découpe une longue liste en plusieurs cartes (limite de Discord : 4096 caractères)."""
    morceaux, courant, taille = [], [], 0
    for ligne in lignes:
        if courant and taille + len(ligne) + 1 > limite:
            morceaux.append(courant)
            courant, taille = [], 0
        courant.append(ligne)
        taille += len(ligne) + 1
    if courant:
        morceaux.append(courant)
    return [{"title": titre if i == 0 else f"{titre} (suite)", "description": "\n".join(m), "color": couleur}
            for i, m in enumerate(morceaux)]


def recap(produits):
    """Récapitulatif : une carte par état, un produit par ligne."""
    groupes = {}
    for slug, v in sorted(produits.items(), key=lambda kv: (not kv[1].get("limitee"), kv[0])):
        groupes.setdefault(v["statut"], []).append((slug, v))   # éditions limitées d'abord
    embeds = []
    for st in ("stock", "precommande", "rupture", "retire", "inconnu"):
        if st not in groupes:
            continue
        lignes = []
        for slug, v in groupes[st]:
            etoile = "⭐ " if v.get("limitee") else ""
            prix = f" · {v['prix']}" if v.get("prix") else ""
            lignes.append(f"{etoile}{nom_lien(slug, v)}{prix}")
        if st == "inconnu":
            lignes.append("\n*Aucune mention de stock lisible sur ces pages : tu es prévenu "
                          "seulement si leurs données changent.*")
        embeds += embeds_liste(f"{LIBELLES[st]} ({len(groupes[st])})", lignes, COULEURS[st])
    return f"👀 **Veille Riftbound** — {len(produits)} produits suivis", embeds


# ───────────────────────── Discord ─────────────────────────

def texte_simple(payload):
    morceaux = [payload.get("content") or ""]
    for e in payload.get("embeds", []):
        morceaux.append(f"{e.get('title', '')}\n{e.get('description', '')}")
    return "\n\n".join(m for m in morceaux if m)


def poster(payload):
    req = urllib.request.Request(WEBHOOK, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "User-Agent": "riftbound-watch/3.0"})
    urllib.request.urlopen(req, timeout=30).read()


def envoyer(payload):
    for essai in (1, 2, 3):
        try:
            poster(payload)
            time.sleep(min(PAUSE, 1))
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and essai < 3:                 # trop de messages d'un coup : on attend
                time.sleep(float(e.headers.get("Retry-After", "2")) + 0.5)
                continue
            if e.code == 400 and payload.get("embeds"):     # mise en forme refusée : repli en texte simple
                print("  ! Discord a refusé la mise en forme, envoi en texte simple")
                payload = {"content": texte_simple(payload)[:1990]}
                continue
            print(f"  ! envoi Discord échoué : HTTP {e.code}")
            raise
        except Exception as e:
            print(f"  ! envoi Discord échoué : {e}")
            raise


def discord(contenu="", embeds=None):
    """Envoie un message (texte et/ou cartes). Discord limite : 10 cartes et ~6000 caractères par message."""
    embeds = embeds or []
    if not WEBHOOK:
        print("[Discord non configuré]\n" + texte_simple({"content": contenu, "embeds": embeds}))
        return
    lots, lot, total = [], [], 0
    for e in embeds:
        t = len(e.get("title", "")) + len(e.get("description", ""))
        if lot and (len(lot) >= 10 or total + t > 5500):
            lots.append(lot)
            lot, total = [], 0
        lot.append(e)
        total += t
    lots.append(lot)
    for i, lot in enumerate(lots):
        texte = contenu[:1990] if i == 0 else ""
        if texte or lot:
            envoyer({"content": texte, "embeds": lot})


# ───────────────────────── Logique d'alerte ─────────────────────────

def changement(avant, apres):
    """Faut-il alerter pour ce passage d'un état à l'autre ?"""
    if apres == "inconnu" or avant == apres:
        return False
    if avant == "inconnu":
        return apres in ACHETABLE   # on ne sait pas d'où l'on vient : seulement si c'est achetable
    return True


def charger():
    """Retourne l'état enregistré, ou None s'il n'y en a pas (ou s'il est d'une ancienne version)."""
    try:
        with open(ETAT, encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if isinstance(d, dict) and d.get("version") == VERSION and isinstance(d.get("produits"), dict):
        return d["produits"]
    return None


def exclu(slug):
    return any(m in slug for m in EXCLUS)


def main():
    produits = charger()
    premiere_fois = produits is None
    produits = {k: v for k, v in (produits or {}).items() if not exclu(k)}

    slugs = sorted(s for s in set(PRODUITS_CONNUS) | decouvrir() | set(produits) if not exclu(s))
    alertes = []          # (slug, info, titre, couleur, ping)
    a_verifier = []       # slugs ambigus : « rupture » ET un bouton d'achat apparu
    fiches_changees = []  # fiches « illisibles » dont les données de stock ont changé
    ok = echecs = 0

    for slug in slugs:
        code, page = telecharger(url_produit(slug))
        time.sleep(PAUSE)

        if code == 404:
            fiche = produits.get(slug)
            if fiche is not None:
                fiche["absent"] = fiche.get("absent", 0) + 1
                if fiche["absent"] == 2 and fiche["statut"] != "retire":   # 2 fois de suite : pas un raté
                    alertes.append((slug, fiche, f"🗑️ Retiré de la boutique (avant : {LIBELLES[fiche['statut']]})",
                                    COULEURS["retire"], False))
                    fiche["statut"] = "retire"
            print(f"  - {slug} : absent de la boutique (404)")
            continue
        if code != 200 or "riftbound" not in page.casefold():
            echecs += 1
            print(f"  ! {slug} : page illisible (HTTP {code}), état conservé")
            continue

        ok += 1
        info = analyser(page)
        extrait = info.pop("extrait")
        avant = produits.get(slug)
        print(f"  - {slug} : {info['statut']} {info['marqueurs']} {info['prix']}")
        if info["statut"] == "inconnu":
            print(f"      extrait : {extrait[:250]}")
            print(f"      signaux : {', '.join(info['signaux'][:30]) or 'aucun'}")

        if not premiere_fois:
            achetable = info["statut"] in ACHETABLE
            if avant is None:
                alertes.append((slug, info, f"🆕 Nouveau produit — {LIBELLES[info['statut']]}",
                                COULEUR_NOUVEAU, achetable))
            elif changement(avant["statut"], info["statut"]):
                alertes.append((slug, info, f"🔄 {LIBELLES[avant['statut']]} → {LIBELLES[info['statut']]}",
                                COULEURS[info["statut"]], achetable))
            elif (info["statut"] == "rupture" and avant["statut"] == "rupture"
                  and (set(info["marqueurs"]) - set(avant.get("marqueurs", []))) & set(ACHETABLE)):
                a_verifier.append(slug)
            elif (info["statut"] == "inconnu" and avant["statut"] == "inconnu"
                  and "signaux" in avant and info["signaux"] != avant["signaux"]):
                fiches_changees.append(slug)
        produits[slug] = info

    if ok == 0:
        print("Aucune page produit lisible : la boutique bloque peut-être les requêtes.")
        return 1

    envoyes = 0
    if not premiere_fois:
        # achetables d'abord, puis éditions limitées, puis displays
        alertes.sort(key=lambda a: (not a[4], not a[1].get("limitee"), "display" not in a[0]))
        for slug, info, titre, couleur, ping in alertes[:MAX_ALERTES]:
            discord(MENTION if ping else "", [carte(slug, info, titre, couleur)])
        if len(alertes) > MAX_ALERTES:
            discord(f"… et {len(alertes) - MAX_ALERTES} autre(s) changement(s) : regarde la boutique.")
        if a_verifier:
            discord(MENTION, [carte_groupe("⚠️ À vérifier — restock possible", a_verifier, produits,
                                           "Un bouton d'achat est apparu alors que « rupture » est encore affiché.")])
        if fiches_changees:
            discord("", [carte_groupe("🔎 Changement sur une fiche illisible", fiches_changees, produits,
                                      "L'état d'achat n'est pas lisible, mais les données de stock "
                                      "de la page ont changé (restock possible).")])
        envoyes = len(alertes) + (1 if a_verifier else 0) + (1 if fiches_changees else 0)

    # Premier lancement ou lancement manuel (« Run workflow ») : récapitulatif de tous les produits.
    if premiere_fois or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        discord(*recap(produits))
        envoyes += 1

    with open(ETAT, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "produits": produits}, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Terminé : {ok} pages lues, {echecs} échecs, {envoyes} message(s) envoyé(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
