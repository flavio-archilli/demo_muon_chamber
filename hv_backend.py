"""
Backend di comunicazione per gli alimentatori di Alta Tensione CAEN.

Interfaccia comune (HVDevice) con tre implementazioni:

  * CaenDT54xxDevice   -> DT5470/DT5471/DT5472 via porta seriale USB.
                          Implementa il protocollo ASCII documentato nel
                          manuale UM3178 (DT54xx User's Manual, cap. 4).
                          Funziona su macOS/Linux/Windows: serve solo pyserial,
                          NESSUNA libreria nativa CAEN.
  * CaenHVWrapperDevice -> crate SY/N1470/DT55xx via CAEN HV Wrapper
                          (richiede la libreria nativa, non disponibile su macOS
                          e comunque NON supporta la famiglia DT547x).
  * SimulatedDevice    -> simulatore software, per lavorare senza modulo.

Protocollo DT54xx (UM3178 cap. 4)
---------------------------------
Porta seriale: 9600 baud, 8 bit, no parity, 1 stop bit.
Comando:   $CMD:<MON|SET>,PAR:<nome>[,VAL:<valore>]<CR><LF>
Risposta:  #CMD:OK[,VAL:<valore>]<CR><LF>   oppure   #CMD:ERR...
"""

from __future__ import annotations

import random
import time
from typing import Sequence

try:
    import serial
    from serial.tools import list_ports
    _SERIAL_ERROR: Exception | None = None
except Exception as exc:  # pyserial non installato
    serial = None  # type: ignore[assignment]
    list_ports = None  # type: ignore[assignment]
    _SERIAL_ERROR = exc

try:
    from caen_libs import caenhvwrapper as hv
    _HVWRAPPER_ERROR: Exception | None = None
except Exception as exc:  # ImportError, o RuntimeError se manca la libreria nativa
    hv = None  # type: ignore[assignment]
    _HVWRAPPER_ERROR = exc


# Backend disponibili
BACKEND_DT54XX = "dt54xx"
BACKEND_HVWRAPPER = "hvwrapper"
BACKEND_SIM = "sim"
BACKENDS = (BACKEND_DT54XX, BACKEND_HVWRAPPER, BACKEND_SIM)

# Default per l'HV Wrapper. ATTENZIONE: "DT547X" non esiste fra i SystemType
# (SY1527, SY2527, SY4527, SY5527, N568, V65XX, N1470, V8100, N568E, DT55XX,
# FTK, DT55XXE, N1068, SMARTHV, NGPS, N1168, R6060): la famiglia DT547x NON e'
# supportata dall'HV Wrapper, va usato il backend seriale.
DEFAULT_SYSTEM_TYPE = "N1470"
DEFAULT_LINK_TYPE = "USB_VCP"
DEFAULT_ARG = "0"
DEFAULT_SLOT = 0
DEFAULT_CHANNEL = 0


class HVError(Exception):
    """Errore di comunicazione o comando rifiutato dal modulo."""


# --------------------------------------------------------------------------- #
# Disponibilita' dei backend
# --------------------------------------------------------------------------- #

def is_serial_available() -> bool:
    return serial is not None


def is_hvwrapper_available() -> bool:
    return hv is not None


def serial_error() -> str:
    if _SERIAL_ERROR is None:
        return ""
    return f"{type(_SERIAL_ERROR).__name__}: {_SERIAL_ERROR}"


def hvwrapper_error() -> str:
    if _HVWRAPPER_ERROR is None:
        return ""
    return f"{type(_HVWRAPPER_ERROR).__name__}: {_HVWRAPPER_ERROR}"


def list_serial_ports() -> list[str]:
    """
    Porte seriali presenti. Su macOS il DT54xx compare come /dev/cu.usbserial-*.

    Le porte con un VID USB sono dispositivi reali: se ce ne sono, le altre
    (Bluetooth, console di debug) vengono scartate.
    """
    if list_ports is None:
        return []
    tutte = list(list_ports.comports())
    usb = [p.device for p in tutte if p.vid is not None]
    if usb:
        return usb
    return [p.device for p in tutte
            if "Bluetooth" not in p.device and "debug-console" not in p.device]


def system_types() -> tuple[str, ...]:
    return tuple(t.name for t in hv.SystemType) if hv is not None else ()


def link_types() -> tuple[str, ...]:
    return tuple(t.name for t in hv.LinkType) if hv is not None else ()


