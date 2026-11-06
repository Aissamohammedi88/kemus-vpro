#!/usr/bin/env bash
# =============================================================================
#  Kemus 1.0 — Installateur + Lanceur 1-commande
#  Compatible Ubuntu 24.04 / Debian 12+ (PEP 668 externally-managed),
#  Fedora, Arch, Mac, WSL. Aucune dependance requise (100% stdlib).
#
#  Usage :
#     bash install.sh                # installe PUIS lance Kemus (port 3922)
#     bash install.sh --port 4000    # lance sur un port custom
#     bash install.sh --user         # pip install --user (si possible)
#     bash install.sh --dev          # pip install -e (editable)
#     bash install.sh --no-launch    # installe sans lancer
#     bash install.sh --no-browser   # ne pas ouvrir le navigateur
#
#  Strategie d'installation (auto, dans l'ordre) :
#     1. pip install classique (venv actif, ou systeme non-geré)
#     2. Si PEP 668 (Ubuntu 24.04/Debian 12+)  -> venv dedie ~/.kemus/venv
#     3. Si pip/venv impossible                -> ZERO-INSTALL :
#        un lanceur ~/.local/bin/kemus qui execute kemus.py directement
#        (kemus est 100% stdlib Python : pip n'est PAS necessaire !)
# =============================================================================
set -uo pipefail
# Pas de 'set -e' : on gere les erreurs explicitement (chaines de fallback).

MODE="auto"
LAUNCH=1
OPEN_BROWSER=1
PORT="${KEMUS_PORT:-3922}"
VENVDIR="${KEMUS_VENV:-$HOME/.kemus/venv}"
BIN_DIR="$HOME/.local/bin"

while [ $# -gt 0 ]; do
  case "$1" in
    --user)       MODE="user" ;;
    --dev)        MODE="dev" ;;
    --no-launch)  LAUNCH=0 ;;
    --no-browser) OPEN_BROWSER=0 ;;
    --port)       shift; PORT="${1:-3922}" ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^#//; s/^ //'
      exit 0 ;;
    *)
      echo "Option inconnue : $1"
      echo "Usage : bash install.sh [--user|--dev] [--port N] [--no-launch] [--no-browser]"
      exit 1 ;;
  esac
  shift
done

PY="${PYTHON:-python3}"

echo "============================================================"
echo "  Kemus 1.0 - Installateur + Lanceur 1-commande"
echo "============================================================"

# --- 0. bash requis ----------------------------------------------------------
if [ -z "${BASH_VERSION:-}" ]; then
  echo "ERREUR : lancez avec bash :  bash install.sh"
  exit 1
fi

# --- 1. Python >= 3.8 ---------------------------------------------------------
PYVER="$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
if [ -z "$PYVER" ]; then
  echo "ERREUR : python3 introuvable. Installez Python 3.8+ puis relancez."
  exit 1
fi
if [ "$($PY -c 'import sys; print(1 if sys.version_info >= (3,8) else 0)')" != "1" ]; then
  echo "ERREUR : Python 3.8+ requis (trouve : $PYVER)."
  exit 1
fi
echo "  Python  : $PYVER"

# --- 2. Se placer dans le dossier du script ----------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$SCRIPT_DIR/kemus.py" ]; then
  echo "ERREUR : kemus.py introuvable dans $SCRIPT_DIR"
  echo "Placez-vous dans le dossier extrait (celui qui contient kemus.py) :"
  echo "   cd kemus && bash install.sh"
  exit 1
fi
cd "$SCRIPT_DIR" || exit 1
echo "  Dossier : $SCRIPT_DIR"

# --- 3. Detection de l'environnement ------------------------------------------
IN_VENV="$($PY -c 'import sys; print(1 if (sys.prefix != getattr(sys, "base_prefix", sys.prefix)) or hasattr(sys, "real_prefix") else 0)' 2>/dev/null || echo 0)"
STDLIB="$($PY -c 'import sysconfig; print(sysconfig.get_path("stdlib"))' 2>/dev/null || echo "")"
PEP668=0
[ "${KEMUS_FORCE_PEP668:-0}" = "1" ] && PEP668=1      # hook de test interne
if [ -n "$STDLIB" ] && [ -f "$STDLIB/EXTERNALLY-MANAGED" ]; then
  PEP668=1
