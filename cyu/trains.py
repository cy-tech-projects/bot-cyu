#!/usr/bin/env python3
"""
Les prochains trains : RER, Transilien, metro et tram, en temps reel.

Deux sources, toutes deux d'Ile-de-France Mobilites :

    l'open data     la liste des gares et celle des lignes (nom, couleur).
                    Sans compte ni cle : telechargees une fois, gardees un
                    mois dans donnees/ ;
    PRIM            les prochains passages, en temps reel. Il faut une cle
                    (gratuite) : `trains.cle_api` dans config.yaml.

Une « gare » est ici ce que tout le monde appelle une gare : La Defense est
une seule gare, meme si IDFM y range le RER, le metro et le tram dans trois
zones d'arret distinctes. On regroupe donc par nom et par ville, et on
interroge chaque zone.

Le sens d'un train se lit sur sa DESTINATION : « vers Cergy » garde ceux dont
le terminus contient « cergy ». Un terme precede de « - » exclut : « -cergy »
a Cergy-Prefecture, c'est tout ce qui part vers Paris, que le RER finisse a
Boissy, a Torcy ou a Marne-la-Vallee.

Comme pour la meteo, le reseau n'est jamais une raison de planter : les
referentiels se rabattent sur le dernier fichier connu, et une panne de PRIM
devient un message lisible (ErreurTrains), jamais une trace.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from . import config
from .edt import normaliser


OPENDATA = "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets"
PRIM = "https://prim.iledefrance-mobilites.fr/marketplace/stop-monitoring"
UA = {"User-Agent": "assistant-cyu/2.0"}

# Les referentiels bougent quelques fois par an : un mois de cache suffit.
REFERENTIEL_JOURS = 30
# Deux personnes qui tapent /trains a dix secondes d'intervalle n'ont pas
# besoin de deux appels PRIM : le temps reel n'a pas bouge.
CACHE_PASSAGES_SECONDES = 25

# Les zones d'arret utiles : gares, stations de metro, arrets de tram. Les
# 17 000 arrets de bus noieraient la recherche.
TYPES_ZONES = ("railStation", "metroStation", "onstreetTram")
MODES_LIGNES = ("rail", "metro", "tram", "funicular", "cableway")


class ErreurTrains(RuntimeError):
    """Un probleme a expliquer tel quel : cle absente, refusee, PRIM en panne."""


# --- Ce qu'on manipule -------------------------------------------------------
@dataclass(frozen=True)
class Gare:
    nom: str
    ville: str
    ids: tuple          # les zones d'arret IDFM (« 47923 »…)

    @property
    def cle(self):
        """L'identifiant court qui voyage dans les boutons : la premiere zone."""
        return self.ids[0]

    @property
    def libelle(self):
        return f"{self.nom} ({self.ville})" if self.ville else self.nom


@dataclass(frozen=True)
class Ligne:
    id: str             # « C01742 »
    nom: str            # « A », « L », « 14 », « T2 »
    mode: str           # rail · metro · tram
    reseau: str = ""    # RER · Transilien · TER…
    fond: tuple = (110, 110, 120)
    texte: tuple = (255, 255, 255)

    @property
    def complet(self):
        """« RER A », « Ligne L », « Métro 14 », « Tram T2 »."""
        if self.reseau == "RER":
            return f"RER {self.nom}"
        if self.reseau == "Transilien":
            return f"Ligne {self.nom}"
        if self.mode == "metro":
            return f"Métro {self.nom}"
        if self.mode == "tram":
            return self.nom if self.nom.upper().startswith("T") else f"Tram {self.nom}"
        return self.nom

    @property
    def rond(self):
        """Le RER et le metro ont une pastille ronde, le Transilien carree."""
        return self.reseau == "RER" or self.mode == "metro"


