"""
Console unica per la dimostrazione con la camera a muoni di LHCb.

Riunisce in una sola finestra il video dell'endoscopio e il controllo
dell'alimentatore CAEN DT5471P.

NON modifica i programmi separati: `gui_caen.py`, `video_camera.py` e
`controllo_hv.py` restano utilizzabili come prima. Qui vengono riusati solo i
due moduli di backend (`hv_backend`, `camera_backend`).

A differenza di video_camera.py, questa console NON fa scegliere la camera:
cerca e usa solo l'endoscopio.

Avvio:
    /Users/farchill/.venv/hep2/bin/python3 console_lhcb.py
    /Users/farchill/.venv/hep2/bin/python3 console_lhcb.py --sim   # HV simulato

Scorciatoie:  F schermo intero · Esc esci · S snapshot
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
import time
import tkinter as tk
from tkinter import messagebox

import cv2
import customtkinter as ctk
from PIL import Image, ImageTk

import camera_backend as cam
import hv_backend as hv

ctk.set_appearance_mode("Dark")      # in sala buia si legge meglio
ctk.set_default_color_theme("blue")

CARTELLA = pathlib.Path(__file__).parent
LOGO = CARTELLA / "lhcb-logo.png"
# Il logo originale e' 11825x7100 (84 Mpixel): decodificarlo a ogni avvio
# costava ~5 s e centinaia di MB. Si tiene una copia ridotta accanto, rigenerata
# solo se l'originale cambia.
LOGO_RIDOTTO = CARTELLA / ".lhcb-logo-cache.png"
ALTEZZA_LOGO = 56
USCITA = CARTELLA / "acquisizioni"

PERIODO_VIDEO_MS = 33     # ~30 aggiornamenti al secondo
PERIODO_HV_MS = 1000      # letture dell'alimentatore

# Solo l'endoscopio accetta questo modo: la FaceTime ripiega su 1760x1328 e
# l'iPhone su 1920x1440. E' l'unico modo affidabile di riconoscerlo, perche'
# i nomi delle camere non corrispondono agli indici di OpenCV (vedi la nota in
# camera_backend.nomi_camere).
MODO_ENDOSCOPIO = (1600, 1200)
RISOLUZIONE_USO = (1280, 720)


def trova_endoscopio(massimo: int = 4) -> int | None:
    """Indice OpenCV dell'endoscopio, riconosciuto dai modi che accetta."""
    for indice in range(massimo):
        cap = cv2.VideoCapture(indice, cam._API)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, MODO_ENDOSCOPIO[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, MODO_ENDOSCOPIO[1])
        for _ in range(15):
            ok, frame = cap.read()
            if ok and frame is not None:
                break
        reale = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                 int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        cap.release()
        if reale == MODO_ENDOSCOPIO:
            return indice
    return None