fi
if [ "$IN_VENV" = "1" ]; then
  echo "  Environnement : venv actif -> pip direct"
elif [ "$PEP668" = "1" ]; then
  echo "  Environnement : PEP 668 (Ubuntu 24.04+/Debian 12) detecte"
else
  echo "  Environnement : python systeme classique"
fi

do_install() { $PY -m pip install "$@" . --quiet --disable-pip-version-check --no-input; }

# Lanceur zero-install : script shell qui appelle kemus.py (100% stdlib)
make_zero_install_wrapper() {
  mkdir -p "$BIN_DIR" 2>/dev/null || return 1
  cat > "$BIN_DIR/kemus" <<WRAPPER || return 1
#!/usr/bin/env bash
# Kemus launcher (zero-install) — 100% stdlib, aucune dependance
exec "$PY" "$SCRIPT_DIR/kemus.py" "\$@"
WRAPPER
  chmod +x "$BIN_DIR/kemus" 2>/dev/null || true
  return 0
}

INSTALL_MODE=""     # "pip" | "venv" | "zero" | ""
install_ok=0

# --- 4A. venv actif : pip direct ----------------------------------------------
if [ "$IN_VENV" = "1" ]; then
  echo ">>> Installation pip (venv actif)..."
  if do_install; then
    install_ok=1; INSTALL_MODE="pip"
  fi
fi

# --- 4B. PEP 668 : venv dedie ~/.kemus/venv -----------------------------------
if [ "$install_ok" != "1" ] && [ "$PEP668" = "1" ] && [ "${KEMUS_NO_VENV:-0}" != "1" ]; then
  echo ">>> Creation d'un venv dedie : $VENVDIR ..."
  if [ ! -x "$VENVDIR/bin/python" ]; then
    rm -rf "$VENVDIR" 2>/dev/null
    $PY -m venv "$VENVDIR" >/dev/null 2>&1
  fi
  # venv sans pip (ensurepip manquant) : on tente de le doter de pip
  if [ -x "$VENVDIR/bin/python" ] && [ ! -x "$VENVDIR/bin/pip" ]; then
    "$VENVDIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
  fi
  if [ -x "$VENVDIR/bin/python" ] && [ -x "$VENVDIR/bin/pip" ]; then
    echo ">>> Installation pip dans le venv dedie..."
    if "$VENVDIR/bin/python" -m pip install . --quiet --disable-pip-version-check --no-input 2>/dev/null; then
      PY="$VENVDIR/bin/python"
      install_ok=1; INSTALL_MODE="venv"
      echo "  OK : venv dedie $VENVDIR"
    else
      rm -rf "$VENVDIR" 2>/dev/null
      echo "  pip dans le venv impossible (offline ?) -> fallback zero-install"
    fi
  else
    rm -rf "$VENVDIR" 2>/dev/null
    echo "  Creation venv impossible (python3-venv manquant ?)"
    echo "  Hint : sudo apt install python3-venv   (optionnel)"
  fi
fi

# --- 4C. Mode --user / --dev explicites ---------------------------------------
if [ "$install_ok" != "1" ]; then
  case "$MODE" in
    user)
      echo ">>> Installation utilisateur (pip install --user .)..."
      if do_install --user; then install_ok=1; INSTALL_MODE="pip"
      elif [ "$PEP668" = "1" ]; then
        echo "  Bloque par PEP 668 -> fallback zero-install"
      fi
      ;;
    dev)
      echo ">>> Installation editable (pip install -e .)..."
      if do_install -e; then install_ok=1; INSTALL_MODE="pip"
      elif [ "$PEP668" = "1" ]; then
        echo "  Bloque par PEP 668 -> fallback zero-install"
      fi
      ;;
  esac
fi

# --- 4D. Systeme classique (auto) ---------------------------------------------
if [ "$install_ok" != "1" ] && [ "$MODE" = "auto" ] && [ "$IN_VENV" != "1" ] && [ "$PEP668" != "1" ]; then
  echo ">>> Installation systeme (pip install .)..."
  if do_install; then
    install_ok=1; INSTALL_MODE="pip"
  else
    echo "  Echec systeme -> tentative --user..."
    if do_install --user; then install_ok=1; INSTALL_MODE="pip"
    else echo "  Echec --user aussi -> fallback zero-install"
    fi
  fi
fi