# --------------------------------------------------------------------------- #
# Status word (UM3178, $CMD:MON,PAR:STAT)
# --------------------------------------------------------------------------- #

# ATTENZIONE: il manuale UM3178 si contraddice sulla status word.
# La tabella del protocollo (cap. 4, p.16) elenca: ... OVP(8), reserved(9),
# DIS(10), KILL(11), ILK(12), NOCAL(13).
# Il VI "Channel Status" del driver LabVIEW (p.32-33) elenca invece:
# ... TRIPPED(7), VCC FAIL(8), OVT(9), CAL_CHKERR(10), DISABLED(11), INTLCK(12),
# cioe' un bit in piu' dopo il 7 e tutto il resto shiftato.
# Verificato sull'hardware (DT5471P, 24/09/2026): con l'interruttore HV del
# pannello su DIS il modulo espone bit 11, e il canale rifiuta SET:ON; portando
# l'interruttore su EN il bit si azzera. Vale quindi l'ordinamento LabVIEW.
STATUS_BITS: tuple[tuple[int, str, str], ...] = (
    (0,  "ON",      "canale acceso"),
    (1,  "RUP",     "rampa di salita"),
    (2,  "RDW",     "rampa di discesa"),
    (3,  "OVC",     "sovracorrente"),
    (4,  "OVV",     "sovratensione"),
    (5,  "UNV",     "sottotensione"),
    (6,  "MAXV",    "in protezione MAXV"),
    (7,  "TRIP",    "generatore di corrente (trip)"),
    (8,  "VCCFAIL", "guasto alimentazione interna"),
    (9,  "OVT",     "sovratemperatura"),
    (10, "CALERR",  "errore di calibrazione"),
    (11, "DIS",     "canale disabilitato dall'interruttore HV del pannello"),
    (12, "ILK",     "canale in INTERLOCK"),
)


def decode_status(word: int) -> list[str]:
    """Nomi dei flag attivi nella status word."""
    return [nome for bit, nome, _ in STATUS_BITS if word & (1 << bit)]


def describe_status(word: int) -> str:
    attivi = decode_status(word)
    return ", ".join(attivi) if attivi else "OFF"


# --------------------------------------------------------------------------- #
# Interfaccia comune
# --------------------------------------------------------------------------- #

class HVDevice:
    """Un solo canale: quello scelto all'apertura."""

    def read(self, name: str) -> float | int | str:
        raise NotImplementedError

    def write(self, name: str, value: float | int | str) -> None:
        raise NotImplementedError

    def power(self, on: bool) -> None:
        self.write("Pw", 1 if on else 0)

    @property
    def vmon(self) -> float:
        return float(self.read("VMon"))

    @property
    def imon(self) -> float:
        return float(self.read("IMon"))

    @property
    def status(self) -> int:
        return int(self.read("Status"))

    def clear_alarm(self) -> None:
        """Rimuove una condizione di allarme (es. interlock)."""

    def info(self) -> str:
        """Identificativo del modulo."""
        return ""

    def parameters(self) -> Sequence[str]:
        return ()

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> "HVDevice":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# DT5470 / DT5471 / DT5472 -- protocollo seriale ASCII (UM3178 cap. 4)
# --------------------------------------------------------------------------- #