@dataclass
class Passage:
    ligne: Ligne
    destination: str
    direction: str
    mission: str
    voie: str
    heure: datetime         # l'heure annoncee (temps reel si connu)
    prevue: datetime        # l'heure theorique
    supprime: bool
    a_quai: bool

    @property
    def retard(self):
        """Le retard en minutes entieres, 0 a l'heure ou en avance. Arrondi par
        defaut : PRIM annonce souvent 30 a 50 s d'ecart sur un train « onTime »,
        qu'un « +1 » rouge ferait passer pour un retard."""
        return max(0, int((self.heure - self.prevue).total_seconds() // 60))

    def minutes(self, maintenant=None):
        maintenant = maintenant or datetime.now()
        return int((self.heure - maintenant).total_seconds() // 60)


@dataclass(frozen=True)
class Trajet:
    """Un favori de config.yaml : une gare, des lignes, une direction."""
    nom: str
    gare: str
    lignes: tuple = ()
    direction: str = ""


# --- Les referentiels (open data, sans cle) ----------------------------------
_VERROU = threading.Lock()
_MEMOIRE = {}


def _export(jeu, where, select):
    r = requests.get(f"{OPENDATA}/{jeu}/exports/json",
                     params={"where": where, "select": select},
                     timeout=30, headers=UA)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise ValueError(f"{jeu} : reponse vide")
    return data


def _referentiel(nom, chemin, telecharger):
    """Le referentiel `nom` : en memoire, sinon sur disque s'il a moins d'un
    mois, sinon telecharge. Un echec reseau ressort la copie perimee."""
    with _VERROU:
        if nom in _MEMOIRE:
            return _MEMOIRE[nom]
        cache = config.lire_json(chemin, {})
        try:
            age = (datetime.now() - datetime.fromisoformat(cache["lu_le"])).days
        except (KeyError, TypeError, ValueError):
            age = None
        if age is None or not 0 <= age < REFERENTIEL_JOURS:
            try:
                cache = {"lu_le": datetime.now().isoformat(timespec="seconds"),
                         "donnees": telecharger()}
                config.preparer_dossiers()
                config.ecrire_json(chemin, cache, indent=None)
            except (requests.RequestException, ValueError) as e:
                print(f"[!] referentiel {nom} indisponible ({type(e).__name__}: {e})",
                      flush=True)
        _MEMOIRE[nom] = cache.get("donnees") or []
        return _MEMOIRE[nom]


def _telecharger_gares():
    types = ",".join(f'"{t}"' for t in TYPES_ZONES)
    lignes = _export("zones-d-arrets", f"zdatype in ({types})",
                     "zdaid,zdaname,zdatown")
    groupes = {}
    for z in lignes:
        nom, ville = str(z.get("zdaname") or "").strip(), str(z.get("zdatown") or "").strip()
        if not nom or not z.get("zdaid"):
            continue
        groupes.setdefault((nom, ville), []).append(str(z["zdaid"]))
    return [{"nom": n, "ville": v, "ids": sorted(ids, key=int)}
            for (n, v), ids in sorted(groupes.items())]


def _telecharger_lignes():
    modes = ",".join(f'"{m}"' for m in MODES_LIGNES)
    return _export("referentiel-des-lignes", f"transportmode in ({modes})",
                   "id_line,shortname_line,transportmode,networkname,"
                   "colourweb_hexa,textcolourweb_hexa")


def gares():
    return [Gare(g["nom"], g["ville"], tuple(g["ids"]))
            for g in _referentiel("gares", config.FICHIER_GARES, _telecharger_gares)]


def _rgb(hexa, defaut):
    hexa = str(hexa or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", hexa):
        return defaut
    return tuple(int(hexa[i:i + 2], 16) for i in (0, 2, 4))


def lignes():
    """{« C01742 »: Ligne}"""
    if "lignes_objets" not in _MEMOIRE:
        brut = _referentiel("lignes", config.FICHIER_LIGNES, _telecharger_lignes)
        _MEMOIRE["lignes_objets"] = {
            l["id_line"]: Ligne(l["id_line"], str(l.get("shortname_line") or "?"),
                                str(l.get("transportmode") or ""),
                                str(l.get("networkname") or ""),
                                _rgb(l.get("colourweb_hexa"), (110, 110, 120)),
                                _rgb(l.get("textcolourweb_hexa"), (255, 255, 255)))
            for l in brut if l.get("id_line")}
    return _MEMOIRE["lignes_objets"]


# --- Trouver une gare --------------------------------------------------------
def _mots(txt):
    return re.sub(r"[^a-z0-9]+", " ", normaliser(txt)).split()


def chercher(saisie, n=25):
    """Les gares qui ressemblent a `saisie`, les plus probables d'abord.

    Chaque mot tape doit commencer un mot du nom (ou de la ville) : « etoile »
    trouve Charles de Gaulle - Etoile, « cergy pref » Cergy Prefecture."""
    tapes = _mots(saisie)
    if not tapes:
        return []
    trouvees = []
    for g in gares():
        mots_nom, mots_ville = _mots(g.nom), _mots(g.ville)
        if not all(any(m.startswith(t) for m in mots_nom + mots_ville) for t in tapes):
            continue
        dans_nom = all(any(m.startswith(t) for m in mots_nom) for t in tapes)
        exact = normaliser(g.nom) == normaliser(saisie)
        trouvees.append((not exact, not dans_nom, len(g.nom), g))
    trouvees.sort(key=lambda t: t[:3])
    return [t[3] for t in trouvees[:n]]


def gare(texte):
    """Une gare a partir de ce qu'on a tape, d'un identifiant de zone, ou de
    « Nom (Ville) » tel que l'autocompletion le propose. None si rien."""
    texte = str(texte or "").strip()
    if not texte:
        return None
    if texte.isdigit():
        return next((g for g in gares() if texte in g.ids), None)
    m = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", texte)
    if m:
        for g in gares():
            if normaliser(g.nom) == normaliser(m[1]) and normaliser(g.ville) == normaliser(m[2]):
                return g
    trouvees = chercher(texte, n=1)
    return trouvees[0] if trouvees else None


# --- Les passages (PRIM, avec cle) -------------------------------------------
_PASSAGES = {}


def _valeur(x):
    """SIRI range presque tout dans [{"value": ...}] ou {"value": ...}."""
    if isinstance(x, list):
        x = x[0] if x else {}
    return str(x.get("value") or "").strip() if isinstance(x, dict) else ""


def _heure(txt):
    """« 2026-11-15T14:08:00.000Z » en heure locale naive, comme le reste du
    projet (config regle le fuseau du processus)."""
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def lire(reponse, index_lignes=None):
    """Les Passage d'une reponse SIRI Lite de PRIM."""
    index_lignes = lignes() if index_lignes is None else index_lignes
    livraisons = (reponse.get("Siri", {}).get("ServiceDelivery", {})
                  .get("StopMonitoringDelivery") or [{}])
    sortie = []
    for visite in livraisons[0].get("MonitoredStopVisit") or []:
        trajet = visite.get("MonitoredVehicleJourney") or {}
        arret = trajet.get("MonitoredCall") or {}
        ref = _valeur(trajet.get("LineRef"))
        ident = next((p for p in ref.split(":") if re.fullmatch(r"C\d+", p)), ref)
        ligne = index_lignes.get(ident) or Ligne(
            ident, _valeur(trajet.get("PublishedLineName")) or "?", "")
        prevue = _heure(arret.get("AimedDepartureTime") or arret.get("AimedArrivalTime"))
        annoncee = _heure(arret.get("ExpectedDepartureTime")
                          or arret.get("ExpectedArrivalTime")) or prevue
        if annoncee is None:
            continue
        statut = f"{arret.get('DepartureStatus', '')} {arret.get('ArrivalStatus', '')}"
        sortie.append(Passage(
            ligne=ligne,
            destination=(_valeur(arret.get("DestinationDisplay"))
                         or _valeur(trajet.get("DestinationName")) or "?"),
            direction=_valeur(trajet.get("DirectionName")),
            mission=_valeur(trajet.get("JourneyNote")),
            voie=(_valeur(arret.get("ArrivalPlatformName"))
                  or _valeur(arret.get("DeparturePlatformName"))),
            heure=annoncee, prevue=prevue or annoncee,
            supprime="cancel" in statut.lower(),
            a_quai=bool(arret.get("VehicleAtStop"))))
    return sortie


def _appeler(zone):
    if not config.TRAINS_CLE_API:
        raise ErreurTrains("il manque la clé de l'API Île-de-France Mobilités : "
                           "`trains.cle_api` dans config.yaml (gratuite sur "
                           "prim.iledefrance-mobilites.fr).")
    try:
        r = requests.get(PRIM, params={"MonitoringRef": f"STIF:StopArea:SP:{zone}:"},
                         headers={**UA, "apikey": config.TRAINS_CLE_API,
                                  "Accept": "application/json"}, timeout=15)
    except requests.RequestException as e:
        raise ErreurTrains(f"PRIM ne répond pas ({type(e).__name__}).") from e
    if r.status_code in (401, 403):
        raise ErreurTrains("PRIM refuse la clé : vérifie `trains.cle_api`, et que tu es "
                           "abonné à l'API « Prochains passages - requête unitaire ».")
    if r.status_code == 429:
        raise ErreurTrains("quota PRIM atteint pour aujourd'hui, réessaie plus tard.")
    if r.status_code >= 400:
        raise ErreurTrains(f"PRIM a répondu HTTP {r.status_code}.")
    try:
        return r.json()
    except ValueError as e:
        raise ErreurTrains("PRIM a renvoyé une réponse illisible.") from e


def passages(g):
    """Tous les passages a venir dans la gare `g`, tries par heure.

    Une zone en panne n'empeche pas les autres de repondre ; l'erreur n'est
    levee que si AUCUNE n'a rien donne."""
    instant = time.monotonic()
    connu = _PASSAGES.get(g.cle)
    if connu and instant - connu[0] < CACHE_PASSAGES_SECONDES:
        return connu[1]
    sortie, erreur = [], None
    for zone in g.ids:
        try:
            sortie += lire(_appeler(zone))
        except ErreurTrains as e:
            erreur = e
    if erreur and not sortie:
        raise erreur
    limite = datetime.now().timestamp() - 60
    sortie = sorted((p for p in sortie if p.heure.timestamp() > limite),
                    key=lambda p: p.heure)
    _PASSAGES[g.cle] = (instant, sortie)
    return sortie


# --- Filtrer -----------------------------------------------------------------
def cle_ligne(txt):
    """« RER A », « a », « Ligne L », « M14 », « métro 14 » -> « a », « l », « 14 »."""
    mots = [m for m in _mots(txt) if m not in ("rer", "ligne", "metro", "tram", "transilien")]
    brut = "".join(mots)
    m = re.fullmatch(r"m(\d+b?)", brut)
    return m[1] if m else brut


def termes(direction):
    """« Cergy » -> ([« cergy »], []) ; « -Cergy, -Poissy » -> ([], [...])."""
    garder, exclure = [], []
    for morceau in re.split(r"[,;/]", str(direction or "")):
        morceau = normaliser(morceau)
        if morceau.startswith(("-", "sauf ", "pas ")):
            morceau = re.sub(r"^(-|sauf |pas )\s*", "", morceau)
            if morceau:
                exclure.append(morceau)
        elif morceau:
            garder.append(morceau)
    return garder, exclure


def filtrer(liste, lignes_voulues=(), direction=""):
    voulues = {cle_ligne(l) for l in lignes_voulues if str(l).strip()}
    garder, exclure = termes(direction)
    sortie = []
    for p in liste:
        if voulues and cle_ligne(p.ligne.nom) not in voulues:
            continue
        dest = normaliser(p.destination)
        if any(t in dest for t in exclure):
            continue
        if garder and not any(t in dest or t in normaliser(p.direction) for t in garder):
            continue
        sortie.append(p)
    return sortie


def lignes_de(liste):
    """Les lignes presentes, dans l'ordre RER, Transilien, metro, tram."""
    vues = {p.ligne.id: p.ligne for p in liste}
    rang = {"RER": 0, "Transilien": 1}
    return sorted(vues.values(), key=lambda l: (rang.get(l.reseau, 2 if l.mode == "metro" else 3),
                                                 l.nom))


def destinations(liste):
    """Les terminus presents, du plus frequent au moins frequent."""
    compte = {}
    for p in liste:
        compte[p.destination] = compte.get(p.destination, 0) + 1
    return sorted(compte, key=lambda d: (-compte[d], d))


# --- Les favoris -------------------------------------------------------------
def trajets():
    sortie = []
    for t in config.TRAINS_TRAJETS:
        if not isinstance(t, dict) or not str(t.get("gare") or "").strip():
            continue
        brut = t.get("lignes") or []
        lignes_ = tuple(str(l) for l in (brut if isinstance(brut, list) else [brut]))
        sortie.append(Trajet(str(t.get("nom") or t["gare"]).strip(), str(t["gare"]).strip(),
                             lignes_, str(t.get("direction") or "").strip()))
    return sortie


def tableau(trajet, maxi=5):
    """(Gare, passages filtres, erreur) pour un favori. Jamais d'exception :
    le panneau doit pouvoir montrer un trajet en panne a cote d'un autre."""
    g = gare(trajet.gare)
    if g is None:
        return None, [], f"gare introuvable : « {trajet.gare} »"
    try:
        return g, filtrer(passages(g), trajet.lignes, trajet.direction)[:maxi], ""
    except ErreurTrains as e:
        return g, [], str(e)