# --- 4E. ZERO-INSTALL (toujours possible : kemus = 100% stdlib) ----------------
if [ "$install_ok" != "1" ]; then
  echo ">>> ZERO-INSTALL : creation du lanceur $BIN_DIR/kemus ..."
  echo "    (kemus n'a AUCUNE dependance : pip n'est pas necessaire)"
  if make_zero_install_wrapper; then
    install_ok=1; INSTALL_MODE="zero"
  else
    echo "ERREUR : impossible d'ecrire dans $BIN_DIR"
    echo "Solution directe :  python3 $SCRIPT_DIR/kemus.py"
    exit 1
  fi
fi

# --- 5. Verification de l'import -----------------------------------------------
echo ">>> Verification..."
IMPORT_OK=0
case "$INSTALL_MODE" in
  pip)   if $PY -c "import kemus; print('  OK : Kemus', kemus.VERSION)" 2>/dev/null; then IMPORT_OK=1; fi ;;
  venv)  if "$VENVDIR/bin/python" -c "import kemus; print('  OK : Kemus', kemus.VERSION)" 2>/dev/null; then IMPORT_OK=1; fi ;;
  zero)  if $PY -c "import ast; ast.parse(open('$SCRIPT_DIR/kemus.py').read()); print('  OK : kemus.py valide')" 2>/dev/null; then IMPORT_OK=1; fi ;;
esac
if [ "$IMPORT_OK" != "1" ]; then
  echo "ERREUR : verification echouee."
  echo "Solution directe :  python3 $SCRIPT_DIR/kemus.py"
  exit 1
fi

# --- 6. Localiser le lanceur ----------------------------------------------------
LAUNCHER=""
LAUNCH_DESC=""
case "$INSTALL_MODE" in
  venv)
    LAUNCHER="$VENVDIR/bin/python -m kemus"
    LAUNCH_DESC="$VENVDIR/bin/python -m kemus"
    if [ ! -d "$SCRIPT_DIR" ]; then LAUNCHER=""; fi
    ;;
  zero)
    if [ -x "$BIN_DIR/kemus" ]; then
      LAUNCHER="$BIN_DIR/kemus"
      LAUNCH_DESC="$BIN_DIR/kemus   (zero-install)"
    fi
    ;;
  *)
    BIN="$(command -v kemus 2>/dev/null || true)"
    if [ -n "$BIN" ]; then
      LAUNCHER="$BIN"; LAUNCH_DESC="$BIN"
    else
      LAUNCHER="$PY $SCRIPT_DIR/kemus.py"
      LAUNCH_DESC="$PY $SCRIPT_DIR/kemus.py"
    fi
    ;;
esac
# Fallback universel
if [ -z "$LAUNCHER" ]; then
  LAUNCHER="$PY $SCRIPT_DIR/kemus.py"
  LAUNCH_DESC="$PY $SCRIPT_DIR/kemus.py"
fi

PATH_NOTE=""
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) if [ -x "$BIN_DIR/kemus" ]; then
       PATH_NOTE="  NOTE : ajoutez au PATH :  export PATH=\"\$HOME/.local/bin:\$PATH\""
     fi ;;
esac

echo ""
echo "============================================================"
echo "  Installation reussie  [mode : $INSTALL_MODE]"
echo ""
echo "  Lanceur       : $LAUNCH_DESC"
echo "  Port          : $PORT"
echo "  Fichiers 'PC' : ~/.kemus/home/"
echo "  Logs          : ~/.kemus/kemus.log"
[ -n "$PATH_NOTE" ] && echo "$PATH_NOTE"
echo "============================================================"

# --- 7. Lancement ----------------------------------------------------------------
if [ "$LAUNCH" != "1" ]; then
  echo "(--no-launch) Pour lancer plus tard :"
  echo "   $LAUNCHER"
  exit 0
fi

if [ "$OPEN_BROWSER" = "1" ]; then
  (
    sleep 1.2
    for b in xdg-open open sensible-browser firefox google-chrome chromium; do
      command -v "$b" >/dev/null 2>&1 || continue
      "$b" "http://127.0.0.1:$PORT/" >/dev/null 2>&1 &
      break
    done
  ) 2>/dev/null &
fi

export KEMUS_PORT="$PORT"
echo ""
echo ">>> Demarrage de Kemus : http://127.0.0.1:$PORT/   (Ctrl+C pour arreter)"
echo ""

exec $LAUNCHER
