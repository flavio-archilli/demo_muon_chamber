"""
Acquisizione video dalla camera USB (UVC), separata dalla GUI.

Pensato per l'endoscopio YPCendoscope usato nella dimostrazione con la camera
a muoni, ma funziona con qualunque camera UVC (e con la FaceTime HD integrata).

La cattura gira su un thread dedicato e tiene in memoria solo l'ultimo frame:
la GUI lo preleva quando vuole, senza mai bloccarsi sulla lettura.
"""

from __future__ import annotations

import platform
import threading
import time
from dataclasses import dataclass

import cv2

# Su macOS conviene forzare AVFoundation: il backend di default puo' fallire.
_API = cv2.CAP_AVFOUNDATION if platform.system() == "Darwin" else cv2.CAP_ANY

# Risoluzioni offerte nella tendina. Chiederne una non supportata non da'
# errore: la camera ne consegna un'altra, percio' la GUI mostra sempre quella
# davvero ottenuta. L'endoscopio arriva a 1600x1200 (a 10 fps).
RISOLUZIONI = ((320, 240), (640, 480), (800, 600), (1280, 720), (1600, 900),
               (1600, 1200), (1920, 1080))


class CameraError(Exception):
    """Camera non apribile, non autorizzata o scollegata."""


def nomi_camere() -> list[str]:
    """
    Nomi delle camere secondo AVFoundation.

    SOLO INFORMATIVO: NON usarlo per scegliere quale camera aprire. Verificato
    sull'hardware il 25/09/2026: l'ordine di AVFoundation, quello di
    `system_profiler` e quello degli indici di OpenCV sono tre ordini diversi,
    e per giunta cambiano da un'esecuzione all'altra. L'unico modo affidabile
    di identificare una camera e' aprirla e guardare cosa consegna: vedi
    sonda_camere().
    """
    try:
        import AVFoundation as av
        return [d.localizedName()
                for d in av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo)]
    except Exception:
        return []


@dataclass(frozen=True)
class CameraTrovata:
    """Risultato della sonda su un indice OpenCV."""
    indice: int
    larghezza: int
    altezza: int
    nera: bool

    def etichetta(self) -> str:
        testo = f"{self.indice}: {self.larghezza}x{self.altezza}"
        return testo + (" (nera)" if self.nera else "")


def sonda_camere(massimo: int = 4, attesa: float = 1.5) -> list[CameraTrovata]:
    """
    Apre a turno gli indici e riporta cosa consegna ciascuno.

    E' l'unico modo affidabile di sapere quale indice e' quale camera, visto
    che i nomi non sono allineati agli indici. Costa qualche secondo, quindi si
    chiama all'avvio e quando l'utente preme il pulsante di ricerca.
    """
    trovate: list[CameraTrovata] = []
    falliti_di_fila = 0
    for indice in range(massimo):
        cap = cv2.VideoCapture(indice, _API)
        if not cap.isOpened():
            cap.release()
            falliti_di_fila += 1
            if falliti_di_fila >= 2:
                break
            continue
        falliti_di_fila = 0

        frame = None
        scadenza = time.monotonic() + attesa
        while time.monotonic() < scadenza:
            ok, letto = cap.read()
            if ok and letto is not None:
                frame = letto
                if frame.any():
                    break  # immagine vera: inutile insistere
            time.sleep(0.05)

        if frame is not None:
            altezza, larghezza = frame.shape[:2]
            trovate.append(CameraTrovata(indice, larghezza, altezza, not frame.any()))
        cap.release()
        time.sleep(0.3)  # AVFoundation non gradisce riaperture immediate
    return trovate


def prima_camera_utile(massimo: int = 4) -> int | None:
    """Indice della prima camera che consegna un'immagine non nera."""
    for trovata in sonda_camere(massimo):
        if not trovata.nera:
            return trovata.indice
    return None