class CaenDT54xxDevice(HVDevice):
    """
    Driver nativo Python per la famiglia DT547x.

    Non usa alcuna libreria CAEN: il modulo espone una porta seriale USB e
    parla il protocollo ASCII del manuale. Il canale e' unico, quindi non ci
    sono parametri slot/canale.
    """

    BAUDRATE = 9600
    TERMINATOR = "\r\n"

    # Parametri leggibili (nome protocollo -> conversione)
    _MON_FLOAT = ("VSET", "ISET", "VMON", "IMON", "RUP", "RDW", "TRIP", "MAXV", "IMAX")
    _MON_INT = ("STAT", "TYPE")
    _MON_STR = ("BDNAME", "POLARITY", "PDWN", "IMRANGE")

    # Alias comodi (nomi in stile HV Wrapper) -> nome di protocollo
    _ALIAS = {
        "vset": "VSET", "iset": "ISET", "vmon": "VMON", "imon": "IMON",
        "rup": "RUP", "rdwn": "RDW", "rdw": "RDW", "trip": "TRIP",
        "maxv": "MAXV", "imax": "IMAX", "status": "STAT", "stat": "STAT",
        "pdwn": "PDWN", "imrange": "IMRANGE", "polarity": "POLARITY",
        "bdname": "BDNAME", "type": "TYPE",
    }

    def __init__(self, port: str, timeout: float = 1.0, rtscts: bool = False) -> None:
        if serial is None:
            raise HVError("pyserial non disponibile: " + serial_error())
        # Il manuale suggerisce Tera Term con flow control hardware, ma la
        # schermata delle impostazioni riporta 'none': il default qui e' none,
        # usa rtscts=True se il modulo non risponde.
        self._port = serial.Serial(
            port=port,
            baudrate=self.BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=rtscts,
            timeout=timeout,
            write_timeout=timeout,
        )
        # Scarta eventuali residui (il modulo ha anche un menu interattivo).
        time.sleep(0.1)
        self._port.reset_input_buffer()
        self._port.reset_output_buffer()

    # ----------------------------------------------------------- protocollo
    def _transact(self, cmd: str, par: str, val: str | None = None) -> str:
        """Invia un comando e restituisce il campo VAL della risposta."""
        stringa = f"$CMD:{cmd},PAR:{par}"
        if val is not None:
            stringa += f",VAL:{val}"
        stringa += self.TERMINATOR

        self._port.reset_input_buffer()
        self._port.write(stringa.encode("ascii"))
        self._port.flush()

        grezza = self._port.readline()
        if not grezza:
            raise HVError(f"nessuna risposta al comando {stringa.strip()!r}")

        risposta = grezza.decode("ascii", errors="replace").strip()
        if not risposta.startswith("#"):
            raise HVError(f"risposta non valida: {risposta!r}")

        # #CMD:OK,VAL:xxx.xx  oppure  #CMD:ERR...
        corpo = risposta[1:]
        testa, _, coda = corpo.partition(",")
        esito = testa.split(":", 1)[-1].strip()
        valore = coda.split(":", 1)[-1].strip() if coda else ""

        if not esito.upper().startswith("OK"):
            raise HVError(f"comando {stringa.strip()!r} rifiutato: {esito} {valore}".strip())
        return valore

    def _monitor(self, par: str) -> str:
        return self._transact("MON", par)

    def _set(self, par: str, val: str | None = None) -> None:
        self._transact("SET", par, val)

    # ----------------------------------------------------------- HVDevice
    def read(self, name: str) -> float | int | str:
        chiave = name.lower()
        if chiave == "pw":
            return 1 if self.status & 1 else 0

        par = self._ALIAS.get(chiave, name.upper())
        grezzo = self._monitor(par)

        if par in self._MON_FLOAT:
            try:
                return float(grezzo)
            except ValueError as exc:
                raise HVError(f"{par}: valore non numerico {grezzo!r}") from exc
        if par in self._MON_INT:
            try:
                return int(float(grezzo))
            except ValueError as exc:
                raise HVError(f"{par}: valore non numerico {grezzo!r}") from exc
        return grezzo

    def write(self, name: str, value: float | int | str) -> None:
        chiave = name.lower()

        if chiave == "pw":
            self._set("ON" if int(value) else "OFF")
            return
        if chiave in ("pdwn", "imrange"):
            self._set(self._ALIAS[chiave], str(value).upper())
            return

        par = self._ALIAS.get(chiave, name.upper())
        if par in ("RUP", "RDW"):
            # Il manuale usa VAL:xxx. per le rampe (risoluzione 1 V/s).
            self._set(par, f"{float(value):.0f}")
        else:
            self._set(par, f"{float(value):.2f}")

    def power(self, on: bool) -> None:
        self._set("ON" if on else "OFF")

    def clear_alarm(self) -> None:
        self._set("BDCLR")

    def info(self) -> str:
        return self._monitor("BDNAME")

    def parameters(self) -> Sequence[str]:
        return ("VSet", "ISet", "VMon", "IMon", "RUp", "RDWn", "Trip",
                "MaxV", "IMax", "Pw", "PDwn", "IMRange", "Polarity", "Status")

    def close(self) -> None:
        self._port.close()


# --------------------------------------------------------------------------- #
# CAEN HV Wrapper (crate SY, N1470, DT55xx...)
# --------------------------------------------------------------------------- #

