#!/usr/bin/env python3
"""Veille Riftbound sur la boutique Riot (merch.riotgames.com).

Envoie une alerte Discord à chaque changement d'état d'un produit :
  ✅ en stock · 🟡 précommande · ❌ rupture · ⚫ retiré · 🆕 nouveau produit

Aucune dépendance : bibliothèque standard Python uniquement.
Variables d'environnement :
  DISCORD_WEBHOOK_URL  (obligatoire) URL du webhook Discord
  DISCORD_MENTION      (optionnel)  ex. "@everyone" ou "<@123456789>"
  RIOT_LOCALE          (optionnel)  défaut "fr-fr"
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
    prix = p.group(0).replace(" ", "") if p else ""
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


# ───────────────────────── Discord ─────────────────────────

def discord(message):
    if not WEBHOOK:
        print("[Discord non configuré]\n" + message)
        return
    data = json.dumps({"content": message[:1990]}).encode()
    for essai in (1, 2):
        req = urllib.request.Request(WEBHOOK, data=data, headers={
            "Content-Type": "application/json", "User-Agent": "riftbound-watch/2.0"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and essai == 1:      # trop de messages d'un coup : on attend
                time.sleep(float(e.headers.get("Retry-After", "2")) + 0.5)
                continue
            print(f"  ! envoi Discord échoué : HTTP {e.code}")
            raise
        except Exception as e:
            print(f"  ! envoi Discord échoué : {e}")
            raise
    time.sleep(min(PAUSE, 1))


def ligne(slug, info):
    limitee = "⭐ ÉDITION LIMITÉE — " if info.get("limitee") else ""
    icone = "📦 " if "display" in slug else ""
    prix = f" — {info['prix']}" if info.get("prix") else ""
    return f"{limitee}{icone}**{info.get('titre') or slug}**{prix}\n{BASE}/{LOCALE}/product/{slug}/"


def court(titre):
    """Titre raccourci pour le récapitulatif (sans « Riftbound: League of Legends TCG »)."""
    t = re.sub(r"\s*Riftbound\s*:?\s*League of Legends\s*(?:™|ᵀᴹ)?\s*(?:TCG)?\s*", " ", titre)
    return re.sub(r"\s+", " ", t).strip() or titre


def exclu(slug):
    return any(m in slug for m in EXCLUS)


def recap(produits):
    groupes = {}
    for slug, v in sorted(produits.items()):
        nom = ("⭐ " if v.get("limitee") else "") + court(v.get("titre") or slug)
        groupes.setdefault(v["statut"], []).append(nom)
    texte = [f"👀 Veille Riftbound activée — {len(produits)} produits suivis"]
    for st in ("stock", "precommande", "rupture", "retire", "inconnu"):
        if st in groupes:
            note = " (état non lisible sur la page : pas de suivi fiable)" if st == "inconnu" else ""
            texte.append(f"\n{LIBELLES[st]} ({len(groupes[st])}){note} :\n" + " · ".join(groupes[st]))
    return "\n".join(texte)


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


def main():
    produits = charger()
    premiere_fois = produits is None
    produits = {k: v for k, v in (produits or {}).items() if not exclu(k)}

    slugs = sorted(s for s in set(PRODUITS_CONNUS) | decouvrir() | set(produits) if not exclu(s))
    alertes = []          # (slug, texte, ping)
    a_verifier = []       # slugs ambigus : « rupture » ET un bouton d'achat apparu
    fiches_changees = []  # fiches « illisibles » dont les données de stock ont changé
    ok = echecs = 0

    for slug in slugs:
        code, page = telecharger(f"{BASE}/{LOCALE}/product/{slug}/")
        time.sleep(PAUSE)

        if code == 404:
            fiche = produits.get(slug)
            if fiche is not None:
                fiche["absent"] = fiche.get("absent", 0) + 1
                if fiche["absent"] == 2 and fiche["statut"] != "retire":   # 2 fois de suite : pas un raté
                    alertes.append((slug, f"🗑️ PRODUIT RETIRÉ DE LA BOUTIQUE (avant : "
                                          f"{LIBELLES[fiche['statut']]})\n{ligne(slug, fiche)}", False))
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
                alertes.append((slug, f"🆕 NOUVEAU PRODUIT — {LIBELLES[info['statut']]}\n"
                                      f"{ligne(slug, info)}", achetable))
            elif changement(avant["statut"], info["statut"]):
                alertes.append((slug, f"🔄 {LIBELLES[avant['statut']]} → {LIBELLES[info['statut']]}\n"
                                      f"{ligne(slug, info)}", achetable))
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
    if premiere_fois:
        discord(recap(produits))
        envoyes = 1
    else:
        # achetables d'abord, puis éditions limitées, puis displays
        alertes.sort(key=lambda a: (not a[2], "⭐" not in a[1], "display" not in a[0]))
        for slug, texte, ping in alertes[:MAX_ALERTES]:
            discord((f"{MENTION} " if ping and MENTION else "") + texte)
        if len(alertes) > MAX_ALERTES:
            discord(f"… et {len(alertes) - MAX_ALERTES} autre(s) changement(s) : regarde la boutique.")
        if a_verifier:
            liste = "\n".join(f"• {produits[s].get('titre') or s}\n  {BASE}/{LOCALE}/product/{s}/"
                              for s in a_verifier[:8])
            discord(f"{MENTION + ' ' if MENTION else ''}⚠️ À VÉRIFIER — un bouton d'achat est apparu "
                    f"alors que « rupture » est encore affiché (restock possible) :\n{liste}")
        if fiches_changees:
            liste = "\n".join(f"• {produits[s].get('titre') or s}\n  {BASE}/{LOCALE}/product/{s}/"
                              for s in fiches_changees[:8])
            discord("🔎 CHANGEMENT SUR UNE FICHE ILLISIBLE — l'état d'achat n'est pas lisible, mais les "
                    f"données de stock de la page ont changé (restock possible) :\n{liste}")
        envoyes = len(alertes) + (1 if a_verifier else 0) + (1 if fiches_changees else 0)
        # Lancement manuel : confirmation que tout tourne, même sans changement.
        if envoyes == 0 and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            discord(f"✅ Veille Riftbound OK — {len(produits)} produits suivis, aucun changement.")

    with open(ETAT, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "produits": produits}, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Terminé : {ok} pages lues, {echecs} échecs, {envoyes} message(s) envoyé(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