class AlimentatoreRobusto(hv.HVDevice):
    """
    Alimentatore che sopravvive a una ri-enumerazione USB.

    Il DT5471P puo' sparire e ricomparire dal bus (succede toccando
    l'interruttore sul retro o con un contatto incerto del cavo): il descrittore
    aperto resta valido per il sistema ma ogni scrittura fallisce con
    errno 6, "Device not configured". Qui ogni operazione che fallisce cosi'
    provoca una chiusura, una nuova scansione delle porte e un secondo
    tentativo, senza che l'operatore debba fare nulla.

    Espone la stessa interfaccia di hv.HVDevice, percio' il resto della console
    la usa come se fosse il device vero.
    """

    PAUSA_FRA_TENTATIVI = 1.0  # s, per non martellare il bus

    def __init__(self, simula: bool = False) -> None:
        self._simula = simula
        self._dev: hv.HVDevice | None = None
        self._prossimo_tentativo = 0.0
        self.riconnessioni = 0
        self.porta: str | None = None

    # ------------------------------------------------------------- gestione
    @property
    def connesso(self) -> bool:
        return self._dev is not None

    def apri(self) -> str:
        """
        Apre il collegamento e restituisce il nome del modulo.

        Prova tutte le porte seriali e convalida ognuna con una transazione
        vera: dopo una ri-enumerazione il nodo /dev vecchio puo' sopravvivere
        per qualche secondo, e si apre senza errori pur essendo morto.
        """
        if self._simula:
            self._dev = hv.open_device(backend=hv.BACKEND_SIM)
            return self._dev.info()

        ultimo: Exception | None = None
        for porta in hv.list_serial_ports():
            tentativo = None
            try:
                tentativo = hv.open_device(backend=hv.BACKEND_DT54XX, port=porta)
                nome = tentativo.info()      # una porta fantasma fallisce qui
                self._dev = tentativo
                self.porta = porta
                return nome
            except Exception as exc:
                ultimo = exc
                if tentativo is not None:
                    try:
                        tentativo.close()    # niente descrittori abbandonati
                    except Exception:
                        pass
        raise hv.HVError(
            f"nessun DT54xx raggiungibile ({ultimo})" if ultimo
            else "nessuna porta seriale disponibile")

    def _scarta(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    def _riapri(self) -> bool:
        adesso = time.monotonic()
        if adesso < self._prossimo_tentativo:
            return False
        self._prossimo_tentativo = adesso + self.PAUSA_FRA_TENTATIVI
        try:
            self.apri()
        except Exception:
            self._dev = None
            return False
        self.riconnessioni += 1
        return True

    @staticmethod
    def _e_caduta(exc: Exception) -> bool:
        """
        Vale la pena riconnettersi per questo errore?

        Si', per tutto tranne un comando che il modulo ha esplicitamente
        rifiutato: quello e' un problema del comando, non del collegamento.

        La regola e' volutamente al contrario (tutto recuperabile salvo prova
        contraria) perche' quando il DT5471P si ri-enumera il flusso seriale
        resta DESINCRONIZZATO: in buffer rimangono pezzi di righe vecchie e il
        modulo risponde "risposta non valida" o "valore non numerico", non con
        un errore di sistema. Trattandoli come guasti definitivi la console
        restava bloccata su "collegamento perso" per sempre.
        """
        return "rifiutato" not in str(exc)

    def _esegui(self, azione):
        if self._dev is None and not self._riapri():
            raise hv.HVError("modulo non raggiungibile")
        try:
            return azione(self._dev)
        except Exception as exc:
            if not self._e_caduta(exc):
                raise                      # es. parametro rifiutato: non c'entra il cavo
            self._scarta()
            if self._riapri():
                return azione(self._dev)
            raise hv.HVError(f"collegamento perso ({exc})") from exc

    # ------------------------------------------------------------ interfaccia
    def read(self, name):
        return self._esegui(lambda d: d.read(name))

    def write(self, name, value) -> None:
        self._esegui(lambda d: d.write(name, value))

    def power(self, on: bool) -> None:
        self._esegui(lambda d: d.power(on))

    def clear_alarm(self) -> None:
        self._esegui(lambda d: d.clear_alarm())

    def info(self) -> str:
        return self._esegui(lambda d: d.info())

    def parameters(self):
        return self._esegui(lambda d: d.parameters())

    def close(self) -> None:
        self._scarta()


class Console(ctk.CTk):
    def __init__(self, simula_hv: bool = False) -> None:
        super().__init__()

        self.title("LHCb — Camera a muoni")
        self.geometry("1280x820")
        self.minsize(1000, 680)

        self.simula_hv = simula_hv
        self.stream: cam.CameraStream | None = None
        self.hv_dev: hv.HVDevice | None = None
        self.hv_acceso = False
        self.schermo_intero = False
        self._photo = None        # riferimento vivo per Tk
        self._logo = None
        # Id dei timer periodici, per poterli annullare alla chiusura: se
        # scattano dopo destroy() Tk stampa "invalid command name".
        self._timer_video: str | None = None
        self._timer_hv: str | None = None
        self._riconnessioni_viste = 0

        self._testata()
        self._corpo()
        self._barra_stato()

        self.bind("<f>", self.toggle_fullscreen)
        self.bind("<F11>", self.toggle_fullscreen)
        self.bind("<Escape>", self.esci_fullscreen)
        self.bind("<s>", lambda _e: self.snapshot())

        # Le aperture costano qualche secondo: prima si mostra la finestra.
        self.after(200, self.avvio_automatico)
        self.aggiorna_video()
        self.aggiorna_hv()

    # ------------------------------------------------------------- interfaccia
    @staticmethod
    def _carica_logo() -> Image.Image | None:
        """Logo gia' ridotto; lo genera al primo avvio e poi lo riusa."""
        if not LOGO.exists():
            return None
        try:
            if (not LOGO_RIDOTTO.exists()
                    or LOGO_RIDOTTO.stat().st_mtime < LOGO.stat().st_mtime):
                originale = Image.open(LOGO)
                originale.thumbnail((240, ALTEZZA_LOGO), Image.LANCZOS)
                originale.save(LOGO_RIDOTTO)
            return Image.open(LOGO_RIDOTTO)
        except Exception:
            return None

    def _testata(self) -> None:
        testata = ctk.CTkFrame(self, corner_radius=0)
        testata.pack(side="top", fill="x")

        immagine = self._carica_logo()
        if immagine is not None:
            self._logo = ctk.CTkImage(light_image=immagine, dark_image=immagine,
                                      size=immagine.size)
            ctk.CTkLabel(testata, image=self._logo, text="").pack(
                side="left", padx=16, pady=10)

        ctk.CTkLabel(testata, text="Camera a muoni — dimostrazione",
                     font=ctk.CTkFont(size=22, weight="bold")).pack(
            side="left", padx=8, pady=10)

        ctk.CTkButton(testata, text="Schermo intero (F)", width=150,
                      fg_color="gray30", hover_color="gray25",
                      command=self.toggle_fullscreen).pack(side="right", padx=16)

    def _corpo(self) -> None:
        corpo = ctk.CTkFrame(self, fg_color="transparent")
        corpo.pack(side="top", fill="both", expand=True, padx=12, pady=10)

        # --- video a sinistra ---
        colonna_video = ctk.CTkFrame(corpo)
        colonna_video.pack(side="left", fill="both", expand=True)

        self.area_video = tk.Label(colonna_video, bg="black", bd=0, highlightthickness=0,
                                   text="endoscopio non collegato",
                                   fg="gray60", font=("Helvetica", 16))
        self.area_video.pack(fill="both", expand=True, padx=10, pady=(10, 6))

        comandi_video = ctk.CTkFrame(colonna_video, fg_color="transparent")
        comandi_video.pack(fill="x", padx=10, pady=(0, 10))

        self.btn_video = ctk.CTkButton(comandi_video, text="Avvia video", width=130,
                                       command=self.toggle_video)
        self.btn_video.pack(side="left")
        self.btn_snapshot = ctk.CTkButton(comandi_video, text="Snapshot (S)", width=130,
                                          state="disabled", fg_color="gray30",
                                          hover_color="gray25", command=self.snapshot)
        self.btn_snapshot.pack(side="left", padx=8)
        self.lbl_video = ctk.CTkLabel(comandi_video, text="—",
                                      font=ctk.CTkFont(size=11), text_color="gray")
        self.lbl_video.pack(side="right")

        # --- alimentatore a destra ---
        colonna_hv = ctk.CTkFrame(corpo, width=320)
        colonna_hv.pack(side="right", fill="y", padx=(12, 0))
        colonna_hv.pack_propagate(False)

        ctk.CTkLabel(colonna_hv, text="Alta tensione",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(14, 2))
        self.lbl_modulo = ctk.CTkLabel(colonna_hv, text="non connesso",
                                       font=ctk.CTkFont(size=11), text_color="gray")
        self.lbl_modulo.pack(pady=(0, 10))

        riquadro = ctk.CTkFrame(colonna_hv)
        riquadro.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(riquadro, text="Tensione", font=ctk.CTkFont(size=11),
                     text_color="gray").pack(pady=(10, 0))
        self.lbl_vmon = ctk.CTkLabel(riquadro, text="—",
                                     font=ctk.CTkFont(size=34, weight="bold"))
        self.lbl_vmon.pack()
        ctk.CTkLabel(riquadro, text="Corrente", font=ctk.CTkFont(size=11),
                     text_color="gray").pack(pady=(10, 0))
        self.lbl_imon = ctk.CTkLabel(riquadro, text="—",
                                     font=ctk.CTkFont(size=28, weight="bold"))
        self.lbl_imon.pack(pady=(0, 12))

        self.lbl_stato_hv = ctk.CTkLabel(colonna_hv, text="—",
                                         font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_stato_hv.pack(pady=8)

        impostazioni = ctk.CTkFrame(colonna_hv)
        impostazioni.pack(fill="x", padx=12, pady=4)
        impostazioni.grid_columnconfigure(1, weight=1)

        self.campi_hv: dict[str, ctk.CTkEntry] = {}
        for riga, (etichetta, chiave, unita, esempio) in enumerate((
            ("Tensione", "VSet", "V", "1500"),
            ("Corrente max", "ISet", "µA", "10"),
            ("Rampa salita", "RUp", "V/s", "50"),
            ("Rampa discesa", "RDWn", "V/s", "50"),
        )):
            ctk.CTkLabel(impostazioni, text=etichetta, font=ctk.CTkFont(size=12)).grid(
                row=riga, column=0, padx=(10, 6), pady=5, sticky="w")
            campo = ctk.CTkEntry(impostazioni, width=80, placeholder_text=esempio)
            campo.grid(row=riga, column=1, padx=4, pady=5, sticky="ew")
            ctk.CTkLabel(impostazioni, text=unita, font=ctk.CTkFont(size=11),
                         text_color="gray", width=32).grid(
                row=riga, column=2, padx=(4, 10), pady=5, sticky="w")
            self.campi_hv[chiave] = campo

        self.btn_applica = ctk.CTkButton(impostazioni, text="Applica", state="disabled",
                                         command=self.applica_impostazioni)
        self.btn_applica.grid(row=4, column=0, columnspan=3, padx=10, pady=(6, 10),
                              sticky="ew")

        self.btn_hv = ctk.CTkButton(colonna_hv, text="HV ON", height=46,
                                    font=ctk.CTkFont(size=16, weight="bold"),
                                    fg_color="green", hover_color="darkgreen",
                                    state="disabled", command=self.toggle_hv)
        self.btn_hv.pack(fill="x", padx=12, pady=(16, 6))

        self.btn_clear = ctk.CTkButton(colonna_hv, text="Clear alarm", state="disabled",
                                       fg_color="gray30", hover_color="gray25",
                                       command=self.clear_alarm)
        self.btn_clear.pack(fill="x", padx=12, pady=4)

        self.btn_hv_conn = ctk.CTkButton(colonna_hv, text="Connetti", state="normal",
                                         fg_color="gray30", hover_color="gray25",
                                         command=self.toggle_hv_connessione)
        self.btn_hv_conn.pack(fill="x", padx=12, pady=(16, 12))

    def _barra_stato(self) -> None:
        self.lbl_stato = ctk.CTkLabel(self, text="Avvio...", font=ctk.CTkFont(size=11),
                                      text_color="gray")
        self.lbl_stato.pack(side="bottom", pady=(0, 8))

    def stato(self, testo: str) -> None:
        self.lbl_stato.configure(text=testo)

    # ----------------------------------------------------------------- avvio
    def avvio_automatico(self) -> None:
        self.stato("Ricerca endoscopio e alimentatore...")
        self.update_idletasks()
        self.avvia_video(silenzioso=True)
        self.connetti_hv(silenzioso=True)
        if self.stream is None and self.hv_dev is None:
            self.stato("Nessun dispositivo trovato — collega USB e premi Avvia / Connetti")

    # ----------------------------------------------------------------- video
    def toggle_video(self) -> None:
        if self.stream is None:
            self.avvia_video()
        else:
            self.ferma_video()

    def avvia_video(self, silenzioso: bool = False) -> None:
        indice = trova_endoscopio()
        if indice is None:
            self.stato("Endoscopio non trovato")
            if not silenzioso:
                messagebox.showwarning(
                    "Endoscopio",
                    "Endoscopio non trovato.\n\nControlla il cavo USB. Se il Mac "
                    "chiede il permesso della fotocamera, concedilo e riprova.",
                )
            return

        try:
            self.stream = cam.CameraStream(indice, *RISOLUZIONE_USO)
        except cam.CameraError as exc:
            self.stream = None
            self.stato(f"Endoscopio: {exc}")
            if not silenzioso:
                messagebox.showerror("Endoscopio", str(exc))
            return

        self.area_video.configure(text="")
        self.btn_video.configure(text="Ferma video")
        self.btn_snapshot.configure(state="normal")
        self.stato(f"Endoscopio attivo (camera {indice})")

    def ferma_video(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self._photo = None
        self.area_video.configure(image="", text="video fermo")
        self.btn_video.configure(text="Avvia video")
        self.btn_snapshot.configure(state="disabled")
        self.lbl_video.configure(text="—")

    def snapshot(self) -> None:
        if self.stream is None:
            return
        frame = self.stream.frame()
        if frame is None:
            return
        USCITA.mkdir(exist_ok=True)
        nome = datetime.datetime.now().strftime("muoni_%Y%m%d_%H%M%S.png")
        cv2.imwrite(str(USCITA / nome), frame)
        self.stato(f"Snapshot salvato: {nome}")

    def aggiorna_video(self) -> None:
        if self.stream is not None:
            if not self.stream.attivo:
                errore = self.stream.errore or "flusso terminato"
                self.ferma_video()
                self.stato(f"Endoscopio: {errore}")
            else:
                frame = self.stream.frame()
                if frame is not None:
                    self._disegna(frame)
        self._timer_video = self.after(PERIODO_VIDEO_MS, self.aggiorna_video)

    def _disegna(self, frame) -> None:
        larghezza = max(self.area_video.winfo_width(), 1)
        altezza = max(self.area_video.winfo_height(), 1)
        if larghezza < 10 or altezza < 10:
            return
        h, w = frame.shape[:2]
        scala = min(larghezza / w, altezza / h)
        misura = (max(int(w * scala), 1), max(int(h * scala), 1))

        immagine = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        self._photo = ImageTk.PhotoImage(immagine.resize(misura, Image.BILINEAR))
        self.area_video.configure(image=self._photo)

        testo = f"{w}x{h} — {self.stream.fps:.0f} fps"
        if self.stream.frame_nero():
            testo += "  ⚠ immagine nera: accendi i LED"
        self.lbl_video.configure(text=testo)

    # -------------------------------------------------------------------- HV
    def toggle_hv_connessione(self) -> None:
        if self.hv_dev is None:
            self.connetti_hv()
        else:
            self.disconnetti_hv()

    def connetti_hv(self, silenzioso: bool = False) -> None:
        alimentatore = AlimentatoreRobusto(self.simula_hv)
        try:
            nome = alimentatore.apri()
            self.hv_dev = alimentatore
        except Exception as exc:
            self.hv_dev = None
            self.stato(f"Alimentatore: {exc}")
            if not silenzioso:
                messagebox.showerror("Alimentatore", str(exc))
            return

        self.lbl_modulo.configure(text=nome or "modulo connesso")
        self.btn_hv_conn.configure(text="Disconnetti")
        for bottone in (self.btn_applica, self.btn_hv, self.btn_clear):
            bottone.configure(state="normal")
        self._rileggi_impostazioni()
        self.stato(f"Alimentatore connesso: {nome}")

    def disconnetti_hv(self) -> None:
        if self.hv_dev is None:
            return
        try:
            self.hv_dev.close()
        finally:
            self.hv_dev = None
        self.lbl_modulo.configure(text="non connesso")
        self.btn_hv_conn.configure(text="Connetti")
        for bottone in (self.btn_applica, self.btn_hv, self.btn_clear):
            bottone.configure(state="disabled")
        self.lbl_vmon.configure(text="—")
        self.lbl_imon.configure(text="—")
        self.lbl_stato_hv.configure(text="—")
        self.stato("Alimentatore disconnesso")

    def _rileggi_impostazioni(self) -> None:
        """Riempie i campi con quanto e' gia' programmato nel modulo."""
        for chiave, campo in self.campi_hv.items():
            try:
                valore = float(self.hv_dev.read(chiave))
            except Exception:
                continue
            campo.delete(0, "end")
            campo.insert(0, f"{valore:g}")

    def applica_impostazioni(self) -> None:
        """Invia tensione, limite di corrente e rampe."""
        da_inviare = {}
        for chiave, campo in self.campi_hv.items():
            testo = campo.get().strip()
            if not testo:
                continue
            try:
                da_inviare[chiave] = float(testo)
            except ValueError:
                messagebox.showwarning("Impostazioni",
                                       f"Valore non valido per {chiave}: {testo!r}")
                return

        inviati = []
        for chiave, valore in da_inviare.items():
            try:
                self.hv_dev.write(chiave, valore)
            except Exception as exc:
                messagebox.showerror("Impostazioni",
                                     f"Impossibile impostare {chiave}:\n{exc}")
                return
            inviati.append(f"{chiave}={valore:g}")

        self._rileggi_impostazioni()   # mostra cosa il modulo ha davvero accettato
        self.stato("Impostazioni applicate: " + ", ".join(inviati))

    def toggle_hv(self) -> None:
        nuovo = not self.hv_acceso
        try:
            self.hv_dev.power(nuovo)
        except Exception as exc:
            messagebox.showerror("Alta tensione", f"Comando fallito:\n{exc}")
            return
        self.hv_acceso = nuovo
        self._aspetto_bottone_hv()
        self.stato("HV ON" if nuovo else "HV OFF")

    def clear_alarm(self) -> None:
        try:
            self.hv_dev.clear_alarm()
        except Exception as exc:
            messagebox.showerror("Clear alarm", str(exc))
            return
        self.stato("Allarme azzerato")

    def _aspetto_bottone_hv(self) -> None:
        if self.hv_acceso:
            self.btn_hv.configure(text="HV OFF", fg_color="orange", hover_color="darkorange")
        else:
            self.btn_hv.configure(text="HV ON", fg_color="green", hover_color="darkgreen")

    def aggiorna_hv(self) -> None:
        if self.hv_dev is not None:
            try:
                self.lbl_vmon.configure(text=f"{self.hv_dev.vmon:.1f} V")
                self.lbl_imon.configure(text=f"{self.hv_dev.imon:.2f} µA")
                parola = self.hv_dev.status
                self.hv_acceso = bool(parola & 1)
                self._aspetto_bottone_hv()
                descrizione = hv.describe_status(parola)
                self.lbl_stato_hv.configure(
                    text=descrizione,
                    text_color="orange" if parola & ~1 else "gray")
                if self.hv_dev.riconnessioni != self._riconnessioni_viste:
                    self._riconnessioni_viste = self.hv_dev.riconnessioni
                    self.stato(f"Alimentatore riconnesso "
                               f"(ri-enumerazione USB n. {self._riconnessioni_viste})")
            except Exception as exc:
                self.lbl_vmon.configure(text="—")
                self.lbl_imon.configure(text="—")
                self.lbl_stato_hv.configure(text="collegamento perso", text_color="orange")
                self.stato(f"Alimentatore non raggiungibile, riprovo... ({exc})")
        self._timer_hv = self.after(PERIODO_HV_MS, self.aggiorna_hv)

    # ------------------------------------------------------------ fullscreen
    def toggle_fullscreen(self, _evento=None) -> None:
        self.schermo_intero = not self.schermo_intero
        self.attributes("-fullscreen", self.schermo_intero)

    def esci_fullscreen(self, _evento=None) -> None:
        if self.schermo_intero:
            self.schermo_intero = False
            self.attributes("-fullscreen", False)

    # --------------------------------------------------------------- chiusura
    def chiudi(self) -> None:
        for timer in (self._timer_video, self._timer_hv):
            if timer is not None:
                self.after_cancel(timer)
        self._timer_video = self._timer_hv = None

        if self.hv_dev is not None and self.hv_acceso:
            if messagebox.askyesno("Uscita", "L'alta tensione e' accesa.\n"
                                             "Spegnerla prima di chiudere?"):
                try:
                    self.hv_dev.power(False)
                except Exception:
                    pass
        self.ferma_video()
        if self.hv_dev is not None:
            try:
                self.hv_dev.close()
            except Exception:
                pass
        self.destroy()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Console dimostrazione camera a muoni")
    parser.add_argument("--sim", action="store_true",
                        help="usa l'alimentatore simulato invece di quello reale")
    args = parser.parse_args(argv)

    app = Console(simula_hv=args.sim)
    app.protocol("WM_DELETE_WINDOW", app.chiudi)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