def permesso_negato() -> bool:
    """True se il sistema elenca delle camere ma nessuna e' apribile."""
    return bool(nomi_camere()) and not sonda_camere()


class CameraStream:
    """
    Cattura continua su thread separato; `frame()` restituisce l'ultimo fotogramma.

    I fotogrammi sono array BGR di OpenCV: la conversione per lo schermo la fa
    il chiamante, cosi' il thread di cattura resta il piu' leggero possibile.
    """

    def __init__(self, indice: int = 0, larghezza: int | None = None,
                 altezza: int | None = None) -> None:
        self._cap = cv2.VideoCapture(indice, _API)
        if not self._cap.isOpened():
            raise CameraError(
                f"camera {indice} non apribile. Su macOS verifica il permesso in "
                "Impostazioni di Sistema > Privacy e sicurezza > Fotocamera, "
                "e che la camera non sia gia' in uso da un altro programma."
            )

        if larghezza and altezza:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, larghezza)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, altezza)

        self.indice = indice
        self._frame = None
        self._lock = threading.Lock()
        self._attivo = True
        self._errore: str | None = None

        # Misura degli fps effettivi
        self._conteggio = 0
        self._t_fps = time.monotonic()
        self._fps = 0.0

        # Alcune camere UVC (l'endoscopio in particolare) impiegano qualche
        # lettura prima di consegnare il primo fotogramma: insistere ~3 s.
        primo = None
        for _ in range(30):
            ok, letto = self._cap.read()
            if ok and letto is not None:
                primo = letto
                break
            time.sleep(0.1)
        if primo is None:
            self._cap.release()
            raise CameraError(
                f"camera {indice} aperta ma non fornisce fotogrammi entro 3 s "
                "(gia' in uso da un altro programma?)"
            )
        self._frame = primo

        self._thread = threading.Thread(target=self._cattura, daemon=True)
        self._thread.start()

    # -------------------------------------------------------------- interno
    def _cattura(self) -> None:
        while self._attivo:
            ok, frame = self._cap.read()
            if not ok:
                self._errore = "flusso interrotto (camera scollegata?)"
                self._attivo = False
                break
            with self._lock:
                self._frame = frame
            self._conteggio += 1
            adesso = time.monotonic()
            trascorso = adesso - self._t_fps
            if trascorso >= 1.0:
                self._fps = self._conteggio / trascorso
                self._conteggio = 0
                self._t_fps = adesso

    # -------------------------------------------------------------- pubblico
    def frame(self):
        """Ultimo fotogramma catturato (copia), oppure None."""
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def risoluzione(self) -> tuple[int, int]:
        return (int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

    @property
    def errore(self) -> str | None:
        return self._errore

    def frame_nero(self) -> bool:
        """
        True se l'ultimo fotogramma e' interamente a zero.

        Sugli endoscopi USB e' quasi sempre l'anello di LED spento (la rotella
        sul cavo, non regolabile via software) oppure il cappuccio sull'ottica:
        un sensore al buio darebbe comunque del rumore, non zero esatto.
        """
        with self._lock:
            return self._frame is not None and not self._frame.any()

    @property
    def attivo(self) -> bool:
        return self._attivo

    def imposta_proprieta(self, nome: str, valore: float) -> bool:
        """
        Tenta di impostare luminosita'/contrasto/saturazione.

        Molte camere UVC economiche (endoscopi compresi) non le espongono, e i
        LED si regolano solo con la rotella sul cavo: il valore di ritorno dice
        se il driver ha accettato.
        """
        proprieta = {
            "brightness": cv2.CAP_PROP_BRIGHTNESS,
            "contrast": cv2.CAP_PROP_CONTRAST,
            "saturation": cv2.CAP_PROP_SATURATION,
        }.get(nome)
        if proprieta is None:
            return False
        return bool(self._cap.set(proprieta, valore))

    def close(self) -> None:
        self._attivo = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._cap.release()

    def __enter__(self) -> "CameraStream":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