class CaenHVWrapperDevice(HVDevice):

    def __init__(self, device, slot: int, channel: int) -> None:
        self._device = device
        self._slot = slot
        self._channel = channel

    @classmethod
    def open(
        cls,
        arg: str = DEFAULT_ARG,
        system_type: str = DEFAULT_SYSTEM_TYPE,
        link_type: str = DEFAULT_LINK_TYPE,
        username: str = "",
        password: str = "",
        slot: int = DEFAULT_SLOT,
        channel: int = DEFAULT_CHANNEL,
    ) -> "CaenHVWrapperDevice":
        if hv is None:
            raise HVError("CAEN HV Wrapper non disponibile: " + hvwrapper_error())
        # Device.open() SOLLEVA un'eccezione in caso di errore: non restituisce
        # un handle negativo come la vecchia API C.
        device = hv.Device.open(
            hv.SystemType[system_type],
            hv.LinkType[link_type],
            str(arg),
            username,
            password,
        )
        return cls(device, slot, channel)

    def read(self, name: str) -> float | int | str:
        # get_ch_param lavora su una LISTA di canali e restituisce una lista.
        return self._device.get_ch_param(self._slot, [self._channel], name)[0]

    def write(self, name: str, value: float | int | str) -> None:
        self._device.set_ch_param(self._slot, [self._channel], name, value)

    def parameters(self) -> Sequence[str]:
        return self._device.get_ch_param_info(self._slot, self._channel)

    def close(self) -> None:
        self._device.close()


# --------------------------------------------------------------------------- #
# Simulatore
# --------------------------------------------------------------------------- #

class SimulatedDevice(HVDevice):
    """Rampa lineare verso VSet, corrente con un po' di rumore."""

    def __init__(self, ramp_rate: float = 50.0) -> None:
        self._vset = 0.0
        self._pw = 0
        self._vmon = 0.0
        self._ramp = ramp_rate
        self._t = time.monotonic()

    def _advance(self) -> None:
        now = time.monotonic()
        dt, self._t = now - self._t, now
        target = self._vset if self._pw else 0.0
        passo = self._ramp * dt
        delta = target - self._vmon
        self._vmon += max(-passo, min(passo, delta))

    def read(self, name: str) -> float | int | str:
        self._advance()
        chiave = name.lower()
        if chiave == "vmon":
            rumore = random.uniform(-0.2, 0.2) if self._vmon > 1.0 else 0.0
            return self._vmon + rumore
        if chiave == "imon":
            return self._vmon / 300.0 + random.uniform(0.0, 0.05)
        if chiave == "vset":
            return self._vset
        if chiave == "pw":
            return self._pw
        if chiave in ("status", "stat"):
            parola = 0
            if self._pw:
                parola |= 1 << 0
                if self._vmon < self._vset - 1.0:
                    parola |= 1 << 1
            elif self._vmon > 1.0:
                parola |= 1 << 2
            return parola
        return 0.0

    def write(self, name: str, value: float | int | str) -> None:
        self._advance()
        chiave = name.lower()
        if chiave == "vset":
            self._vset = float(value)
        elif chiave == "pw":
            self._pw = int(value)

    def info(self) -> str:
        return "DT5471 3kV/500uA (simulato)"

    def parameters(self) -> Sequence[str]:
        return ("VSet", "ISet", "VMon", "IMon", "Pw", "RUp", "RDWn", "Status")

    def close(self) -> None:
        self._pw = 0
        self._vmon = 0.0


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def open_device(backend: str = BACKEND_DT54XX, **kwargs) -> HVDevice:
    """
    Apre il device col backend richiesto.

    dt54xx    -> port=<porta seriale>, [timeout], [rtscts]
    hvwrapper -> arg, system_type, link_type, slot, channel, username, password
    sim       -> nessun parametro
    """
    if backend == BACKEND_SIM:
        return SimulatedDevice()

    if backend == BACKEND_DT54XX:
        porta = kwargs.get("port")
        if not porta:
            disponibili = list_serial_ports()
            if not disponibili:
                raise HVError(
                    "nessuna porta seriale trovata: collega il modulo DT54xx "
                    "e verifica il driver USB"
                )
            porta = disponibili[0]
        return CaenDT54xxDevice(
            porta,
            timeout=kwargs.get("timeout", 1.0),
            rtscts=kwargs.get("rtscts", False),
        )

    if backend == BACKEND_HVWRAPPER:
        accettati = ("arg", "system_type", "link_type", "username", "password",
                     "slot", "channel")
        return CaenHVWrapperDevice.open(
            **{k: v for k, v in kwargs.items() if k in accettati}
        )

    raise HVError(f"backend sconosciuto: {backend!r} (validi: {', '.join(BACKENDS)})")
